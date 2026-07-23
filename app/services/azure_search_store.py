"""Azure AI Search vector store backend.

Same contract as :class:`app.services.vector_store.InMemoryVectorStore`
(``add_batch`` / ``search`` / ``len``), backed by an Azure AI Search index
with **hybrid retrieval**: BM25 keyword search fused (RRF) with vector
similarity over embeddings computed client-side by the platform's embedder.

Configuration (see the AKS guide §4):

- ``AZURE_SEARCH_ENDPOINT`` — e.g. ``https://<name>.search.windows.net``
- ``AZURE_SEARCH_KEY``      — admin key (index create + upload + query)
- ``AZURE_SEARCH_INDEX``    — index name (default ``policy-corpus``)

The index is created on first use (create-or-update, idempotent). Every
document is *also* written to a local in-memory shadow index, so a search
outage degrades to local retrieval instead of empty citations — the same
"failures never propagate" stance as the LLM adapter.
"""
from __future__ import annotations

import json
import logging
import os
import re
import zlib
from typing import Any, Dict, List

import requests

from app.services.vector_store import Embedder, HashedTfEmbedder, InMemoryVectorStore

logger = logging.getLogger("azure_search")

_TIMEOUT_SECONDS = 30
# Azure AI Search document keys allow only letters, digits, _ - =
_KEY_UNSAFE = re.compile(r"[^a-zA-Z0-9_\-=]")


def azure_search_configured() -> bool:
    return bool(os.getenv("AZURE_SEARCH_ENDPOINT") and os.getenv("AZURE_SEARCH_KEY"))


def _safe_key(doc_id: str) -> str:
    """Sanitise an id into a valid search key, collision-proofed by CRC32."""
    return f"{_KEY_UNSAFE.sub('_', doc_id)}-{zlib.crc32(doc_id.encode('utf-8')):08x}"


class AzureSearchVectorStore:
    """Hybrid (BM25 + vector) index on Azure AI Search with a local shadow."""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder = embedder or HashedTfEmbedder()
        self.endpoint = os.getenv("AZURE_SEARCH_ENDPOINT", "").rstrip("/")
        self.key = os.getenv("AZURE_SEARCH_KEY", "")
        self.index = os.getenv("AZURE_SEARCH_INDEX", "policy-corpus")
        self.api_version = os.getenv("AZURE_SEARCH_API_VERSION", "2024-07-01")
        if not (self.endpoint and self.key):
            raise RuntimeError("AZURE_SEARCH_ENDPOINT and AZURE_SEARCH_KEY are required")
        # Shadow copy: searches fall back here if the service is unreachable.
        self._shadow = InMemoryVectorStore(embedder=self.embedder)
        self._ensure_index()

    def __len__(self) -> int:
        return len(self._shadow)

    # ------------------------------------------------------------------- REST
    def _call(self, method: str, path: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
        url = f"{self.endpoint}{path}?api-version={self.api_version}"
        response = requests.request(
            method, url, json=payload, headers={"api-key": self.key}, timeout=_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def _ensure_index(self) -> None:
        """Create or update the index (PUT is idempotent for a fixed schema)."""
        self._call(
            "PUT",
            f"/indexes/{self.index}",
            {
                "name": self.index,
                "fields": [
                    {"name": "id", "type": "Edm.String", "key": True, "filterable": True},
                    {"name": "source_id", "type": "Edm.String", "filterable": True},
                    {"name": "text", "type": "Edm.String", "searchable": True},
                    {"name": "label", "type": "Edm.String", "filterable": True, "facetable": True},
                    {"name": "metadata_json", "type": "Edm.String", "searchable": False},
                    {
                        "name": "embedding",
                        "type": "Collection(Edm.Single)",
                        "searchable": True,
                        "dimensions": self.embedder.dim,
                        "vectorSearchProfile": "hnsw-profile",
                    },
                ],
                "vectorSearch": {
                    "algorithms": [{"name": "hnsw", "kind": "hnsw"}],
                    "profiles": [{"name": "hnsw-profile", "algorithm": "hnsw"}],
                },
            },
        )

    # ------------------------------------------------------------------ write
    def add(self, doc_id: str, text: str, metadata: Dict[str, Any] | None = None) -> None:
        self.add_batch([(doc_id, text, metadata or {})])

    def add_batch(self, items: List[tuple[str, str, Dict[str, Any]]]) -> None:
        if not items:
            return
        self._shadow.add_batch(items)
        vectors = self.embedder.embed([text for _, text, _ in items])
        documents = [
            {
                "@search.action": "mergeOrUpload",
                "id": _safe_key(doc_id),
                "source_id": doc_id,
                "text": text,
                "label": str(metadata.get("label", "")),
                "metadata_json": json.dumps(metadata, default=str),
                "embedding": [float(value) for value in vector],
            }
            for (doc_id, text, metadata), vector in zip(items, vectors)
        ]
        try:
            self._call("POST", f"/indexes/{self.index}/docs/index", {"value": documents})
        except Exception:
            # Shadow already holds the documents; queries stay grounded.
            logger.exception("Azure AI Search upload failed for %d documents", len(documents))

    # ----------------------------------------------------------------- search
    def search(self, query: str, k: int = 3, min_score: float = 0.0) -> List[Dict[str, Any]]:
        if not query.strip():
            return []
        try:
            vector = [float(value) for value in self.embedder.embed([query])[0]]
            body = self._call(
                "POST",
                f"/indexes/{self.index}/docs/search",
                {
                    "search": query,
                    "top": k,
                    "select": "id,source_id,text,metadata_json",
                    "vectorQueries": [{"kind": "vector", "vector": vector, "fields": "embedding", "k": k}],
                },
            )
        except Exception:
            logger.exception("Azure AI Search query failed; serving from local shadow index")
            return self._shadow.search(query, k=k)
        results: List[Dict[str, Any]] = []
        for hit in body.get("value", [])[:k]:
            try:
                metadata = json.loads(hit.get("metadata_json") or "{}")
            except json.JSONDecodeError:
                metadata = {}
            results.append(
                {
                    "id": hit.get("source_id") or hit.get("id", ""),
                    # Hybrid RRF scores are not cosine similarities; report
                    # them as-is (min_score thresholding is a local concern).
                    "score": round(float(hit.get("@search.score", 0.0)), 4),
                    "text": hit.get("text", ""),
                    "metadata": metadata,
                }
            )
        return results
