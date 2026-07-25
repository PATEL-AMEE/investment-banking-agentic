"""GraphRAG retrieval: vector search + knowledge-graph enrichment.

Indexes the Policy/Regulation/Evidence corpus (and, as documents are
ingested, their text chunks) into a vector store, then answers queries by
semantic similarity and enriches each hit with its graph relationships —
which clients/documents a piece of evidence belongs to, which
policy/regulation a chunk cites. This replaces the old keyword substring
match behind the ``policy_search`` tool.

Vector backend: Azure AI Search (hybrid BM25 + vector) when
``AZURE_SEARCH_ENDPOINT``/``AZURE_SEARCH_KEY`` are configured; the local
in-memory store otherwise, and as the fallback when the service is down.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Tuple

from app.services.vector_store import InMemoryVectorStore

logger = logging.getLogger("retrieval")

# Near-duplicate detection: the corpus carries templated policy variants whose
# wording differs only by a stopword or punctuation (e.g. "Sanctions screening
# mandatory for all counterparties" vs "Sanctions screening is mandatory for
# all counterparties."). Two texts collapse to the same *signature* — the set
# of content tokens with stopwords removed — so retrieval returns one
# representative per distinct policy instead of citing the same rule 3 times.
_SIG_WORD = re.compile(r"[a-z0-9]+")
_SIG_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "be", "been", "for", "of", "to", "in",
    "on", "and", "or", "with", "by", "at", "as", "that", "this", "it", "its",
    "must", "shall", "should", "may", "not", "no", "all", "any", "per",
}


def _dedupe_signature(text: str) -> Tuple[str, ...]:
    """Order-independent content-token signature used to spot near-duplicates."""
    return tuple(sorted({t for t in _SIG_WORD.findall(text.lower()) if t not in _SIG_STOPWORDS}))

_FALLBACK = [
    {"source_id": "POL-AML-01", "excerpt": "Enhanced customer due diligence is required for high-risk profiles."}
]

# Relevance cutoff: drop hits scoring below this fraction of the best hit's
# score. Relative (not absolute) so it works for both cosine scores from the
# in-memory store and RRF-scale scores from Azure AI Search hybrid ranking.
_MIN_RELATIVE_SCORE = 0.5


def _new_vector_store() -> Any:
    """Azure AI Search when configured; local in-memory store otherwise."""
    from app.services.azure_search_store import AzureSearchVectorStore, azure_search_configured

    if azure_search_configured():
        try:
            store = AzureSearchVectorStore()
            logger.info("retrieval: Azure AI Search backend (index=%s)", store.index)
            return store
        except Exception:
            logger.exception("Azure AI Search unavailable; falling back to in-memory vector store")
    return InMemoryVectorStore()


class GraphRAGRetriever:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.vector_store = _new_vector_store()
        self._indexed_node_count = -1

    # ------------------------------------------------------------------ index
    def build_index(self) -> int:
        """(Re)build the vector index from the knowledge graph corpus."""
        nodes = getattr(self.store, "nodes", None) or {}
        items: List[tuple[str, str, Dict[str, Any]]] = []
        if not nodes and hasattr(self.store, "retrieval_corpus"):
            # Backend without an in-memory node map (Neo4j): the store
            # supplies the indexable corpus directly.
            try:
                for entry in self.store.retrieval_corpus():
                    items.append(
                        (entry["source_id"], entry["text"], {"label": entry["label"], "node": entry.get("node", {})})
                    )
            except Exception:
                items = []
            self.vector_store = _new_vector_store()
            self.vector_store.add_batch(items)
            self._indexed_node_count = len(items)
            return len(items)
        for node in nodes.values():
            label = node.get("label", "")
            if label in ("Policy", "Regulation"):
                source_id = node.get("policy_id") or node.get("regulation_id") or ""
                text = " ".join(filter(None, [node.get("title"), node.get("summary")]))
                if source_id and text:
                    items.append((source_id, text, {"label": label, "node": node}))
            elif label == "Evidence":
                source_id = node.get("evidence_id") or ""
                text = node.get("excerpt") or ""
                if source_id and text:
                    items.append((source_id, text, {"label": label, "node": node}))
        self.vector_store = _new_vector_store()
        self.vector_store.add_batch(items)
        self._indexed_node_count = len(nodes)
        return len(items)

    def _ensure_index(self) -> None:
        nodes = getattr(self.store, "nodes", None)
        if nodes is None:
            # Corpus-backed store (Neo4j): build once; chunks added later via
            # index_chunks stay in place.
            if self._indexed_node_count == -1:
                self.build_index()
            return
        if len(nodes) != self._indexed_node_count:
            self.build_index()

    def index_chunks(self, doc_id: str, chunks: List[str], metadata: Dict[str, Any] | None = None) -> None:
        """Add ingested document chunks to the live index."""
        self._ensure_index()
        base_meta = {"label": "DocumentChunk", "doc_id": doc_id, **(metadata or {})}
        self.vector_store.add_batch(
            [(f"{doc_id}#chunk{i}", chunk, {**base_meta, "chunk": i}) for i, chunk in enumerate(chunks)]
        )
        # Chunks aren't graph nodes; remember the count so _ensure_index
        # doesn't rebuild (and drop them) on the next query.
        self._indexed_node_count = len(getattr(self.store, "nodes", None) or {})

    # ---------------------------------------------------------------- retrieve
    def _graph_context(self, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """Follow graph edges from a hit to related entities."""
        related: Dict[str, Any] = {}
        relationships = getattr(self.store, "relationships", None) or []
        node = metadata.get("node") or {}
        if metadata.get("label") == "Evidence":
            evidence_id = node.get("evidence_id")
            docs = [rel["from"] for rel in relationships if rel.get("to") == evidence_id and rel.get("type") == "HAS_EVIDENCE"]
            if docs:
                related["documents"] = docs
        elif metadata.get("label") == "DocumentChunk":
            related["document"] = metadata.get("doc_id")
        elif metadata.get("label") == "Policy":
            policy_id = node.get("policy_id")
            clients = [rel["from"] for rel in relationships if rel.get("to") == policy_id and rel.get("type") == "GOVERNED_BY"]
            if clients:
                related["governed_clients"] = clients[:5]
        return related

    def retrieve(self, query: str, k: int = 3) -> List[Dict[str, Any]]:
        self._ensure_index()
        # Pull a wider pool than k: duplicate variants of the same policy would
        # otherwise fill the top-k and push the canonical rule out of range, so
        # we over-fetch, then collapse duplicates back down to k distinct hits.
        pool = self.vector_store.search(query, k=max(k * 5, 15))
        # Trim the weak tail: keep the top hit, drop hits far below it so
        # off-topic excerpts neither dilute precision nor feed the LLM.
        if pool:
            floor = (pool[0].get("score") or 0.0) * _MIN_RELATIVE_SCORE
            pool = [h for h in pool if (h.get("score") or 0.0) >= floor] or pool[:1]

        # Collapse near-duplicates: one group per distinct policy meaning. The
        # representative is the canonical (lowest) id — the golden "-01" rather
        # than a generated "-09"/"-17" clone — shown at the group's best rank
        # and best score, so the copilot cites each rule once.
        groups: Dict[Tuple[str, ...], Dict[str, Any]] = {}
        for order, hit in enumerate(pool):
            sig = _dedupe_signature(hit["text"])
            group = groups.get(sig)
            if group is None:
                groups[sig] = {"hit": hit, "score": hit.get("score") or 0.0, "order": order}
            else:
                group["score"] = max(group["score"], hit.get("score") or 0.0)
                group["order"] = min(group["order"], order)
                if hit["id"] < group["hit"]["id"]:
                    group["hit"] = hit  # adopt the canonical (lowest) id

        citations: List[Dict[str, Any]] = []
        for group in sorted(groups.values(), key=lambda g: g["order"])[:k]:
            hit = group["hit"]
            citation = {
                "source_id": hit["id"],
                "excerpt": hit["text"][:300],
                "score": round(group["score"], 4),
                "source_type": hit["metadata"].get("label", "Unknown"),
            }
            related = self._graph_context(hit["metadata"])
            if related:
                citation["related"] = related
            citations.append(citation)
        return citations or list(_FALLBACK)


# One retriever per graph store instance (tests build their own stores).
_retrievers: Dict[int, GraphRAGRetriever] = {}


def get_retriever(store: Any) -> GraphRAGRetriever:
    key = id(store)
    retriever = _retrievers.get(key)
    if retriever is None or retriever.store is not store:
        retriever = GraphRAGRetriever(store)
        _retrievers[key] = retriever
    return retriever
