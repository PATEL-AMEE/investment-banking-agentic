"""Copilot answer feedback — the "flag as wrong" loop (Phase 6.6).

Every Copilot/supervisor answer carries a lightweight feedback control. Staff
verdicts are captured here and become signal for the Phase 9 evaluation loop:
a ``wrong`` verdict turns the flagged question into a regression case that the
eval harness re-runs, so a human-spotted failure is measured on every
subsequent evaluation until it is fixed.

Demo scope: in-memory. A real deployment persists this next to the audit trail
and feeds it into the Phase 1 data-curation / Phase 8 fine-tuning backlog.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

VALID_VERDICTS = {"wrong", "good"}

_feedback: List[Dict[str, Any]] = []


def record_feedback(
    *,
    query: str,
    answer: str,
    verdict: str,
    user_id: str,
    comment: Optional[str] = None,
    sources: Optional[List[str]] = None,
    audit_log: Any = None,
) -> Dict[str, Any]:
    """Store one feedback entry; raises ``ValueError`` on an unknown verdict."""
    verdict = (verdict or "").strip().lower()
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VALID_VERDICTS)}")
    entry = {
        "feedback_id": f"FB-{uuid.uuid4().hex[:8].upper()}",
        "query": query,
        "answer": answer,
        "verdict": verdict,
        "comment": comment or "",
        "sources": sources or [],
        "user_id": user_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _feedback.append(entry)
    if audit_log is not None:
        audit_log.record(
            event_type="copilot_feedback",
            actor_id=user_id or "user-unknown",
            action=f"flag:{verdict}",
            result="recorded",
            metadata={"has_comment": bool(comment)},
        )
    return entry


def list_feedback(verdict: Optional[str] = None) -> List[Dict[str, Any]]:
    if verdict is None:
        return list(_feedback)
    return [f for f in _feedback if f["verdict"] == verdict]


def flagged_questions() -> List[Dict[str, Any]]:
    """Distinct questions flagged ``wrong`` — the eval-loop regression set.

    Deduplicated by question text, keeping the most recent flag so a question
    fixed and re-flagged reflects the latest report.
    """
    by_question: Dict[str, Dict[str, Any]] = {}
    for entry in _feedback:
        if entry["verdict"] == "wrong":
            by_question[entry["query"].strip().lower()] = entry
    return list(by_question.values())


def summary() -> Dict[str, int]:
    return {
        "total": len(_feedback),
        "wrong": len([f for f in _feedback if f["verdict"] == "wrong"]),
        "good": len([f for f in _feedback if f["verdict"] == "good"]),
        "flagged_questions": len(flagged_questions()),
    }


def clear() -> None:
    _feedback.clear()
