from __future__ import annotations

from pathlib import Path

from app.services.graph_store import GraphStore
from app.services.ingestion import ingest_document


def test_ingest_document_persists_to_graph_store(tmp_path: Path) -> None:
    store = GraphStore(data_dir=tmp_path)
    ingest_document(
        file_path=str(tmp_path / "sample.txt"),
        metadata={"client_id": "C123", "source": "upload", "upload_date": "2026-07-21"},
        store=store,
    )
    assert any(node.get("doc_id") for node in store.nodes.values() if node.get("label") == "Document")
    assert any(rel for rel in store.relationships if rel["from"] == "C123" and rel["type"] == "CREATED_FROM")
