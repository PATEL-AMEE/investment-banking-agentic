from __future__ import annotations

from pathlib import Path

from app.services.graph_store import GraphStore
from app.services.ingestion import ingest_document


def test_ingest_document_creates_client_and_evidence_relationships(tmp_path: Path) -> None:
    store = GraphStore(data_dir=tmp_path)
    store.load_demo_data(Path("data"))

    ingest_document(
        file_path=str(tmp_path / "sample.txt"),
        metadata={
            "client_id": "C123",
            "source": "upload",
            "upload_date": "2026-07-21",
            "evidence_ids": ["E-1", "E-2"],
        },
        store=store,
    )

    assert any(node.get("label") == "Document" for node in store.nodes.values())
    assert any(rel for rel in store.relationships if rel["from"] == "C123" and rel["type"] == "CREATED_FROM")
    assert any(rel for rel in store.relationships if rel["from"] == "E-1" and rel["type"] == "SUPPORTS")
