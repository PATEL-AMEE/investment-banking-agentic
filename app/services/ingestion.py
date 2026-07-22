from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from app.services.graph_store import GraphStore

_TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".log", ".html", ".xml"}


def seed_demo_data(store: GraphStore, data_dir: Path | None = None) -> None:
    store.load_demo_data(data_dir or Path("data"))


def extract_text(file_path: str | Path) -> str:
    """Extract raw text from an uploaded document (plain text or PDF)."""
    path = Path(file_path)
    if not path.exists():
        return ""
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        try:
            return path.read_text(encoding="utf-8-sig", errors="ignore")
        except Exception:
            return ""
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception:
            return ""
    return ""


def chunk_text(text: str, chunk_size: int = 800, overlap: int = 120) -> List[str]:
    """Split text into overlapping chunks on whitespace boundaries."""
    text = " ".join(text.split())
    if not text:
        return []
    if len(text) <= chunk_size:
        return [text]
    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = start + chunk_size
        if end < len(text):
            boundary = text.rfind(" ", start, end)
            if boundary > start:
                end = boundary
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return [chunk for chunk in chunks if chunk]


def ingest_document(file_path: str, metadata: Dict[str, Any] | None = None, store: GraphStore | None = None) -> Dict[str, Any]:
    path = Path(file_path)
    content = path.read_bytes() if path.exists() else b"sample"
    digest = hashlib.sha256(content).hexdigest()
    doc_id = f"D-{digest[:8]}"
    meta = metadata or {}
    source = meta.get("source", "upload")
    upload_date = meta.get("upload_date", date.today().isoformat())
    client_id = meta.get("client_id")

    # Real pipeline steps: text extraction + chunking for retrieval.
    # PII is masked before chunks ever reach the search index (DLP).
    from app.services.dlp import mask_pii

    text = extract_text(path)
    masked_text, pii_found = mask_pii(text)
    chunks = chunk_text(masked_text)

    document = {
        "doc_id": doc_id,
        "status": "accepted",
        "sha256": digest,
        "metadata": meta,
        "chunk_count": len(chunks),
        "pii_masked": pii_found,
    }

    if store is not None:
        evidence_ids = meta.get("evidence_ids")
        if isinstance(evidence_ids, str):
            try:
                evidence_ids = json.loads(evidence_ids)
            except Exception:
                evidence_ids = [evidence_ids]
        if evidence_ids is None:
            evidence_ids = []
        try:
            store.add_document(
                doc_id,
                source,
                upload_date,
                digest,
                metadata=meta,
                client_id=client_id,
                evidence_ids=evidence_ids,
            )
        except AttributeError:
            # fallback if store does not support document persistence
            pass

        if chunks:
            # Index chunks into the store's GraphRAG retriever so uploaded
            # documents become searchable by the copilot immediately.
            try:
                from app.services.retrieval import get_retriever

                get_retriever(store).index_chunks(doc_id, chunks, metadata={"client_id": client_id, "source": source})
            except Exception:
                pass

    return document
