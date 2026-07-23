"""Client-facing application intake and status.

The client is the trigger for the onboarding chain but sits completely
outside the internal staff tools. A submission from the client portal:

1. kicks off the **onboarding agent** (the downstream orchestrator:
   regulations retrieval, sanctions + PEP screening, risk scoring, and
   human-review escalation),
2. runs the **NLP pipeline** over any supplied document text (entities,
   clauses, classification) for the internal record,
3. computes the **missing-document checklist** against the required set.

Everything the pipeline produces stays server-side in the application
record. The client only ever sees a plain-language status — "application
received", "under review", "additional documents needed" — never the risk
score, screening results, regulation citations, or internal reasoning.
Resource scoping (a client token reads its own application only) is
enforced at the API layer.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Documents every corporate application must provide before approval.
REQUIRED_DOCUMENTS: Dict[str, str] = {
    "certificate_of_incorporation": "Certificate of incorporation",
    "proof_of_registered_address": "Proof of registered business address",
    "source_of_funds_declaration": "Source-of-funds declaration",
    "beneficial_ownership_evidence": "Beneficial ownership documentation",
}

# In-memory application register (demo scope; a real deployment would persist
# this next to the graph). Keyed by application_id.
_applications: Dict[str, Dict[str, Any]] = {}


def submit_application(
    *,
    company_name: str,
    jurisdiction: str,
    product: str,
    beneficial_owners: List[Dict[str, Any]],
    documents_provided: List[str],
    document_text: Optional[str],
    store: Any,
    audit_log: Any = None,
) -> Dict[str, Any]:
    """Accept a client submission and run the internal onboarding pipeline."""
    from app.services.onboarding import run_onboarding_workflow

    application_id = f"APP-{uuid.uuid4().hex[:8].upper()}"
    request_id = f"REQ-{application_id}"
    client_id = "CLIENT-" + (company_name or "unknown").upper().replace(" ", "-")

    # 1) Onboarding agent — the orchestrator for everything downstream
    #    (graph regulations, sanctions/PEP tools, risk scoring, escalation).
    kyc = run_onboarding_workflow(
        client_id=client_id,
        client_name=company_name,
        jurisdiction=jurisdiction,
        beneficial_owners=beneficial_owners,
        request_id=request_id,
        user_id=f"client-portal:{company_name}",
        store=store,
        audit_log=audit_log,
    )

    # 2) Document analysis over any pasted/uploaded text (internal record).
    nlp_result: Dict[str, Any] = {}
    if document_text:
        from app.services.nlp_pipeline import analyze

        nlp_result = analyze(document_text)

    # 3) Missing-document checklist.
    provided = {doc for doc in documents_provided if doc in REQUIRED_DOCUMENTS}
    missing = [key for key in REQUIRED_DOCUMENTS if key not in provided]

    record = {
        "application_id": application_id,
        "client_id": kyc.get("client_id", client_id),
        "company_name": company_name,
        "jurisdiction": jurisdiction,
        "product": product,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "documents_provided": sorted(provided),
        "documents_missing": missing,
        # Internal-only pipeline outputs (never returned to the client):
        "internal": {
            "kyc": kyc,
            "nlp": nlp_result,
            "review_task_id": kyc.get("review_task_id"),
        },
    }
    _applications[application_id] = record
    _ensure_final_review(record, store, audit_log)

    if audit_log is not None:
        audit_log.record(
            event_type="client_application",
            actor_id=f"client-portal:{company_name}",
            action="submit_application",
            result=kyc.get("status", "unknown"),
            resource_id=application_id,
            request_id=request_id,
            metadata={"product": product, "documents_missing": len(missing)},
        )
    return record


def add_documents(
    *,
    record: Dict[str, Any],
    submitted: List[Dict[str, Any]],
    store: Any,
    audit_log: Any = None,
) -> Dict[str, Any]:
    """Attach follow-up documents to an existing application.

    Each file runs through the document-analysis agent (classification, NLP
    enrichment, knowledge-graph persistence) — outputs stay internal. The
    missing-document checklist shrinks accordingly, which is all the client
    sees.

    ``submitted`` items: ``{"doc_type": <REQUIRED_DOCUMENTS key>,
    "file_path": <saved upload path>, "original_name": <client filename>}``.
    """
    from app.agents.document_analysis import run_document_analysis

    accepted: List[str] = []
    analyses = record["internal"].setdefault("documents", {})
    for item in submitted:
        doc_type = item.get("doc_type")
        if doc_type not in REQUIRED_DOCUMENTS:
            continue
        analysis = run_document_analysis(
            item["file_path"],
            metadata={
                "client_id": record["client_id"],
                "application_id": record["application_id"],
                "required_doc_type": doc_type,
                "original_name": item.get("original_name", ""),
            },
            store=store,
        )
        analyses[doc_type] = analysis
        accepted.append(doc_type)

    provided = set(record["documents_provided"]) | set(accepted)
    record["documents_provided"] = sorted(provided)
    record["documents_missing"] = [key for key in REQUIRED_DOCUMENTS if key not in provided]
    _ensure_final_review(record, store, audit_log)

    if audit_log is not None and accepted:
        audit_log.record(
            event_type="client_documents_submitted",
            actor_id=f"client-portal:{record['company_name']}",
            action="submit_documents",
            result="accepted",
            resource_id=record["application_id"],
            metadata={
                "documents": ",".join(accepted),
                "still_missing": len(record["documents_missing"]),
            },
        )
    return record


def _ensure_final_review(record: Dict[str, Any], store: Any, audit_log: Any = None) -> None:
    """Maker-checker: no application is ever approved without a human.

    Once the automated screening is clean AND the document file is complete,
    the application still goes to the review queue as a low-severity final
    onboarding approval. Flagged applications already carry their own
    (higher-severity) review from the onboarding agent.
    """
    if record["internal"].get("review_task_id"):
        return  # already in the review queue (screening flag or earlier final review)
    if record["documents_missing"]:
        return  # file incomplete — nothing for an approver to sign off yet
    review_id = f"REV-ONB-{record['application_id'].split('-', 1)[-1]}"
    details = {
        "case_id": review_id,
        "risk_level": "Low",
        "agent_recommendation": "Approve — automated checks passed and the document file is complete",
        "decision_summary": (
            f"Onboarding application {record['application_id']} for {record['company_name']} "
            f"({record['jurisdiction']}, {record['product']}): screening clean, all required documents received."
        ),
        "evidence": [REQUIRED_DOCUMENTS[key] + " received" for key in record["documents_provided"]],
        "policy_references": [],
        "approver_role": "Onboarding Approvals",
        "available_actions": ["approve", "reject", "request_more_information", "escalate_to_senior_compliance"],
    }
    try:
        store.add_review(
            review_id,
            record["client_id"],
            "Final onboarding approval (all automated checks passed)",
            "low",
            f"client-portal:{record['company_name']}",
            details=details,
        )
    except Exception:
        return
    record["internal"]["review_task_id"] = review_id
    if audit_log is not None:
        audit_log.record(
            event_type="review_escalated",
            actor_id="AGENT_ONBOARDING_001",
            action="final_onboarding_approval",
            result="pending",
            resource_id=record["application_id"],
            metadata={"review_task_id": review_id},
        )


def get_application(application_id: str) -> Optional[Dict[str, Any]]:
    return _applications.get(application_id)


def applications_for_client(client_id: str) -> List[Dict[str, Any]]:
    return [r for r in _applications.values() if r.get("client_id") == client_id]


def _review_state(record: Dict[str, Any], store: Any) -> Optional[Dict[str, Any]]:
    review_id = record["internal"].get("review_task_id")
    if not review_id:
        return None
    try:
        for review in store.list_all_reviews():
            if review.get("review_id") == review_id:
                return review
    except Exception:
        pass
    return None


def client_view(record: Dict[str, Any], store: Any) -> Dict[str, Any]:
    """The ONLY shape the client ever sees: plain status + what they must do.

    Deliberately excludes risk scores, PEP/sanctions results, regulation
    citations, review reasons, and all agent reasoning.
    """
    review = _review_state(record, store)
    kyc_status = record["internal"]["kyc"].get("status")

    if review and review.get("status") == "resolved":
        decision = str(review.get("decision", "")).lower()
        if decision in {"approve", "approved"}:
            status, message = "approved", "Your application has been approved. Our team will contact you to activate the account."
        else:
            status, message = "declined", "We are unable to proceed with your application at this time. Please contact your relationship manager."
    elif record["documents_missing"] and review is None and kyc_status != "PENDING_REVIEW":
        status, message = "additional_documents_needed", "We need additional documents before your application can proceed."
    else:
        # Every complete application awaits a human decision — approval is
        # never automatic (maker-checker), and clients are given the SLA.
        status, message = (
            "under_review",
            "Your application is under review by our team. Decisions typically take up to 5 working days. "
            "No action is needed from you right now.",
        )

    next_steps: List[Dict[str, str]] = []
    if record["documents_missing"] and status not in {"approved", "declined"}:
        next_steps = [
            {"key": key, "label": REQUIRED_DOCUMENTS[key]} for key in record["documents_missing"]
        ]

    return {
        "applicationId": record["application_id"],
        "company": record["company_name"],
        "product": record["product"],
        "submittedAt": record["submitted_at"],
        "status": status,
        "message": message,
        "outstandingDocuments": next_steps,
    }
