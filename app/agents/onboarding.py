"""KYC/AML onboarding agent — a LangGraph ``StateGraph``.

parse_kyc → retrieve_regulations → check_sanctions → analyze_pep
→ assess_risk → (escalate_if_needed) → persist_record

Deterministic screening/scoring stays in tools (no LLM in the decision path);
output contract matches the original ``run_onboarding_workflow`` exactly.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict
from uuid import uuid4

from langgraph.graph import END, START, StateGraph

from app.agents.base import default_registry
from app.services.audit import AuditLog
from app.services.event_bus import TOPIC_CLIENT_ONBOARDED, TOPIC_REVIEW_ESCALATED, event_bus
from app.services.sanctions import HIGH_RISK_JURISDICTIONS

# Non-PEP markers that should not count as a politically exposed person.
_NON_PEP = {"", "non_pep", "none", "no"}


class OnboardingState(TypedDict, total=False):
    # inputs
    client_id: str
    client_name: str
    jurisdiction: str
    beneficial_owners: List[Dict[str, Any]]
    request_id: str
    user_id: str
    store: Any
    audit_log: Optional[AuditLog]
    # intermediate
    regulations: List[Dict[str, Any]]
    sanctions: Dict[str, Any]
    pep: Dict[str, Any]
    risk_score: float
    risk_tier: str
    final_status: str
    review_task_id: Optional[str]
    tool_calls: List[Dict[str, Any]]
    # output
    result: Dict[str, Any]


def _parse_kyc(state: OnboardingState) -> Dict[str, Any]:
    """Node: parse_kyc_input — normalise client-supplied KYC fields."""
    return {
        "client_name": (state.get("client_name") or "").strip(),
        "jurisdiction": (state.get("jurisdiction") or "").strip().upper(),
        "beneficial_owners": state.get("beneficial_owners") or [],
        "tool_calls": [],
    }


def _retrieve_regulations(state: OnboardingState) -> Dict[str, Any]:
    """Node: retrieve_regulations — applicable AML/KYC regulations via graph tool."""
    regulations = default_registry.call(
        "graph_retriever", store=state["store"], jurisdiction=state["jurisdiction"]
    )
    tool_calls = state["tool_calls"] + [
        {"tool": "graph_retriever", "status": "ok", "result_count": len(regulations)}
    ]
    return {"regulations": regulations, "tool_calls": tool_calls}


def _check_sanctions(state: OnboardingState) -> Dict[str, Any]:
    """Node: check_sanctions — screen client and beneficial owners."""
    sanctions = default_registry.call(
        "sanctions_check",
        client_name=state["client_name"],
        client_country=state["jurisdiction"],
        beneficial_owners=state["beneficial_owners"],
    )
    tool_calls = state["tool_calls"] + [
        {"tool": "sanctions_check", "status": "ok", "is_sanctioned": sanctions["is_sanctioned"]}
    ]
    return {"sanctions": sanctions, "tool_calls": tool_calls}


def _analyze_pep(state: OnboardingState) -> Dict[str, Any]:
    """Node: analyze_pep — flag politically exposed beneficial owners."""
    pep_details: List[Dict[str, Any]] = []
    for owner in state["beneficial_owners"]:
        status = str(owner.get("pep_status", "")).strip().lower()
        if status not in _NON_PEP:
            pep_details.append(
                {
                    "name": owner.get("full_name") or owner.get("name"),
                    "pep_type": owner.get("pep_status"),
                    "risk_level": "high",
                }
            )
    return {"pep": {"pep_found": bool(pep_details), "pep_count": len(pep_details), "pep_details": pep_details}}


def _assess_risk(state: OnboardingState) -> Dict[str, Any]:
    """Node: assess_risk — deterministic AML risk score (0-10)."""
    score = 1.0  # base low risk
    if state["pep"]["pep_found"]:
        score += 3.0
    if state["sanctions"].get("is_sanctioned"):
        score += 4.0
    if state["jurisdiction"] in HIGH_RISK_JURISDICTIONS:
        score += 2.0
    score = min(score, 10.0)
    escalate = score >= 5.0 or state["pep"]["pep_found"] or state["sanctions"]["is_sanctioned"]
    return {
        "risk_score": score,
        "risk_tier": "high" if score >= 5.0 else "low",
        "final_status": "PENDING_REVIEW" if escalate else "APPROVED",
        "review_task_id": None,
    }


def _route_after_risk(state: OnboardingState) -> str:
    if state["final_status"] == "PENDING_REVIEW":
        return "escalate_if_needed"
    return "persist_record"


def _escalate_if_needed(state: OnboardingState) -> Dict[str, Any]:
    """Node: escalate_if_needed — create a human review task with an approval package."""
    request_id = state["request_id"]
    review_suffix = request_id.rsplit("-", 1)[-1] or request_id
    if not review_suffix or review_suffix.lower() in {"unknown", "none"}:
        review_suffix = uuid4().hex[:8].upper()
    review_task_id: Optional[str] = f"REV-KYC-{review_suffix}"
    reason_bits = []
    evidence: List[str] = []
    if state["pep"]["pep_found"]:
        reason_bits.append("PEP detected")
        evidence.extend(
            f"PEP match: {detail.get('name')} ({detail.get('pep_type')})"
            for detail in state["pep"]["pep_details"]
        )
    if state["sanctions"]["is_sanctioned"]:
        reason_bits.append("sanctions match")
        evidence.extend(
            f"Sanctions match: {match.get('entity')} ({match.get('list')} list, {match.get('type')})"
            for match in state["sanctions"].get("matches", [])
        )
    if state["risk_score"] >= 5.0:
        reason_bits.append(f"risk score {state['risk_score']:.1f}")
    reason = "; ".join(reason_bits) or "Enhanced due diligence required"
    details = {
        "case_id": review_task_id,
        "risk_level": state["risk_tier"].capitalize(),
        "agent_recommendation": "Escalate — hold onboarding until the review is approved",
        "decision_summary": (
            f"KYC assessment for {state['client_name']} ({state['jurisdiction']}): "
            f"{state['final_status']} with risk score {state['risk_score']:.1f}/10."
        ),
        "evidence": evidence[:5],
        "policy_references": [
            reg.get("regulation_id") for reg in state["regulations"] if reg.get("regulation_id")
        ],
        "approver_role": "Financial Crime Risk Team",
        "available_actions": ["approve", "reject", "request_more_information", "escalate_to_senior_compliance"],
    }
    try:
        state["store"].add_review(
            review_task_id, state["client_id"], reason, state["risk_tier"], state["user_id"], details=details
        )
    except Exception:
        review_task_id = None
    return {"review_task_id": review_task_id}


def _persist_record(state: OnboardingState) -> Dict[str, Any]:
    """Node: persist_record — audit event + final response assembly."""
    sanctions_result = "match" if state["sanctions"]["is_sanctioned"] else "no_match"
    provenance = [
        {"source_id": reg.get("regulation_id"), "source_type": "Regulation", "excerpt": reg.get("title", "")}
        for reg in state["regulations"]
    ]

    audit_log = state.get("audit_log")
    if audit_log is not None:
        audit_log.record(
            event_type="onboarding_assessment",
            actor_id="AGENT_ONBOARDING_001",
            action="assess_kyc",
            result=state["final_status"],
            resource_id=state["client_id"],
            request_id=state["request_id"],
            metadata={
                "risk_score": state["risk_score"],
                "pep_found": state["pep"]["pep_found"],
                "sanctions_result": sanctions_result,
            },
        )

    # Publish domain events for downstream agents/consumers (Kafka-ready).
    event_bus.publish(
        TOPIC_CLIENT_ONBOARDED,
        {
            "client_id": state["client_id"],
            "request_id": state["request_id"],
            "status": state["final_status"],
            "risk_score": round(state["risk_score"], 2),
            "actor": "AGENT_ONBOARDING_001",
        },
    )
    if state["review_task_id"]:
        event_bus.publish(
            TOPIC_REVIEW_ESCALATED,
            {
                "review_task_id": state["review_task_id"],
                "client_id": state["client_id"],
                "source_agent": "AGENT_ONBOARDING_001",
                "request_id": state["request_id"],
            },
        )

    return {
        "result": {
            "request_id": state["request_id"],
            "client_id": state["client_id"],
            "status": state["final_status"],
            "risk_score": round(state["risk_score"], 2),
            "risk_tier": state["risk_tier"],
            "pep_found": state["pep"]["pep_found"],
            "pep_details": state["pep"]["pep_details"],
            "sanctions_check_result": sanctions_result,
            "sanctions_matches": state["sanctions"]["matches"],
            "review_required": state["final_status"] == "PENDING_REVIEW",
            "review_task_id": state["review_task_id"],
            "retrieved_regulations": state["regulations"],
            "provenance": provenance,
            "tool_calls": state["tool_calls"],
        }
    }


def build_onboarding_agent():
    graph = StateGraph(OnboardingState)
    graph.add_node("parse_kyc", _parse_kyc)
    graph.add_node("retrieve_regulations", _retrieve_regulations)
    graph.add_node("check_sanctions", _check_sanctions)
    graph.add_node("analyze_pep", _analyze_pep)
    graph.add_node("assess_risk", _assess_risk)
    graph.add_node("escalate_if_needed", _escalate_if_needed)
    graph.add_node("persist_record", _persist_record)

    graph.add_edge(START, "parse_kyc")
    graph.add_edge("parse_kyc", "retrieve_regulations")
    graph.add_edge("retrieve_regulations", "check_sanctions")
    graph.add_edge("check_sanctions", "analyze_pep")
    graph.add_edge("analyze_pep", "assess_risk")
    graph.add_conditional_edges("assess_risk", _route_after_risk, ["escalate_if_needed", "persist_record"])
    graph.add_edge("escalate_if_needed", "persist_record")
    graph.add_edge("persist_record", END)
    return graph.compile()


_onboarding_agent = None


def get_onboarding_agent():
    global _onboarding_agent
    if _onboarding_agent is None:
        _onboarding_agent = build_onboarding_agent()
    return _onboarding_agent


def run_onboarding_workflow(
    client_id: str,
    client_name: str,
    jurisdiction: str,
    beneficial_owners: Optional[List[Dict[str, Any]]],
    request_id: str,
    user_id: str,
    store: Any,
    audit_log: Optional[AuditLog] = None,
) -> Dict[str, Any]:
    """Run the KYC/AML onboarding agent (LangGraph StateGraph)."""
    from app.services.telemetry import span

    with span("agent.onboarding", client_id=client_id, request_id=request_id):
        final_state = get_onboarding_agent().invoke(
            {
                "client_id": client_id,
                "client_name": client_name,
                "jurisdiction": jurisdiction,
                "beneficial_owners": beneficial_owners or [],
                "request_id": request_id,
                "user_id": user_id,
                "store": store,
                "audit_log": audit_log,
            }
        )
    return final_state["result"]
