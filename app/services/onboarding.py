from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.services.audit import AuditLog
from app.services.sanctions import HIGH_RISK_JURISDICTIONS, sanctions_check


# Non-PEP markers that should not count as a politically exposed person.
_NON_PEP = {"", "non_pep", "none", "no"}


def _normalise_kyc(client_name: str, jurisdiction: str) -> Dict[str, Any]:
    """Node: parse_kyc_input — normalise client-supplied KYC fields."""
    return {
        "client_name": (client_name or "").strip(),
        "jurisdiction": (jurisdiction or "").strip().upper(),
    }


def _retrieve_regulations(store: Any, jurisdiction: str) -> List[Dict[str, Any]]:
    """Node: retrieve_regulations — applicable AML/KYC regulations.

    Best-effort against whichever backend is wired in. The default local
    ``GraphStore`` exposes ``get_regulations_by_jurisdiction``; other backends
    fall back to an empty list rather than failing onboarding.
    """
    getter = getattr(store, "get_regulations_by_jurisdiction", None)
    if getter is None:
        return []
    try:
        return getter(jurisdiction)
    except Exception:
        return []


def _analyze_pep(beneficial_owners: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Node: analyze_pep — flag politically exposed beneficial owners."""
    pep_details: List[Dict[str, Any]] = []
    for owner in beneficial_owners:
        status = str(owner.get("pep_status", "")).strip().lower()
        if status not in _NON_PEP:
            pep_details.append(
                {
                    "name": owner.get("full_name") or owner.get("name"),
                    "pep_type": owner.get("pep_status"),
                    "risk_level": "high",
                }
            )
    return {"pep_found": bool(pep_details), "pep_count": len(pep_details), "pep_details": pep_details}


def _assess_risk(pep_found: bool, sanctions: Dict[str, Any], jurisdiction: str) -> float:
    """Node: assess_risk — deterministic AML risk score (0-10)."""
    score = 1.0  # base low risk
    if pep_found:
        score += 3.0
    if sanctions.get("is_sanctioned"):
        score += 4.0
    if jurisdiction in HIGH_RISK_JURISDICTIONS:
        score += 2.0
    return min(score, 10.0)


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
    """Run the KYC/AML onboarding agent as an explicit node sequence.

    parse_kyc -> retrieve_regulations -> check_sanctions -> analyze_pep
    -> assess_risk -> generate_profile -> escalate_if_needed -> persist_record
    """
    beneficial_owners = beneficial_owners or []
    tool_calls: List[Dict[str, Any]] = []

    kyc = _normalise_kyc(client_name, jurisdiction)

    regulations = _retrieve_regulations(store, kyc["jurisdiction"])
    tool_calls.append({"tool": "graph_retriever", "status": "ok", "result_count": len(regulations)})

    sanctions = sanctions_check(kyc["client_name"], kyc["jurisdiction"], beneficial_owners)
    tool_calls.append({"tool": "sanctions_check", "status": "ok", "is_sanctioned": sanctions["is_sanctioned"]})

    pep = _analyze_pep(beneficial_owners)
    risk_score = _assess_risk(pep["pep_found"], sanctions, kyc["jurisdiction"])
    risk_tier = "high" if risk_score >= 5.0 else "low"

    escalate = risk_score >= 5.0 or pep["pep_found"] or sanctions["is_sanctioned"]
    final_status = "PENDING_REVIEW" if escalate else "APPROVED"

    review_task_id = None
    if escalate:
        review_suffix = request_id.rsplit("-", 1)[-1] or request_id
        review_task_id = f"REV-KYC-{review_suffix}"
        reason_bits = []
        if pep["pep_found"]:
            reason_bits.append("PEP detected")
        if sanctions["is_sanctioned"]:
            reason_bits.append("sanctions match")
        if risk_score >= 5.0:
            reason_bits.append(f"risk score {risk_score:.1f}")
        reason = "; ".join(reason_bits) or "Enhanced due diligence required"
        try:
            store.add_review(review_task_id, client_id, reason, risk_tier, user_id)
        except Exception:
            review_task_id = None

    sanctions_result = "match" if sanctions["is_sanctioned"] else "no_match"

    provenance = [
        {"source_id": reg.get("regulation_id"), "source_type": "Regulation", "excerpt": reg.get("title", "")}
        for reg in regulations
    ]

    if audit_log is not None:
        audit_log.record(
            event_type="onboarding_assessment",
            actor_id="AGENT_ONBOARDING_001",
            action="assess_kyc",
            result=final_status,
            resource_id=client_id,
            request_id=request_id,
            metadata={"risk_score": risk_score, "pep_found": pep["pep_found"], "sanctions_result": sanctions_result},
        )

    return {
        "request_id": request_id,
        "client_id": client_id,
        "status": final_status,
        "risk_score": round(risk_score, 2),
        "risk_tier": risk_tier,
        "pep_found": pep["pep_found"],
        "pep_details": pep["pep_details"],
        "sanctions_check_result": sanctions_result,
        "sanctions_matches": sanctions["matches"],
        "review_required": escalate,
        "review_task_id": review_task_id,
        "retrieved_regulations": regulations,
        "provenance": provenance,
        "tool_calls": tool_calls,
    }
