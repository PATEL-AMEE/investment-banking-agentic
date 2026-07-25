"""Policy copilot agent — a LangGraph ``StateGraph`` over a message exchange.

guard_input → retrieve_citations → generate_answer
        ↘ refuse (prompt-injection attempts)   ↘ out_of_scope (off-domain query)

Grounded policy Q&A: queries pass DLP guardrails (prompt-injection screening
+ PII masking), then citations are retrieved from the policy/regulation
corpus via the MCP ``policy_search`` tool (inter-agent ``tools/call``, so
the retrieval hop is schema-validated and audited) and an answer is
composed. The generation node is the Azure OpenAI integration point.

A scope boundary sits between retrieval and generation: if nothing in the
bank's knowledge base matches, the copilot never guesses — it returns a
branded refusal, so a general-knowledge question ("what is the capital of the
UK?") gets "I'm the Barclays compliance copilot, I can't help with that"
rather than a hallucinated answer.
"""
from __future__ import annotations

from typing import Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

from app.mcp.client import MCPClient
from app.services.dlp import guard_prompt
from app.services.llm_adapter import LLMAdapter

# The assistant's identity + scope. Keep the brand in one place.
ASSISTANT_NAME = "Barclays compliance copilot"
OUT_OF_SCOPE_MESSAGE = (
    f"I'm the {ASSISTANT_NAME}. I can only answer questions about the bank's "
    "regulatory policies and compliance procedures, so I can't help with that. "
    "Try asking about AML, KYC, sanctions, or a specific policy (e.g. POL-AML-01)."
)
# The grounding sentence the LLM is told to emit when no source addresses the
# question (see llm_adapter's system prompt). We treat it as "off-domain" and
# swap in the branded refusal above.
_NO_ANSWER_SENTINEL = "the retrieved sources do not answer this question"


def _looks_off_domain(answer: str) -> bool:
    """True when the grounded model signalled it has nothing relevant to say."""
    return _NO_ANSWER_SENTINEL in (answer or "").strip().lower()


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
        # ``blocked`` marks the guardrail refusal so it can be told apart from an
        # out-of-scope refusal and a real answer when recording the outcome.
        "result": {"answer": answer, "citations": [], "guardrails": state["guard"]["flags"], "blocked": True},
    }


def _retrieve_citations(state: CopilotState) -> Dict[str, Any]:
    # Inter-agent hop over MCP: the copilot asks the retrieval capability for
    # citations via tools/call instead of importing the retriever directly.
    client = MCPClient("AGENT_COPILOT_001", store=state["store"])
    citations = client.call_tool("policy_search", {"query": state["query"]})
    return {"citations": citations}


def _route_after_retrieval(state: CopilotState) -> str:
    # No citations means nothing in the bank's corpus addresses the question —
    # it's outside the assistant's domain, so refuse rather than generate.
    return "generate_answer" if state.get("citations") else "out_of_scope"


def _out_of_scope(state: CopilotState) -> Dict[str, Any]:
    flags = list(state["guard"]["flags"]) + ["out_of_scope"]
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": OUT_OF_SCOPE_MESSAGE}],
        "result": {"answer": OUT_OF_SCOPE_MESSAGE, "citations": [], "guardrails": flags, "out_of_scope": True},
    }


def _generate_answer(state: CopilotState) -> Dict[str, Any]:
    generation = LLMAdapter().answer_with_citations(state["query"], state["citations"])
    # Loose keyword retrieval can surface citations for an off-topic query
    # (e.g. "capital of the UK" matching a capital-adequacy policy). When the
    # grounded model reports nothing relevant, hold the scope boundary here too.
    if _looks_off_domain(generation["answer"]):
        return _out_of_scope(state)
    # Score groundedness inline with the cheap deterministic proxies (same as
    # the eval harness' lexical engine) so every live answer — not just golden
    # eval runs — carries a faithfulness/relevancy read for the metrics pipeline.
    from app.eval.harness import answer_relevancy, faithfulness

    contexts = [c.get("excerpt", "") for c in state["citations"]]
    faith = faithfulness(generation["answer"], contexts)
    relevancy = answer_relevancy(state["query"], generation["answer"])
    result: Dict[str, Any] = {
        "answer": generation["answer"],
        "citations": state["citations"],
        "generation_mode": generation["mode"],
        "faithfulness": faith,
        "hallucination_rate": round(1 - faith, 4),
        "answer_relevancy": relevancy,
    }
    if state["guard"]["flags"]:
        result["guardrails"] = state["guard"]["flags"]
    return {
        "messages": state["messages"] + [{"role": "assistant", "content": generation["answer"]}],
        "result": result,
    }


def build_copilot_agent():
    graph = StateGraph(CopilotState)
    graph.add_node("guard_input", _guard_input)
    graph.add_node("refuse", _refuse)
    graph.add_node("retrieve_citations", _retrieve_citations)
    graph.add_node("out_of_scope", _out_of_scope)
    graph.add_node("generate_answer", _generate_answer)

    graph.add_edge(START, "guard_input")
    graph.add_conditional_edges("guard_input", _route_after_guard, ["retrieve_citations", "refuse"])
    graph.add_conditional_edges("retrieve_citations", _route_after_retrieval, ["generate_answer", "out_of_scope"])
    graph.add_edge("generate_answer", END)
    graph.add_edge("out_of_scope", END)
    graph.add_edge("refuse", END)
    return graph.compile()


_copilot_agent = None


def get_copilot_agent():
    global _copilot_agent
    if _copilot_agent is None:
        _copilot_agent = build_copilot_agent()
    return _copilot_agent


def run_copilot(query: str, store: Any) -> Dict[str, Any]:
    from app.services.telemetry import (
        current_trace_id,
        langgraph_config,
        record_copilot_outcome,
        record_copilot_quality,
        span,
    )

    with span("agent.copilot") as current:
        final_state = get_copilot_agent().invoke(
            {"query": query, "store": store},
            config=langgraph_config("copilot", trace_id=current_trace_id()),
        )
        result = final_state["result"]
        # Every request returns HTTP 200 whether it answered, refused, or fell
        # back to the stub, so the outcome has to be recorded explicitly for a
        # refusal spike or a retrieval regression to be visible in monitoring.
        if result.get("blocked"):
            outcome = "blocked"
        elif result.get("out_of_scope"):
            outcome = "out_of_scope"
        else:
            outcome = "answered"
        citation_count = len(result.get("citations") or [])
        generation_mode = result.get("generation_mode", "none")
        record_copilot_outcome(outcome, citation_count, generation_mode)
        current.set_attribute("copilot.outcome", outcome)
        current.set_attribute("copilot.citation_count", citation_count)
        current.set_attribute("copilot.generation_mode", generation_mode)
        # Answered requests carry an inline groundedness score — emit it so
        # faithfulness/hallucination trend continuously in Azure Monitor.
        if outcome == "answered" and "faithfulness" in result:
            record_copilot_quality(result["faithfulness"], result["answer_relevancy"], generation_mode)
            current.set_attribute("copilot.faithfulness", result["faithfulness"])
            current.set_attribute("copilot.answer_relevancy", result["answer_relevancy"])
    return result
