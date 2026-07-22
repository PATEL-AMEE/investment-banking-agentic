"""Policy copilot agent — a LangGraph ``StateGraph`` over a message exchange.

guard_input → retrieve_citations → generate_answer
        ↘ refuse (prompt-injection attempts)

Grounded policy Q&A: queries pass DLP guardrails (prompt-injection screening
+ PII masking), then citations are retrieved from the policy/regulation
corpus via the ``policy_search`` tool and an answer is composed. The
generation node is the Azure OpenAI integration point (Phase 4).
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.base import default_registry
from app.services.dlp import guard_prompt


class CopilotState(TypedDict, total=False):
    # inputs
    query: str
    store: Any
    # guardrails
    guard: Dict[str, Any]
    # conversation trace
    messages: List[Dict[str, str]]
    # intermediate
    citations: List[Dict[str, Any]]
    # output
    result: Dict[str, Any]


def _guard_input(state: CopilotState) -> Dict[str, Any]:
    guard = guard_prompt(state["query"])
    return {
        "guard": guard,
        "query": guard["sanitised"],
        "messages": [{"role": "user", "content": guard["sanitised"]}],
    }


def _route_after_guard(state: CopilotState) -> str:
    return "retrieve_citations" if state["guard"]["allowed"] else "refuse"


def _refuse(state: CopilotState) -> Dict[str, Any]:
    answer = "This request was blocked by prompt guardrails and has not been processed."
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": answer}],
        "result": {"answer": answer, "citations": [], "guardrails": state["guard"]["flags"]},
    }


def _retrieve_citations(state: CopilotState) -> Dict[str, Any]:
    citations = default_registry.call("policy_search", store=state["store"], query=state["query"])
    return {"citations": citations}


def _generate_answer(state: CopilotState) -> Dict[str, Any]:
    answer = f"Policy guidance for: {state['query']}"
    result: Dict[str, Any] = {"answer": answer, "citations": state["citations"]}
    if state["guard"]["flags"]:
        result["guardrails"] = state["guard"]["flags"]
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": answer}],
        "result": result,
    }


def build_copilot_agent():
    graph = StateGraph(CopilotState)
    graph.add_node("guard_input", _guard_input)
    graph.add_node("refuse", _refuse)
    graph.add_node("retrieve_citations", _retrieve_citations)
    graph.add_node("generate_answer", _generate_answer)

    graph.add_edge(START, "guard_input")
    graph.add_conditional_edges("guard_input", _route_after_guard, ["retrieve_citations", "refuse"])
    graph.add_edge("retrieve_citations", "generate_answer")
    graph.add_edge("generate_answer", END)
    graph.add_edge("refuse", END)
    return graph.compile()


_copilot_agent = None


def get_copilot_agent():
    global _copilot_agent
    if _copilot_agent is None:
        _copilot_agent = build_copilot_agent()
    return _copilot_agent


def run_copilot(query: str, store: Any) -> Dict[str, Any]:
    final_state = get_copilot_agent().invoke({"query": query, "store": store})
    return final_state["result"]
