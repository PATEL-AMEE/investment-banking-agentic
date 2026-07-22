from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Dict

from app.services.graph_store import GraphStore


def seed_demo_data(store: GraphStore, data_dir: Path | None = None) -> None:
    store.load_demo_data(data_dir or Path("data"))


def ingest_document(file_path: str, metadata: Dict[str, Any] | None = None, store: GraphStore | None = None) -> Dict[str, Any]:
    path = Path(file_path)
    content = path.read_bytes() if path.exists() else b"sample"
    digest = hashlib.sha256(content).hexdigest()
    doc_id = f"D-{digest[:8]}"
    meta = metadata or {}
    source = meta.get("source", "upload")
    upload_date = meta.get("upload_date", date.today().isoformat())
    client_id = meta.get("client_id")

    document = {
        "doc_id": doc_id,
        "status": "accepted",
        "sha256": digest,
        "metadata": meta,
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

    return document
