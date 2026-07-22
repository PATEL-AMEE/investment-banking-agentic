from __future__ import annotations

from typing import Any, Dict

from app.services.azure_openai_adapter import AzureOpenAIAdapter
from app.services.graph_store import GraphStore


def _build_rule_hits(client: Dict[str, Any], documents: list[Dict[str, Any]], evidence: list[Dict[str, Any]], policies: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    rule_hits = []
    if float(client.get("risk_score", 0)) > 0.6:
        rule_hits.append({"rule_id": "AML-001", "rule_title": "Enhanced due diligence", "match_score": 0.91, "evidence_ids": [e["evidence_id"] for e in evidence if e.get("evidence_id")]})
    if documents:
        rule_hits.append({"rule_id": "KYC-001", "rule_title": "Document completeness", "match_score": 0.84, "evidence_ids": [e["evidence_id"] for e in evidence if e.get("evidence_id")]})
    if policies:
        rule_hits.append({"rule_id": "POL-001", "rule_title": "Policy applicability", "match_score": 0.88, "evidence_ids": [e["evidence_id"] for e in evidence if e.get("evidence_id")]})
    return rule_hits


def run_inspection_workflow(
    client_id: str,
    tx_data: Dict[str, Any],
    request_id: str,
    user_id: str,
    session_id: str,
    workflow_step: str,
    store: GraphStore,
    persist_review: bool = True,
) -> Dict[str, Any]:
    client = store.get_client(client_id)
    if not client:
        raise ValueError(f"Client {client_id} not found")

    documents = store.get_related_documents(client_id)
    evidence = []
    for document in documents:
        evidence.extend(store.get_evidence(document.get("doc_id", "")))
    policies = store.get_related_policies(client_id)

    rule_hits = _build_rule_hits(client, documents, evidence, policies)
    risk_score = float(client.get("risk_score", 0.0))
    amount = float(tx_data.get("amount", 0))
    confidence = min(0.99, 0.65 + (risk_score * 0.2) + (0.05 if rule_hits else 0.0) + (0.05 if amount > 500000 else 0.0))

    review_required = risk_score > 0.7 or amount > 500000 or confidence < 0.8
    decision = "pass"
    if review_required:
        decision = "warn"

    review_task_id = None
    if review_required and persist_review:
        # Reuse the trailing segment of the request id (e.g. "REQ-001" -> "001")
        # so review ids read cleanly as "REV-001" rather than "REV--001".
        review_suffix = request_id.rsplit("-", 1)[-1] or request_id
        review_task_id = f"REV-{review_suffix}"
        store.add_review(review_task_id, client_id, "High risk or low confidence review required", "high" if risk_score > 0.7 else "medium", user_id)

    adapter = AzureOpenAIAdapter()
    reasoning = adapter.generate_reasoning(f"Review {client_id} with risk {risk_score}")

    provenance = [
        {"source_id": doc.get("doc_id"), "source_type": "Document", "excerpt": doc.get("doc_id", "")}
        for doc in documents
    ]
    provenance.extend(
        {"source_id": e.get("evidence_id"), "source_type": "Evidence", "excerpt": e.get("excerpt", "")}
        for e in evidence
    )

    return {
        "decision": decision,
        "decisionCode": decision.upper(),
        "confidence": round(confidence, 2),
        "rationale": "Compliance workflow evaluated the client profile, supporting evidence, and policy applicability.",
        "rule_hits": rule_hits,
        "provenance": provenance,
        "reviewRequired": review_required,
        "reviewTaskId": review_task_id,
        "reasoning": reasoning,
        "request_id": request_id,
        "user_id": user_id,
        "session_id": session_id,
        "workflow_step": workflow_step,
        "tx_data": tx_data,
    }
