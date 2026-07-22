from __future__ import annotations

from typing import Any, Dict

from app.graph import run_inspection_workflow
from app.models import AgentState


def inspect_client(payload: Dict[str, Any]) -> Dict[str, Any]:
    state = AgentState(
        request_id=payload.get("requestId", "req-001"),
        user_id=payload.get("userId", "user-001"),
        session_id=payload.get("sessionId"),
        client_id=payload.get("clientId"),
        tx_data=payload.get("txData", {}),
    )
    state = run_inspection_workflow(state)

    return {
        "decision": state.decision,
        "decisionCode": state.decision.upper(),
        "confidence": state.confidence,
        "rationale": state.rationale,
        "provenance": state.provenance,
        "reviewRequired": state.review_required,
        "requestId": state.request_id,
        "sessionId": state.session_id,
    }


def copilot_query(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "answer": f"Answer for query: {payload.get('query', '')}",
        "citations": [{"source_id": "POL-AML-01", "excerpt": "Enhanced due diligence is required for high-risk clients."}],
        "requestId": payload.get("requestId", "req-001"),
    }
