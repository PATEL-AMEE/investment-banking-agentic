"""Client portal tests: the client is a separate, much more limited actor.

- submission triggers the internal onboarding chain,
- the client only ever sees the plain-language status view (no risk scores,
  screening results, citations, or reasoning),
- resource scoping: a client token reads its OWN application only,
- the client role can reach nothing else on the platform.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import app, store
from app.services import rbac
from app.services.local_auth import issue_token

client = TestClient(app)

_INTERNAL_KEYS = {
    "riskScore", "risk_score", "riskTier", "risk_tier", "pepFound", "pep_found",
    "sanctionsCheckResult", "sanctions_check_result", "provenance", "rationale",
    "rule_hits", "regulations", "internal", "reviewTaskId", "review_task_id",
}


def _apply(company: str = "Blue Harbour Ltd", **overrides) -> dict:
    payload = {
        "companyName": company,
        "jurisdiction": "GB",
        "product": "Corporate account",
        "beneficialOwners": [{"name": "Jane Smith", "country": "GB"}],
        "documentsProvided": ["certificate_of_incorporation"],
    }
    payload.update(overrides)
    response = client.post("/api/client/apply", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


# ----------------------------------------------------------------- intake flow
def test_apply_returns_client_safe_view_and_scoped_token():
    view = _apply()
    assert view["applicationId"].startswith("APP-")
    assert view["status"] in {"received", "under_review", "additional_documents_needed", "approved"}
    assert view["message"]
    assert "accessToken" in view
    # No internal pipeline detail leaks to the client.
    assert not (_INTERNAL_KEYS & set(view))


def test_missing_documents_drive_the_checklist():
    view = _apply(company="Docless Ventures Ltd", documentsProvided=[])
    assert view["status"] == "additional_documents_needed"
    assert len(view["outstandingDocuments"]) == 4


def test_flagged_client_lands_under_review_without_leaking_why():
    # Sanctions-listed name: internally this is a sanctions match + review
    # escalation; the client just sees "under review".
    view = _apply(company="Red Star Trading", documentsProvided=[
        "certificate_of_incorporation", "proof_of_registered_address",
        "source_of_funds_declaration", "beneficial_ownership_evidence",
    ])
    assert view["status"] == "under_review"
    lowered = str(view).lower()
    assert "sanction" not in lowered
    assert "pep" not in lowered
    assert "risk" not in lowered


# ------------------------------------------------------------ status + scoping
def test_status_readable_with_own_token_only():
    view = _apply(company="Own Records Ltd")
    headers = {"Authorization": f"Bearer {view['accessToken']}"}
    status = client.get(f"/api/client/status/{view['applicationId']}", headers=headers)
    assert status.status_code == 200
    assert not (_INTERNAL_KEYS & set(status.json()))

    # A different client's token is denied on this application.
    other = issue_token("client:Other Co", ["client"], extra_claims={"client_id": "CLIENT-OTHER-CO"})
    denied = client.get(
        f"/api/client/status/{view['applicationId']}",
        headers={"Authorization": f"Bearer {other}"},
    )
    assert denied.status_code == 403


def test_client_role_cannot_reach_internal_endpoints():
    view = _apply(company="Curious Client Ltd")
    headers = {"Authorization": f"Bearer {view['accessToken']}"}
    assert client.post("/api/agents/inspect", json={"clientId": "C123", "txData": {}}, headers=headers).status_code == 403
    assert client.post("/api/copilot/query", json={"query": "aml policy"}, headers=headers).status_code == 403
    assert client.get("/api/reviews/pending", headers=headers).status_code == 403
    assert client.get("/api/audit/logs", headers=headers).status_code == 403
    assert client.get("/api/dashboard/summary", headers=headers).status_code == 403


def test_client_role_permissions_are_narrow():
    perms = rbac.permissions_for(["client"])
    assert perms == {rbac.PERM_APPLICATION_SUBMIT, rbac.PERM_APPLICATION_STATUS}


# -------------------------------------------------- review resolution updates
def test_review_resolution_flows_through_to_client_status():
    view = _apply(
        company="Volkov Family Office",
        beneficialOwners=[{"name": "Oleg Volkov", "country": "RU"}],
        documentsProvided=[
            "certificate_of_incorporation", "proof_of_registered_address",
            "source_of_funds_declaration", "beneficial_ownership_evidence",
        ],
    )
    assert view["status"] == "under_review"

    # Find the review this application escalated to and approve it as staff.
    from app.services import applications

    record = applications.get_application(view["applicationId"])
    review_id = record["internal"]["review_task_id"]
    assert review_id
    staff = issue_token("risk.mgr", ["risk_manager"])
    resolved = client.post(
        f"/api/reviews/{review_id}/resolve",
        json={"decision": "approve", "reviewer": "risk.mgr"},
        headers={"Authorization": f"Bearer {staff}"},
    )
    assert resolved.status_code == 200

    headers = {"Authorization": f"Bearer {view['accessToken']}"}
    status = client.get(f"/api/client/status/{view['applicationId']}", headers=headers).json()
    assert status["status"] == "approved"
