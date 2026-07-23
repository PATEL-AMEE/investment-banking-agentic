"""Azure AI Search vector backend tests — fully offline via a fake REST layer."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from app.services.azure_search_store import AzureSearchVectorStore, _safe_key, azure_search_configured
from app.services.retrieval import GraphRAGRetriever, _new_vector_store
from app.services.vector_store import InMemoryVectorStore


class FakeRest:
    """Records every REST call; optionally fails selected paths."""

    def __init__(self, search_response: Optional[Dict[str, Any]] = None, fail_on: str = "") -> None:
        self.calls: List[tuple[str, str, Any]] = []
        self.search_response = search_response or {"value": []}
        self.fail_on = fail_on

    def __call__(self, method: str, path: str, payload: Any = None) -> Dict[str, Any]:
        if self.fail_on and self.fail_on in path:
            raise ConnectionError(f"simulated outage on {path}")
        self.calls.append((method, path, payload))
        if path.endswith("/docs/search"):
            return self.search_response
        return {}


@pytest.fixture
def azure_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_SEARCH_ENDPOINT", "https://unit-test.search.windows.net")
    monkeypatch.setenv("AZURE_SEARCH_KEY", "test-key")
    monkeypatch.setenv("AZURE_SEARCH_INDEX", "policy-corpus-test")


# ------------------------------------------------------------- configuration
def test_not_configured_by_default():
    assert azure_search_configured() is False
    assert isinstance(_new_vector_store(), InMemoryVectorStore)


def test_configured_env_selects_azure_backend(azure_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(AzureSearchVectorStore, "_call", FakeRest())
    assert azure_search_configured() is True
    assert isinstance(_new_vector_store(), AzureSearchVectorStore)


def test_unreachable_service_falls_back_to_in_memory(azure_env, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(AzureSearchVectorStore, "_call", FakeRest(fail_on="/indexes/"))
    assert isinstance(_new_vector_store(), InMemoryVectorStore)


# ------------------------------------------------------------- index + write
def test_index_created_and_documents_uploaded(azure_env, monkeypatch: pytest.MonkeyPatch):
    fake = FakeRest()
    monkeypatch.setattr(AzureSearchVectorStore, "_call", fake)
    store = AzureSearchVectorStore()
    store.add_batch([("POL-AML-01", "Enhanced due diligence text", {"label": "Policy"})])

    methods_paths = [(method, path) for method, path, _ in fake.calls]
    assert ("PUT", "/indexes/policy-corpus-test") in methods_paths
    upload = next(payload for _, path, payload in fake.calls if path.endswith("/docs/index"))
    document = upload["value"][0]
    assert document["@search.action"] == "mergeOrUpload"
    assert document["source_id"] == "POL-AML-01"
    assert document["label"] == "Policy"
    assert len(document["embedding"]) == store.embedder.dim


def test_safe_key_sanitises_invalid_characters():
    key = _safe_key("DOC-1#chunk0")
    assert "#" not in key
    assert key != _safe_key("DOC-1_chunk0")  # CRC suffix avoids collisions


# ------------------------------------------------------------------- search
def test_search_maps_hits_and_round_trips_metadata(azure_env, monkeypatch: pytest.MonkeyPatch):
    fake = FakeRest(
        search_response={
            "value": [
                {
                    "@search.score": 0.031,
                    "id": _safe_key("EV-001"),
                    "source_id": "EV-001",
                    "text": "Evidence excerpt",
                    "metadata_json": '{"label": "Evidence", "node": {"evidence_id": "EV-001"}}',
                }
            ]
        }
    )
    monkeypatch.setattr(AzureSearchVectorStore, "_call", fake)
    store = AzureSearchVectorStore()
    hits = store.search("due diligence", k=3)
    assert hits[0]["id"] == "EV-001"
    assert hits[0]["metadata"]["label"] == "Evidence"
    assert hits[0]["metadata"]["node"]["evidence_id"] == "EV-001"


def test_search_outage_serves_from_local_shadow(azure_env, monkeypatch: pytest.MonkeyPatch):
    fake = FakeRest(fail_on="/docs/search")
    monkeypatch.setattr(AzureSearchVectorStore, "_call", fake)
    store = AzureSearchVectorStore()
    store.add_batch([("POL-AML-01", "Enhanced customer due diligence is required", {"label": "Policy"})])
    hits = store.search("due diligence")
    assert hits and hits[0]["id"] == "POL-AML-01"  # shadow answered


# ------------------------------------------------- retrieval integration
class _FakeGraphStore:
    """Minimal graph store: one Policy node, no relationships."""

    def __init__(self) -> None:
        self.nodes = {
            "p1": {
                "label": "Policy",
                "policy_id": "POL-AML-01",
                "title": "AML Policy",
                "summary": "Enhanced customer due diligence is required for high-risk profiles.",
            }
        }
        self.relationships: List[Dict[str, Any]] = []


def test_graphrag_retriever_uses_azure_backend(azure_env, monkeypatch: pytest.MonkeyPatch):
    graph_store = _FakeGraphStore()
    fake = FakeRest(
        search_response={
            "value": [
                {
                    "@search.score": 0.03,
                    "source_id": "POL-AML-01",
                    "text": "Enhanced customer due diligence is required for high-risk profiles.",
                    "metadata_json": '{"label": "Policy", "node": {"policy_id": "POL-AML-01"}}',
                }
            ]
        }
    )
    monkeypatch.setattr(AzureSearchVectorStore, "_call", fake)
    retriever = GraphRAGRetriever(graph_store)
    citations = retriever.retrieve("enhanced due diligence")
    assert isinstance(retriever.vector_store, AzureSearchVectorStore)
    assert citations[0]["source_id"] == "POL-AML-01"
    assert citations[0]["source_type"] == "Policy"
    # The corpus was uploaded to the index during build_index.
    assert any(path.endswith("/docs/index") for _, path, _ in fake.calls)
