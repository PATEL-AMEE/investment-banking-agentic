from pathlib import Path

from app.services.audit import AuditLog
from app.services.graph_store import GraphStore
from app.services.ingestion import seed_demo_data
from app.services.onboarding import run_onboarding_workflow
from app.services.sanctions import sanctions_check


def _store() -> GraphStore:
    store = GraphStore(data_dir=Path("data"))
    seed_demo_data(store, data_dir=Path("data"))
    return store


def test_low_risk_client_is_auto_approved():
    result = run_onboarding_workflow(
        client_id="CLIENT-CLEAN",
        client_name="Clean Corp",
        jurisdiction="GB",
        beneficial_owners=[{"full_name": "Alice Honest", "pep_status": "non_pep"}],
        request_id="REQ-KYC-001",
        user_id="U-001",
        store=_store(),
    )

    assert result["status"] == "APPROVED"
    assert result["review_required"] is False
    assert result["pep_found"] is False
    assert result["sanctions_check_result"] == "no_match"


def test_pep_owner_triggers_escalation_and_review_task():
    store = _store()
    result = run_onboarding_workflow(
        client_id="CLIENT-PEP",
        client_name="Globex Ltd",
        jurisdiction="US",
        beneficial_owners=[{"full_name": "Jane Doe", "pep_status": "pep_usa_congress"}],
        request_id="REQ-KYC-002",
        user_id="U-002",
        store=store,
    )

    assert result["status"] == "PENDING_REVIEW"
    assert result["pep_found"] is True
    assert result["review_task_id"]
    assert any(t["review_id"] == result["review_task_id"] for t in store.list_pending_reviews())


def test_sanctions_match_forces_review_and_high_risk():
    result = run_onboarding_workflow(
        client_id="CLIENT-BAD",
        client_name="Sanctioned Holdings Ltd",
        jurisdiction="IR",
        beneficial_owners=[],
        request_id="REQ-KYC-003",
        user_id="U-003",
        store=_store(),
    )

    assert result["sanctions_check_result"] == "match"
    assert result["review_required"] is True
    assert result["risk_score"] >= 5.0


def test_regulations_are_retrieved_by_jurisdiction():
    result = run_onboarding_workflow(
        client_id="CLIENT-GB",
        client_name="Acme Corp",
        jurisdiction="GB",
        beneficial_owners=[],
        request_id="REQ-KYC-004",
        user_id="U-004",
        store=_store(),
    )
    # GB and EU-wide regulations from the demo dataset should be retrieved.
    assert result["retrieved_regulations"]
    assert result["provenance"]


def test_audit_log_records_onboarding_event():
    audit = AuditLog()
    run_onboarding_workflow(
        client_id="CLIENT-AUDIT",
        client_name="Clean Corp",
        jurisdiction="GB",
        beneficial_owners=[],
        request_id="REQ-KYC-005",
        user_id="U-005",
        store=_store(),
        audit_log=audit,
    )
    events = audit.list("REQ-KYC-005")
    assert len(events) == 1
    assert events[0]["event_type"] == "onboarding_assessment"


def test_sanctions_check_clean_name():
    result = sanctions_check("Totally Clean Inc", "GB", [])
    assert result["is_sanctioned"] is False
    assert result["high_risk_jurisdiction"] is False
