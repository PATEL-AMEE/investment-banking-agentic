"""Client-profiling agent — a LangGraph ``StateGraph``.

fetch_client → gather_relationships → build_profile

Produces a 360° client view by traversing the knowledge graph: identity,
risk banding, related documents/evidence/policies, and the regulations
applying in the client's home jurisdiction.
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.base import default_registry


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
    return {"documents": documents, "evidence": evidence, "policies": policies, "regulations": regulations}


def _build_profile(state: ProfilingState) -> Dict[str, Any]:
    client = state["client"]
    risk_score = float(client.get("risk_score", 0.0))
    if risk_score > 0.7:
        risk_band = "high"
    elif risk_score >= 0.4:
        risk_band = "medium"
    else:
        risk_band = "low"

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
            "summary": (
                f"{client.get('name')} ({client.get('country')}) is a {risk_band}-risk client "
                f"(score {risk_score:.2f}) with {len(state['documents'])} documents, "
                f"{len(state['evidence'])} evidence items and {len(state['policies'])} applicable policies."
            ),
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
    final_state = get_profiling_agent().invoke({"client_id": client_id, "store": store})
    return final_state["result"]
