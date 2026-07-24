"""Phase 6.6 (Copilot feedback loop) + Phase 6b.5 (client status notifications).

- staff can flag a Copilot answer; a "wrong" flag becomes an eval regression case,
- clients are emailed (dev outbox) on every status transition, client-safe only,
- the client-portal token cannot submit staff feedback (app boundary holds).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import app, store
from app.services import feedback, notifications
from app.services.local_auth import issue_client_token, issue_staff_token

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clean_state():
    feedback.clear()
    notifications.clear_outbox()
    yield


# ------------------------------------------------------------ Copilot feedback
def test_staff_can_flag_answer_wrong_and_it_feeds_the_eval_loop():
    staff = {"Authorization": f"Bearer {issue_staff_token('ana', ['compliance_analyst'])}"}
    resp = client.post(
        "/api/copilot/feedback",
        json={"query": "What does the AML policy require?", "answer": "Nonsense.",
              "verdict": "wrong", "comment": "not grounded"},
        headers=staff,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "wrong"
    assert body["wrong"] == 1 and body["flagged_questions"] == 1

    # The flagged question is now a regression case the harness re-scores.
    from app.eval.harness import run_feedback_regression

    report = run_feedback_regression(store)
    assert report["flagged_count"] == 1
    assert report["cases"][0]["question"] == "What does the AML policy require?"
    assert "faithfulness" in report["cases"][0]["metrics"]


def test_invalid_verdict_is_rejected():
    staff = {"Authorization": f"Bearer {issue_staff_token('ana', ['risk_manager'])}"}
    resp = client.post("/api/copilot/feedback", json={"query": "q", "verdict": "meh"}, headers=staff)
    assert resp.status_code == 422


def test_client_portal_token_cannot_submit_staff_feedback():
    tok = issue_client_token("client:Walled", extra_claims={"client_id": "CLIENT-WALLED"})
    resp = client.post(
        "/api/copilot/feedback",
        json={"query": "q", "verdict": "wrong"},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert resp.status_code == 403


# -------------------------------------------------------- client notifications
def _apply(company, **overrides):
    payload = {
        "companyName": company, "jurisdiction": "GB", "product": "Corporate account",
        "beneficialOwners": [{"name": "Jane Smith", "country": "GB"}],
        "documentsProvided": ["certificate_of_incorporation"],
    }
    payload.update(overrides)
    r = client.post("/api/client/apply", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def test_apply_with_email_sends_a_client_safe_notification():
    _apply("Notify Co", email="ceo@notify.co", password="pw-123456", documentsProvided=[])
    box = [m for m in notifications.outbox() if m["to"] == "ceo@notify.co"]
    assert len(box) == 1
    msg = box[0]
    assert msg["status"] == "additional_documents_needed"
    # No internal detail ever reaches the client channel.
    blob = (msg["subject"] + msg["body"]).lower()
    assert "risk" not in blob and "sanction" not in blob and "pep" not in blob


def test_no_email_means_no_notification():
    _apply("Silent Co")  # no email supplied
    assert notifications.outbox() == []


def test_decision_triggers_a_notification_on_status_change():
    view = _apply(
        "Decision Co", email="cfo@decision.co", password="pw-123456",
        documentsProvided=[
            "certificate_of_incorporation", "proof_of_registered_address",
            "source_of_funds_declaration", "beneficial_ownership_evidence",
        ],
    )
    notifications.clear_outbox()  # drop the initial "under review" notice

    from app.services import applications

    record = applications.get_application(view["applicationId"])
    review_id = record["internal"]["review_task_id"]
    staff = {"Authorization": f"Bearer {issue_staff_token('mgr', ['risk_manager'])}"}
    resolved = client.post(
        f"/api/reviews/{review_id}/resolve",
        json={"decision": "approve", "reviewer": "mgr"},
        headers=staff,
    )
    assert resolved.status_code == 200
    box = [m for m in notifications.outbox() if m["to"] == "cfo@decision.co"]
    assert len(box) == 1 and box[0]["status"] == "approved"
