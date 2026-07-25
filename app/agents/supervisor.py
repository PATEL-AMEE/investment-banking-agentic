"""Supervisor agent — a LangGraph ``StateGraph`` that routes and aggregates.

guard_input → classify_intents → enforce_rbac → [worker fan-out] → aggregate
        ↘ refuse (prompt-injection attempts)

The supervisor is the single natural-language entry point to the platform
(``POST /api/agents/ask``). It screens the request through DLP guardrails,
classifies which specialised worker agents should handle it, enforces
per-intent RBAC centrally, dispatches the surviving intents to the workers
**over MCP ``tools/call``** (so every hop is schema-validated and audited),
runs the selected workers in parallel LangGraph branches, and aggregates
their outputs into one answer with per-agent sections.

Every routing decision lands on the signed audit trail
(``supervisor_route``) and is published on the event bus
(``agents.supervisor.routed``).
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

from app.mcp.client import MCPClient
from app.services import rbac
from app.services.dlp import guard_prompt
from app.services.event_bus import TOPIC_SUPERVISOR_ROUTED, event_bus

AGENT_ID = "AGENT_SUPERVISOR_001"

# Intent -> keyword patterns (rule-based classifier; deterministic/offline).
_INTENT_PATTERNS: Dict[str, re.Pattern[str]] = {
    "sanctions_check": re.compile(r"sanction|watchlist|embargo|\bpep\b|politically exposed", re.IGNORECASE),
    "compliance_inspect": re.compile(r"compl(y|iance|iant)|mifid|\baml\b|due diligence|disclosure|regulat", re.IGNORECASE),
    "client_profile": re.compile(r"\bprofile\b|\b360\b|risk (summary|overview)", re.IGNORECASE),
    "nlp_analyze": re.compile(r"entit(y|ies)|clause|classif(y|ication)|extract", re.IGNORECASE),
}

# Central RBAC: each intent maps to a permission in the platform policy
# (:mod:`app.services.rbac`). ``roles=None`` marks the anonymous dev path
# (no auth configured) and bypasses the check, mirroring the API layer's
# ``_is_anonymous_dev`` behaviour.
_INTENT_PERMISSION: Dict[str, str] = {
    "compliance_inspect": rbac.PERM_COMPLIANCE_INSPECT,
    "sanctions_check": rbac.PERM_SANCTIONS_CHECK,
    "client_profile": rbac.PERM_CLIENT_PROFILE,
    "nlp_analyze": rbac.PERM_NLP_ANALYZE,
    "copilot_query": rbac.PERM_COPILOT_QUERY,
}

_WORKER_NODE = {
    "compliance_inspect": "run_compliance",
    "sanctions_check": "run_sanctions",
    "client_profile": "run_profile",
    "nlp_analyze": "run_nlp",
    "copilot_query": "run_copilot",
}


class SupervisorState(TypedDict, total=False):
    # inputs
    query: str
    store: Any
    user_id: str
    roles: List[str] | None  # None => anonymous dev (RBAC bypass)
    client_id: str | None
    client_name: str | None
    jurisdiction: str
    text: str | None
    request_id: str
    # guardrails
    guard: Dict[str, Any]
    # routing
    intents: List[str]
    intent_args: Dict[str, Dict[str, Any]]
    allowed_intents: List[str]
    denied_intents: List[str]
    routing_notes: List[str]
    # worker outputs (one key per worker: parallel branches never collide)
    compliance_result: Dict[str, Any]
    sanctions_result: Dict[str, Any]
    profile_result: Dict[str, Any]
    nlp_result: Dict[str, Any]
    copilot_result: Dict[str, Any]
    # output
    result: Dict[str, Any]


# ------------------------------------------------------------------ guardrail
def _guard_input(state: SupervisorState) -> Dict[str, Any]:
    guard = guard_prompt(state["query"])
    return {"guard": guard, "query": guard["sanitised"]}


def _route_after_guard(state: SupervisorState) -> str:
    return "classify_intents" if state["guard"]["allowed"] else "refuse"


def _refuse(state: SupervisorState) -> Dict[str, Any]:
    answer = "This request was blocked by prompt guardrails and has not been routed to any agent."
    return {
        "result": {
            "answer": answer,
            "routed_to": [],
            "sections": {},
            "guardrails": state["guard"]["flags"],
            "request_id": state["request_id"],
        }
    }


# ----------------------------------------------------------------- classifier
def _classify_intents(state: SupervisorState) -> Dict[str, Any]:
    query = state["query"]
    notes: List[str] = []
    intents: List[str] = [name for name, pattern in _INTENT_PATTERNS.items() if pattern.search(query)]
    args: Dict[str, Dict[str, Any]] = {}

    if "compliance_inspect" in intents:
        if state.get("client_id"):
            args["compliance_inspect"] = {
                "client_id": state["client_id"],
                "tx_data": {"query": query},
                "request_id": state["request_id"],
            }
        else:
            # A regulatory question without a client in scope is grounded
            # Q&A, not a client inspection — downgrade to the copilot.
            intents.remove("compliance_inspect")
            notes.append("compliance_inspect downgraded to copilot_query (no client_id supplied)")

    if "sanctions_check" in intents:
        client_name = state.get("client_name")
        if not client_name and state.get("client_id"):
            client = state["store"].get_client(state["client_id"]) or {}
            client_name = client.get("name")
        if client_name:
            args["sanctions_check"] = {
                "client_name": client_name,
                "client_country": state.get("jurisdiction") or "GB",
            }
        else:
            intents.remove("sanctions_check")
            notes.append("sanctions_check skipped (no client_name or client_id supplied)")

    if "client_profile" in intents:
        if state.get("client_id"):
            args["client_profile"] = {"client_id": state["client_id"]}
        else:
            intents.remove("client_profile")
            notes.append("client_profile skipped (no client_id supplied)")

    if "nlp_analyze" in intents:
        if state.get("text"):
            args["nlp_analyze"] = {"text": state["text"]}
        else:
            intents.remove("nlp_analyze")
            notes.append("nlp_analyze skipped (no document text supplied)")

    # The copilot answers whenever nothing else can, and also alongside a
    # sanctions/profile hop when the query is phrased as a question.
    if not intents:
        intents.append("copilot_query")
        notes.append("no specialised intent matched; defaulting to grounded copilot Q&A")
    if "copilot_query" in intents:
        args["copilot_query"] = {"query": query}

    return {"intents": intents, "intent_args": args, "routing_notes": notes}


# ----------------------------------------------------------------------- rbac
def _enforce_rbac(state: SupervisorState) -> Dict[str, Any]:
    """Gateway check before any worker/LLM runs: role → permitted intents.

    Each per-intent decision — allowed or denied — is written to the signed
    audit trail (``rbac_check``) via :func:`app.services.rbac.check_permission`.
    """
    roles = state.get("roles")
    if roles is None:  # anonymous dev — no auth configured
        return {"allowed_intents": state["intents"], "denied_intents": []}
    allowed: List[str] = []
    denied: List[str] = []
    for intent in state["intents"]:
        permission = _INTENT_PERMISSION.get(intent, rbac.PERM_AGENTS_ASK)
        ok = rbac.check_permission(
            state.get("user_id", "user-unknown"),
            list(roles),
            permission,
            resource=f"intent:{intent}",
        )
        (allowed if ok else denied).append(intent)
    return {"allowed_intents": allowed, "denied_intents": denied}


def _route_to_workers(state: SupervisorState) -> List[str]:
    targets = [_WORKER_NODE[intent] for intent in state["allowed_intents"]]
    return targets or ["aggregate"]


# -------------------------------------------------------------------- workers
def _call_tool(state: SupervisorState, tool: str) -> Dict[str, Any]:
    """One audited MCP ``tools/call`` hop to a worker agent."""
    client = MCPClient(AGENT_ID, store=state["store"])
    try:
        return {"output": client.call_tool(tool, state["intent_args"][tool])}
    except (ValueError, RuntimeError) as exc:
        return {"output": {"error": str(exc)}, "failed": True}


def _run_compliance(state: SupervisorState) -> Dict[str, Any]:
    return {"compliance_result": _call_tool(state, "compliance_inspect")}


def _run_sanctions(state: SupervisorState) -> Dict[str, Any]:
    return {"sanctions_result": _call_tool(state, "sanctions_check")}


def _run_profile(state: SupervisorState) -> Dict[str, Any]:
    return {"profile_result": _call_tool(state, "client_profile")}


def _run_nlp(state: SupervisorState) -> Dict[str, Any]:
    return {"nlp_result": _call_tool(state, "nlp_analyze")}


def _run_copilot(state: SupervisorState) -> Dict[str, Any]:
    return {"copilot_result": _call_tool(state, "copilot_query")}


# ------------------------------------------------------------------ aggregate
_RESULT_KEYS = {
    "compliance_inspect": "compliance_result",
    "sanctions_check": "sanctions_result",
    "client_profile": "profile_result",
    "nlp_analyze": "nlp_result",
    "copilot_query": "copilot_result",
}


def _summarise(intent: str, output: Dict[str, Any]) -> str:
    """One human-readable line per worker outcome (arbitration summary)."""
    if "error" in output:
        return f"{intent}: failed ({output['error']})"
    if intent == "compliance_inspect":
        return (
            f"Compliance decision: {output.get('decision', 'unknown')} "
            f"(confidence {output.get('confidence', 0):.2f}, review required: {output.get('reviewRequired', False)})."
        )
    if intent == "sanctions_check":
        status = "MATCH FOUND" if output.get("is_sanctioned") else "clear"
        return f"Sanctions screening: {status} against {output.get('screened_against', 'watchlists')}."
    if intent == "client_profile":
        return (
            f"Client profile: {output.get('name', output.get('client_id', 'unknown'))} — "
            f"risk {output.get('risk_band', 'unknown')} ({output.get('risk_score', 0):.2f})."
        )
    if intent == "nlp_analyze":
        return (
            f"NLP analysis: {len(output.get('entities', []))} entities, "
            f"{len(output.get('clauses', []))} clauses, class '{output.get('classification', {}).get('label', 'n/a')}'."
        )
    return str(output.get("answer", ""))


def _aggregate(state: SupervisorState) -> Dict[str, Any]:
    from app.services.audit import audit_log

    sections: Dict[str, Any] = {}
    summary_lines: List[str] = []
    for intent in state["allowed_intents"]:
        wrapped = state.get(_RESULT_KEYS[intent])
        if not wrapped:
            continue
        output = wrapped["output"]
        sections[intent] = output
        summary_lines.append(_summarise(intent, output))

    # The copilot's grounded answer is the primary narrative when present;
    # otherwise the arbitrated worker summaries form the answer.
    copilot = sections.get("copilot_query", {})
    answer = copilot.get("answer") or " ".join(summary_lines)
    if not answer:
        answer = "No agent was able to process this request." + (
            " Access denied for: " + ", ".join(state["denied_intents"]) + "." if state["denied_intents"] else ""
        )

    result: Dict[str, Any] = {
        "answer": answer,
        "routed_to": state["allowed_intents"],
        "sections": sections,
        "request_id": state["request_id"],
    }
    if copilot.get("citations"):
        result["citations"] = copilot["citations"]
    # Surface the copilot's scope boundary at the top level so the UI/caller
    # sees it without digging into sections.
    if copilot.get("out_of_scope"):
        result["out_of_scope"] = True
    if state["denied_intents"]:
        result["denied"] = state["denied_intents"]
        result["all_denied"] = not state["allowed_intents"]
    if state["routing_notes"]:
        result["routing_notes"] = state["routing_notes"]
    # Merge the supervisor's own guard flags with the copilot's (e.g.
    # ``out_of_scope``) so every guardrail action shows on one list.
    guard_flags = list(state["guard"]["flags"])
    for flag in copilot.get("guardrails", []):
        if flag not in guard_flags:
            guard_flags.append(flag)
    if guard_flags:
        result["guardrails"] = guard_flags

    audit_log.record(
        event_type="supervisor_route",
        actor_id=AGENT_ID,
        action="route_and_aggregate",
        result="denied" if state["denied_intents"] and not state["allowed_intents"] else "success",
        request_id=state["request_id"],
        metadata={
            "routed_to": ",".join(state["allowed_intents"]) or "none",
            "denied": ",".join(state["denied_intents"]) or "none",
            "user_id": state.get("user_id", "user-unknown"),
        },
    )
    event_bus.publish(
        TOPIC_SUPERVISOR_ROUTED,
        {
            "request_id": state["request_id"],
            "routed_to": state["allowed_intents"],
            "denied": state["denied_intents"],
            "actor": AGENT_ID,
        },
    )
    return {"result": result}


# ---------------------------------------------------------------------- graph
def build_supervisor_agent():
    graph = StateGraph(SupervisorState)
    graph.add_node("guard_input", _guard_input)
    graph.add_node("refuse", _refuse)
    graph.add_node("classify_intents", _classify_intents)
    graph.add_node("enforce_rbac", _enforce_rbac)
    graph.add_node("run_compliance", _run_compliance)
    graph.add_node("run_sanctions", _run_sanctions)
    graph.add_node("run_profile", _run_profile)
    graph.add_node("run_nlp", _run_nlp)
    graph.add_node("run_copilot", _run_copilot)
    graph.add_node("aggregate", _aggregate)

    graph.add_edge(START, "guard_input")
    graph.add_conditional_edges("guard_input", _route_after_guard, ["classify_intents", "refuse"])
    graph.add_edge("classify_intents", "enforce_rbac")
    # Fan-out: the router returns every worker node the request needs; the
    # selected workers run as parallel branches converging on ``aggregate``.
    graph.add_conditional_edges(
        "enforce_rbac",
        _route_to_workers,
        [*sorted(set(_WORKER_NODE.values())), "aggregate"],
    )
    for node in sorted(set(_WORKER_NODE.values())):
        graph.add_edge(node, "aggregate")
    graph.add_edge("aggregate", END)
    graph.add_edge("refuse", END)
    return graph.compile()


_supervisor_agent = None


def get_supervisor_agent():
    global _supervisor_agent
    if _supervisor_agent is None:
        _supervisor_agent = build_supervisor_agent()
    return _supervisor_agent


def run_supervisor(
    query: str,
    store: Any,
    *,
    user_id: str = "user-unknown",
    roles: List[str] | None = None,
    client_id: str | None = None,
    client_name: str | None = None,
    jurisdiction: str = "GB",
    text: str | None = None,
    request_id: str | None = None,
) -> Dict[str, Any]:
    from app.services.telemetry import current_trace_id, langgraph_config, span

    request_id = request_id or f"SUP-{uuid.uuid4().hex[:8]}"
    with span("agent.supervisor", request_id=request_id, user_id=user_id):
        # Captured inside the span so it's the request's real trace id — the
        # same id stamped on every audit event this request emits.
        trace_id = current_trace_id()
        final_state = get_supervisor_agent().invoke(
            {
                "query": query,
                "store": store,
                "user_id": user_id,
                "roles": roles,
                "client_id": client_id,
                "client_name": client_name,
                "jurisdiction": jurisdiction,
                "text": text,
                "request_id": request_id,
            },
            # Name the top-level LangSmith trace as the business operation it is
            # (a compliance request being triaged/routed across the agent tools);
            # sub-agents invoked inside this call nest underneath it as children.
            # Also correlates with the Azure Monitor trace via shared ids (no-op
            # when LangSmith is disabled).
            config=langgraph_config("compliance_triage", request_id=request_id, trace_id=trace_id, user_id=user_id),
        )
    result = final_state["result"]
    if isinstance(result, dict):
        result.setdefault("requestId", request_id)
        result.setdefault("traceId", trace_id)
    return result
