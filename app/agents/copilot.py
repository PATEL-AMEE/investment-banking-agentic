"""Policy copilot agent — a LangGraph ``StateGraph`` over a message exchange.

retrieve_citations → generate_answer

Grounded policy Q&A: retrieves citations from the policy/regulation corpus
via the ``policy_search`` tool, then composes an answer. The generation node
is the Azure OpenAI integration point (Phase 4); until then it produces the
deterministic grounded answer the API has always returned.
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agents.base import default_registry


class CopilotState(TypedDict, total=False):
    # inputs
    query: str
    store: Any
    # conversation trace
    messages: List[Dict[str, str]]
    # intermediate
    citations: List[Dict[str, Any]]
    # output
    result: Dict[str, Any]


def _retrieve_citations(state: CopilotState) -> Dict[str, Any]:
    citations = default_registry.call("policy_search", store=state["store"], query=state["query"])
    return {
        "citations": citations,
        "messages": [{"role": "user", "content": state["query"]}],
    }


def _generate_answer(state: CopilotState) -> Dict[str, Any]:
    answer = f"Policy guidance for: {state['query']}"
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": answer}],
        "result": {"answer": answer, "citations": state["citations"]},
    }


def build_copilot_agent():
    graph = StateGraph(CopilotState)
    graph.add_node("retrieve_citations", _retrieve_citations)
    graph.add_node("generate_answer", _generate_answer)

    graph.add_edge(START, "retrieve_citations")
    graph.add_edge("retrieve_citations", "generate_answer")
    graph.add_edge("generate_answer", END)
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
