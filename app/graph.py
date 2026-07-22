from __future__ import annotations

from typing import Any, Dict, List

from app.models import AgentState, AuditEvent


def _mock_rule_evaluator(state: AgentState) -> Dict[str, Any]:
    risk_score = 0.42
    if state.client_id == "C456":
        risk_score = 0.78

    decision = "pass"
    review_required = False
    rationale = "Mock rule evaluation completed successfully."

    if risk_score >= 0.7:
        decision = "warn"
        review_required = True
        rationale = "High-risk profile requires human review."

    return {
        "decision": decision,
        "confidence": 0.88 if decision == "pass" else 0.76,
        "rationale": rationale,
        "review_required": review_required,
        "risk_score": risk_score,
    }


def _mock_retriever(state: AgentState) -> List[Dict[str, Any]]:
    return [
        {
            "source_id": "POL-AML-01",
            "source_type": "policy",
            "excerpt": "Enhanced due diligence is required for high-risk clients.",
            "rank": 1,
        }
    ]


def _mock_llm(state: AgentState) -> Dict[str, Any]:
    rule_result = _mock_rule_evaluator(state)
    return {
        "decision": rule_result["decision"],
        "confidence": rule_result["confidence"],
        "rationale": rule_result["rationale"],
        "review_required": rule_result["review_required"],
        "provenance": _mock_retriever(state),
    }


def run_inspection_workflow(state: AgentState) -> AgentState:
    state.tool_calls.append({"tool": "mock_retriever", "status": "ok"})
    state.retrieved_context = _mock_retriever(state)

    state.tool_calls.append({"tool": "mock_rule_evaluator", "status": "ok"})
    rule_result = _mock_rule_evaluator(state)

    llm_result = _mock_llm(state)

    state.decision = llm_result["decision"]
    state.confidence = llm_result["confidence"]
    state.rationale = llm_result["rationale"]
    state.review_required = llm_result["review_required"]
    state.provenance = llm_result["provenance"]

    state.audit_events.append(
        AuditEvent(
            event_id=f"AUD-{state.request_id}",
            event_type="decision",
            request_id=state.request_id,
            user_id=state.user_id,
            details={
                "decision": state.decision,
                "confidence": state.confidence,
                "review_required": state.review_required,
            },
        )
    )

    return state
