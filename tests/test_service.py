from app.service import copilot_query, inspect_client


def test_inspect_client_returns_review_for_high_risk_client():
    response = inspect_client({
        "clientId": "C456",
        "txData": {"amount": 2500000},
        "requestId": "req-100",
        "userId": "user-100",
        "sessionId": "sess-100",
    })

    assert response["decision"] == "warn"
    assert response["reviewRequired"] is True
    assert response["confidence"] >= 0.7


def test_copilot_query_returns_citation():
    response = copilot_query({
        "query": "What is required for high-risk clients?",
        "requestId": "req-200",
        "userId": "user-200",
    })

    assert "Answer" in response["answer"]
    assert response["citations"][0]["source_id"] == "POL-AML-01"
