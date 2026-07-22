from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from app.services.neo4j_store import Neo4jStore


@pytest.mark.skipif(not os.getenv("TEST_NEO4J"), reason="NEO4J integration test disabled")
def test_neo4j_store_loads_demo_data_and_persists_reviews() -> None:
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")
    password = os.getenv("NEO4J_PASSWORD", "test")
    store = Neo4jStore(uri, user, password)
    try:
        store.load_demo_data(Path("data"))
        client = store.get_client("C123")
        assert client is not None
        assert client.get("name") == "Acme Corp"

        documents = store.get_related_documents("C123")
        assert any(doc.get("doc_id") == "D-0001" for doc in documents)

        evidence = store.get_evidence("D-0001")
        assert any(ev.get("doc_id") == "D-0001" for ev in evidence)

        review_id = f"REV-{uuid.uuid4().hex[:8]}"
        review = store.add_review(review_id, "C123", "test review", "low", "user-test")
        assert review["status"] == "pending"

        pending = store.list_pending_reviews()
        assert any(r.get("review_id") == review_id for r in pending)

        resolved = store.resolve_review(review_id, "approved", "tester", "done")
        assert resolved["status"] == "resolved"
        assert resolved["review_id"] == review_id
    finally:
        store.close()
