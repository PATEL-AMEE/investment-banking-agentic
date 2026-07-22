"""Vector store with pluggable embedders, behind an adapter interface.

Local default: a deterministic hashed-TF embedder (no external model, no
network) feeding an in-memory cosine-similarity index. The interface is
deliberately shaped so Azure OpenAI embeddings + Azure AI Search can be
slotted in (Phase 4/5) without touching retrieval or agent code.
"""
from __future__ import annotations

import math
import re
import zlib
from typing import Any, Dict, List, Protocol

import numpy as np

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class Embedder(Protocol):
    dim: int

    def embed(self, texts: List[str]) -> np.ndarray:  # (n, dim) L2-normalised
        ...


class HashedTfEmbedder:
    """Deterministic local embedder: hashed bag-of-words, log-TF weighted.

    Uses CRC32 bucket hashing (stable across processes, unlike ``hash()``)
    over unigrams and bigrams, then L2-normalises so cosine similarity is a
    dot product. Swap for ``AzureOpenAIEmbedder`` when Azure is configured.
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    @staticmethod
    def _tokens(text: str) -> List[str]:
        unigrams = _TOKEN_RE.findall(text.lower())
        bigrams = [f"{a}_{b}" for a, b in zip(unigrams, unigrams[1:])]
        return unigrams + bigrams

    def embed(self, texts: List[str]) -> np.ndarray:
        matrix = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            counts: Dict[int, int] = {}
            for token in self._tokens(text):
                bucket = zlib.crc32(token.encode("utf-8")) % self.dim
                counts[bucket] = counts.get(bucket, 0) + 1
            for bucket, count in counts.items():
                matrix[row, bucket] = 1.0 + math.log(count)
            norm = np.linalg.norm(matrix[row])
            if norm > 0:
                matrix[row] /= norm
        return matrix


class InMemoryVectorStore:
    """Cosine-similarity index over embedded texts (the local vector store)."""

    def __init__(self, embedder: Embedder | None = None) -> None:
        self.embedder = embedder or HashedTfEmbedder()
        self._ids: List[str] = []
        self._texts: List[str] = []
        self._metadata: List[Dict[str, Any]] = []
        self._matrix: np.ndarray | None = None

    def __len__(self) -> int:
        return len(self._ids)

    def add(self, doc_id: str, text: str, metadata: Dict[str, Any] | None = None) -> None:
        vector = self.embedder.embed([text])
        self._ids.append(doc_id)
        self._texts.append(text)
        self._metadata.append(metadata or {})
        self._matrix = vector if self._matrix is None else np.vstack([self._matrix, vector])

    def add_batch(self, items: List[tuple[str, str, Dict[str, Any]]]) -> None:
        if not items:
            return
        vectors = self.embedder.embed([text for _, text, _ in items])
        for doc_id, text, metadata in items:
            self._ids.append(doc_id)
            self._texts.append(text)
            self._metadata.append(metadata)
        self._matrix = vectors if self._matrix is None else np.vstack([self._matrix, vectors])

    def search(self, query: str, k: int = 3, min_score: float = 0.05) -> List[Dict[str, Any]]:
        if self._matrix is None or not query.strip():
            return []
        query_vec = self.embedder.embed([query])[0]
        scores = self._matrix @ query_vec
        order = np.argsort(scores)[::-1][:k]
        results = []
        for idx in order:
            score = float(scores[idx])
            if score < min_score:
                continue
            results.append(
                {
                    "id": self._ids[idx],
                    "score": round(score, 4),
                    "text": self._texts[idx],
                    "metadata": self._metadata[idx],
                }
            )
        return results
