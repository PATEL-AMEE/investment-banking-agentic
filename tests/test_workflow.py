from pathlib import Path

from app.services.workflow import run_inspection_workflow
from app.services.graph_store import GraphStore
from app.services.ingestion import seed_demo_data


def test_run_inspection_workflow_returns_decision_with_provenance(tmp_path: Path):
    store = GraphStore(data_dir=tmp_path)
    seed_demo_data(store, data_dir=Path("data"))

    result = run_inspection_workflow(
        client_id="C123",
        tx_data={"amount": 250000, "currency": "GBP"},
        request_id="REQ-001",
        user_id="U-001",
        session_id="S-001",
        workflow_step="compliance-review",
        store=store,
    )

    assert result["decision"] in {"pass", "warn", "fail"}
    assert 0.0 <= result["confidence"] <= 1.0
    assert result["provenance"]
    assert result["rule_hits"]


def test_run_inspection_workflow_creates_review_task_for_high_risk_client(tmp_path: Path):
    store = GraphStore(data_dir=tmp_path)
    seed_demo_data(store, data_dir=Path("data"))

    result = run_inspection_workflow(
        client_id="C456",
        tx_data={"amount": 650000, "currency": "USD"},
        request_id="REQ-002",
        user_id="U-002",
        session_id="S-002",
        workflow_step="human-review",
        store=store,
    )

    assert result["reviewRequired"] is True
    assert result["reviewTaskId"]
    assert any(task["review_id"] == result["reviewTaskId"] for task in store.list_pending_reviews())


def test_seed_demo_data_loads_rich_dataset(tmp_path: Path):
    store = GraphStore(data_dir=tmp_path)
    seed_demo_data(store, data_dir=Path("data"))

    assert len(store.nodes) >= 15
    assert any(node.get("label") == "Policy" for node in store.nodes.values())
    assert any(node.get("label") == "Regulation" for node in store.nodes.values())
