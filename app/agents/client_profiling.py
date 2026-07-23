"""Client-profiling agent — a LangGraph ``StateGraph``.

fetch_client → gather_relationships → build_profile

Produces a 360° client view by traversing the knowledge graph: identity,
risk banding, related documents/evidence/policies, the regulations applying
in the client's home jurisdiction, and the beneficial-ownership structure
with explained relationship paths (client → owner → jurisdiction → rule).
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.base import default_registry
from app.services.graph_store import HIGH_RISK_COUNTRIES


class ProfilingState(TypedDict, total=False):
    # inputs
    client_id: str
    store: Any
    # intermediate
    client: Dict[str, Any]
    documents: List[Dict[str, Any]]
    evidence: List[Dict[str, Any]]
    policies: List[Dict[str, Any]]
    regulations: List[Dict[str, Any]]
    owners: List[Dict[str, Any]]
    # output
    result: Dict[str, Any]


def _fetch_client(state: ProfilingState) -> Dict[str, Any]:
    client = state["store"].get_client(state["client_id"])
    if not client:
        raise ValueError(f"Client {state['client_id']} not found")
    return {"client": client}


def _gather_relationships(state: ProfilingState) -> Dict[str, Any]:
    store = state["store"]
    documents = store.get_related_documents(state["client_id"])
    evidence: List[Dict[str, Any]] = []
    for document in documents:
        evidence.extend(store.get_evidence(document.get("doc_id", "")))
    policies = store.get_related_policies(state["client_id"])
    regulations = default_registry.call(
        "graph_retriever", store=store, jurisdiction=(state["client"].get("country") or "").upper()
    )
    # Ownership traversal is optional on older store backends.
    get_ownership = getattr(store, "get_ownership", None)
    owners = get_ownership(state["client_id"]) if get_ownership else []
    return {
        "documents": documents,
        "evidence": evidence,
        "policies": policies,
        "regulations": regulations,
        "owners": owners,
    }


def _build_ownership_paths(client: Dict[str, Any], owners: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Explained graph paths: client → owner entity → jurisdiction → rule.

    Each path carries the hop list, a one-line narrative, and — when the
    owner sits in a jurisdiction flagged by the country-risk framework — the
    triggered control and the reason the conclusion was reached.
    """
    client_name = client.get("name") or client.get("client_id", "client")
    paths: List[Dict[str, Any]] = []
    for owner in owners:
        owner_name = owner.get("name", owner.get("entity_id", "entity"))
        country = (owner.get("country") or "").upper()
        pct = owner.get("ownership_pct", "")
        high_risk = country in HIGH_RISK_COUNTRIES

        hops = [
            {"from": client_name, "relation": "OWNED_BY", "to": owner_name, "detail": f"{pct}% beneficial ownership" if pct else ""},
            {"from": owner_name, "relation": "LOCATED_IN", "to": country},
        ]
        narrative = f"{client_name} → owned by {owner_name} ({pct}%) → located in {country}"
        if high_risk:
            hops.append({"from": country, "relation": "SUBJECT_TO", "to": "Enhanced Due Diligence (country-risk framework)"})
            narrative += " → subject to Enhanced Due Diligence → requires Financial Crime Risk Team approval"
        paths.append(
            {
                "hops": hops,
                "narrative": narrative,
                "high_risk": high_risk,
                "reason": (
                    f"{owner_name} operates in {country}, a jurisdiction classified as high risk "
                    "under the bank's country-risk framework."
                    if high_risk
                    else f"{country} is not flagged by the bank's country-risk framework."
                ),
            }
        )
    return paths


def _build_profile(state: ProfilingState) -> Dict[str, Any]:
    client = state["client"]
    risk_score = float(client.get("risk_score", 0.0))
    if risk_score > 0.7:
        risk_band = "high"
    elif risk_score >= 0.4:
        risk_band = "medium"
    else:
        risk_band = "low"

    owners = state.get("owners") or []
    ownership_paths = _build_ownership_paths(client, owners)
    high_risk_ownership = any(path["high_risk"] for path in ownership_paths)

    summary = (
        f"{client.get('name')} ({client.get('country')}) is a {risk_band}-risk client "
        f"(score {risk_score:.2f}) with {len(state['documents'])} documents, "
        f"{len(state['evidence'])} evidence items and {len(state['policies'])} applicable policies."
    )
    if high_risk_ownership:
        summary += " Ownership structure includes entities in high-risk jurisdictions; enhanced due diligence applies."

    return {
        "result": {
            "client_id": state["client_id"],
            "name": client.get("name"),
            "country": client.get("country"),
            "risk_score": risk_score,
            "risk_band": risk_band,
            "document_count": len(state["documents"]),
            "evidence_count": len(state["evidence"]),
            "policy_count": len(state["policies"]),
            "documents": [doc.get("doc_id") for doc in state["documents"]],
            "policies": [pol.get("policy_id") for pol in state["policies"]],
            "applicable_regulations": [
                {"regulation_id": reg.get("regulation_id"), "title": reg.get("title", "")}
                for reg in state["regulations"]
            ],
            "ownership_structure": [
                {
                    "entity_id": owner.get("entity_id"),
                    "name": owner.get("name"),
                    "country": owner.get("country"),
                    "ownership_pct": owner.get("ownership_pct"),
                }
                for owner in owners
            ],
            "relationship_paths": ownership_paths,
            "high_risk_ownership": high_risk_ownership,
            "summary": summary,
        }
    }


def build_profiling_agent():
    graph = StateGraph(ProfilingState)
    graph.add_node("fetch_client", _fetch_client)
    graph.add_node("gather_relationships", _gather_relationships)
    graph.add_node("build_profile", _build_profile)

    graph.add_edge(START, "fetch_client")
    graph.add_edge("fetch_client", "gather_relationships")
    graph.add_edge("gather_relationships", "build_profile")
    graph.add_edge("build_profile", END)
    return graph.compile()


_profiling_agent = None


def get_profiling_agent():
    global _profiling_agent
    if _profiling_agent is None:
        _profiling_agent = build_profiling_agent()
    return _profiling_agent


def run_client_profiling(client_id: str, store: Any) -> Dict[str, Any]:
    from app.services.telemetry import span

    with span("agent.client_profiling", client_id=client_id):
        final_state = get_profiling_agent().invoke({"client_id": client_id, "store": store})
    return final_state["result"]
