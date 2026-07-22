from app.api import KYCRequest, copilot_query, get_audit_logs, onboarding_kyc

USER = {"sub": "test-user"}


def test_onboarding_kyc_endpoint_escalates_pep():
    resp = onboarding_kyc(
        KYCRequest(
            clientName="Globex Ltd",
            jurisdiction="US",
            beneficialOwners=[{"full_name": "Jane Doe", "pep_status": "pep_usa_congress"}],
            clientId="CLIENT-XYZ",
            requestId="REQ-API-001",
        ),
        current_user=USER,
    )
    assert resp.status == "PENDING_REVIEW"
    assert resp.pepFound is True
    assert resp.reviewTaskId


def test_audit_logs_endpoint_returns_recorded_events():
    onboarding_kyc(
        KYCRequest(clientName="Clean Corp", jurisdiction="GB", requestId="REQ-API-002"),
        current_user=USER,
    )
    body = get_audit_logs(request_id="REQ-API-002", current_user=USER)
    assert body["count"] >= 1
    assert body["audit_events"][0]["request_id"] == "REQ-API-002"


def test_copilot_query_returns_relevant_citation():
    body = copilot_query({"query": "suspicious activity reporting requirements"}, current_user=USER)
    assert body["citations"]
    assert "source_id" in body["citations"][0]
