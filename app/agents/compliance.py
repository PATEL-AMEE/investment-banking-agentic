"""Compliance inspection agent — a LangGraph ``StateGraph``.

validate_input → retrieve_context → analyze_rules → decide
→ (escalate_if_needed) → generate_reasoning → assemble_result

Output contract matches the original ``run_inspection_workflow`` exactly.
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.services.azure_openai_adapter import AzureOpenAIAdapter
from app.services.event_bus import TOPIC_COMPLIANCE_DECISION, TOPIC_REVIEW_ESCALATED, event_bus


class ComplianceState(TypedDict, total=False):
    # inputs
    client_id: str
    tx_data: Dict[str, Any]
    request_id: str
    user_id: str
    session_id: str
    workflow_step: str
    store: Any
    persist_review: bool
    # intermediate
    client: Dict[str, Any]
    documents: List[Dict[str, Any]]
    evidence: List[Dict[str, Any]]
    policies: List[Dict[str, Any]]
    rule_hits: List[Dict[str, Any]]
    risk_score: float
    confidence: float
    decision: str
    review_required: bool
    review_task_id: str | None
    reasoning: str
    # output
    result: Dict[str, Any]


def _validate_input(state: ComplianceState) -> Dict[str, Any]:
    client = state["store"].get_client(state["client_id"])
    if not client:
        raise ValueError(f"Client {state['client_id']} not found")
    return {"client": client}


def _retrieve_context(state: ComplianceState) -> Dict[str, Any]:
    store = state["store"]
    documents = store.get_related_documents(state["client_id"])
    evidence: List[Dict[str, Any]] = []
    for document in documents:
        evidence.extend(store.get_evidence(document.get("doc_id", "")))
    policies = store.get_related_policies(state["client_id"])
    return {"documents": documents, "evidence": evidence, "policies": policies}


def _analyze_rules(state: ComplianceState) -> Dict[str, Any]:
    client, documents = state["client"], state["documents"]
    evidence, policies = state["evidence"], state["policies"]
    evidence_ids = [e["evidence_id"] for e in evidence if e.get("evidence_id")]
    rule_hits = []
    if float(client.get("risk_score", 0)) > 0.6:
        rule_hits.append({"rule_id": "AML-001", "rule_title": "Enhanced due diligence", "match_score": 0.91, "evidence_ids": evidence_ids})
    if documents:
        rule_hits.append({"rule_id": "KYC-001", "rule_title": "Document completeness", "match_score": 0.84, "evidence_ids": evidence_ids})
    if policies:
        rule_hits.append({"rule_id": "POL-001", "rule_title": "Policy applicability", "match_score": 0.88, "evidence_ids": evidence_ids})
    return {"rule_hits": rule_hits}


def _decide(state: ComplianceState) -> Dict[str, Any]:
    risk_score = float(state["client"].get("risk_score", 0.0))
    amount = float(state["tx_data"].get("amount", 0))
    confidence = min(0.99, 0.65 + (risk_score * 0.2) + (0.05 if state["rule_hits"] else 0.0) + (0.05 if amount > 500000 else 0.0))
    # Three-tier outcome: a very-high-risk client (or a very large transaction)
    # fails outright; an elevated-risk/low-confidence case warns and is routed
    # to human review; otherwise it passes. All non-pass outcomes require review.
    fail = risk_score >= 0.85 or amount >= 5_000_000
    review_required = fail or risk_score > 0.7 or amount > 500000 or confidence < 0.8
    decision = "fail" if fail else ("warn" if review_required else "pass")
    return {
        "risk_score": risk_score,
        "confidence": confidence,
        "review_required": review_required,
        "decision": decision,
        "review_task_id": None,
    }


def _route_after_decide(state: ComplianceState) -> str:
    if state["review_required"] and state.get("persist_review", True):
        return "escalate_if_needed"
    return "generate_reasoning"


def _escalate_if_needed(state: ComplianceState) -> Dict[str, Any]:
    # Reuse the trailing segment of the request id (e.g. "REQ-001" -> "001")
    # so review ids read cleanly as "REV-001" rather than "REV--001". Fall
    # back to a unique suffix when the request id carries no real identity
    # (avoids collisions like "REV-unknown" swallowing unrelated cases).
    review_suffix = state["request_id"].rsplit("-", 1)[-1] or state["request_id"]
    if not review_suffix or review_suffix.lower() in {"unknown", "none"}:
        review_suffix = uuid4().hex[:8].upper()
    review_task_id = f"REV-{review_suffix}"
    severity = "high" if state["risk_score"] > 0.7 else "medium"
    # Approval package: everything a human approver needs to act on the case.
    details = {
        "case_id": review_task_id,
        "risk_level": severity.capitalize(),
        "agent_recommendation": "Escalate to Level 2 compliance review",
        "decision_summary": (
            f"Compliance decision '{state['decision']}' for client {state['client_id']} "
            f"(risk score {state['risk_score']:.2f}, confidence {state['confidence']:.2f})."
        ),
        "evidence": [e.get("excerpt", "") for e in state["evidence"] if e.get("excerpt")][:5],
        "policy_references": [p.get("policy_id") for p in state["policies"] if p.get("policy_id")],
        "rule_hits": [hit["rule_id"] for hit in state["rule_hits"]],
        "approver_role": "Level 2 Compliance Officer",
        "available_actions": ["approve", "reject", "request_more_information", "escalate_to_senior_compliance"],
    }
    state["store"].add_review(
        review_task_id,
        state["client_id"],
        "High risk or low confidence review required",
        severity,
        state["user_id"],
        details=details,
    )
    return {"review_task_id": review_task_id}


def _generate_reasoning(state: ComplianceState) -> Dict[str, Any]:
    # Dashboard scans run this workflow for every client; skip LLM calls
    # there to avoid dozens of generations per page load.
    if state.get("workflow_step") == "dashboard-scan":
        return {"reasoning": {"summary": "Dashboard scan (no generated narrative).", "mode": "skipped"}}
    adapter = AzureOpenAIAdapter()
    reasoning = adapter.generate_reasoning(
        f"Client {state['client_id']} was assessed with risk score {state['risk_score']:.2f}. "
        f"Decision: {state['decision']} (confidence {state['confidence']:.2f}). "
        f"Rules triggered: {', '.join(hit['rule_id'] for hit in state['rule_hits']) or 'none'}. "
        f"Human review required: {state['review_required']}."
    )
    return {"reasoning": reasoning}


def _assemble_result(state: ComplianceState) -> Dict[str, Any]:
    provenance = [
        {"source_id": doc.get("doc_id"), "source_type": "Document", "excerpt": doc.get("doc_id", "")}
        for doc in state["documents"]
    ]
    provenance.extend(
        {"source_id": e.get("evidence_id"), "source_type": "Evidence", "excerpt": e.get("excerpt", "")}
        for e in state["evidence"]
    )
    # Prefer the LLM-generated narrative as the human-facing rationale;
    # fall back to the static sentence when generation is mocked/skipped.
    reasoning = state["reasoning"]
    if reasoning.get("mode") in ("azure", "openai-compatible") and reasoning.get("summary"):
        rationale = reasoning["summary"]
    else:
        rationale = "Compliance workflow evaluated the client profile, supporting evidence, and policy applicability."

    # Publish the decision (skip dashboard scans — 30+ synthetic runs/page).
    if state.get("workflow_step") != "dashboard-scan":
        event_bus.publish(
            TOPIC_COMPLIANCE_DECISION,
            {
                "client_id": state["client_id"],
                "request_id": state["request_id"],
                "decision": state["decision"],
                "confidence": round(state["confidence"], 2),
                "review_task_id": state["review_task_id"],
                "actor": "AGENT_COMPLIANCE_001",
            },
        )
        if state["review_task_id"]:
            event_bus.publish(
                TOPIC_REVIEW_ESCALATED,
                {
                    "review_task_id": state["review_task_id"],
                    "client_id": state["client_id"],
                    "source_agent": "AGENT_COMPLIANCE_001",
                    "request_id": state["request_id"],
                },
            )
    return {
        "result": {
            "decision": state["decision"],
            "decisionCode": state["decision"].upper(),
            "confidence": round(state["confidence"], 2),
            "rationale": rationale,
            "rule_hits": state["rule_hits"],
            "provenance": provenance,
            "reviewRequired": state["review_required"],
            "reviewTaskId": state["review_task_id"],
            "reasoning": state["reasoning"],
            "request_id": state["request_id"],
            "user_id": state["user_id"],
            "session_id": state["session_id"],
            "workflow_step": state["workflow_step"],
            "tx_data": state["tx_data"],
        }
    }


def build_compliance_agent():
    graph = StateGraph(ComplianceState)
    graph.add_node("validate_input", _validate_input)
    graph.add_node("retrieve_context", _retrieve_context)
    graph.add_node("analyze_rules", _analyze_rules)
    graph.add_node("decide", _decide)
    graph.add_node("escalate_if_needed", _escalate_if_needed)
    graph.add_node("generate_reasoning", _generate_reasoning)
    graph.add_node("assemble_result", _assemble_result)

    graph.add_edge(START, "validate_input")
    graph.add_edge("validate_input", "retrieve_context")
    graph.add_edge("retrieve_context", "analyze_rules")
    graph.add_edge("analyze_rules", "decide")
    graph.add_conditional_edges("decide", _route_after_decide, ["escalate_if_needed", "generate_reasoning"])
    graph.add_edge("escalate_if_needed", "generate_reasoning")
    graph.add_edge("generate_reasoning", "assemble_result")
    graph.add_edge("assemble_result", END)
    return graph.compile()


_compliance_agent = None


def get_compliance_agent():
    global _compliance_agent
    if _compliance_agent is None:
        _compliance_agent = build_compliance_agent()
    return _compliance_agent


def run_compliance_agent(
    client_id: str,
    tx_data: Dict[str, Any],
    request_id: str,
    user_id: str,
    session_id: str,
    workflow_step: str,
    store: Any,
    persist_review: bool = True,
) -> Dict[str, Any]:
    from app.services.telemetry import span

    with span("agent.compliance", client_id=client_id, request_id=request_id, workflow_step=workflow_step):
        final_state = get_compliance_agent().invoke(
            {
                "client_id": client_id,
                "tx_data": tx_data,
                "request_id": request_id,
                "user_id": user_id,
                "session_id": session_id,
                "workflow_step": workflow_step,
                "store": store,
                "persist_review": persist_review,
            }
        )
    return final_state["result"]
