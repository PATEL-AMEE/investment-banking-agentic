"""Copilot scope boundary: off-domain questions get a branded refusal instead
of a guessed answer. Deterministic — MCP retrieval and the LLM are stubbed, so
the suite never hits the network.
"""
from __future__ import annotations

from app.agents import copilot
from app.agents.copilot import (
    OUT_OF_SCOPE_MESSAGE,
    _looks_off_domain,
    _out_of_scope,
    _route_after_retrieval,
    run_copilot,
)


class _FakeMCP:
    """Stand-in for MCPClient that returns a fixed citation list."""

    def __init__(self, citations):
        self._citations = citations

    def call_tool(self, *args, **kwargs):
        return self._citations


# ------------------------------------------------------------------ unit level
def test_looks_off_domain_detects_the_grounding_sentinel():
    assert _looks_off_domain("The retrieved sources do not answer this question.")
    assert _looks_off_domain("  the retrieved sources do not answer this question  ")
    assert not _looks_off_domain("Enhanced due diligence required [POL-AML-01].")
    assert not _looks_off_domain("")


def test_route_gates_on_whether_anything_was_retrieved():
    assert _route_after_retrieval({"citations": []}) == "out_of_scope"
    assert _route_after_retrieval({"citations": [{"source_id": "POL-AML-01"}]}) == "generate_answer"


def test_out_of_scope_node_returns_branded_refusal():
    out = _out_of_scope({"guard": {"flags": []}, "messages": []})
    assert out["result"]["answer"] == OUT_OF_SCOPE_MESSAGE
    assert out["result"]["out_of_scope"] is True
    assert "out_of_scope" in out["result"]["guardrails"]
    assert out["result"]["citations"] == []
    assert "Barclays" in OUT_OF_SCOPE_MESSAGE


# ----------------------------------------------------------- through the graph
def test_off_topic_with_no_citations_is_refused(monkeypatch):
    monkeypatch.setattr(copilot, "MCPClient", lambda *a, **k: _FakeMCP([]))
    result = run_copilot("What is the capital of the UK?", store=None)
    assert result["out_of_scope"] is True
    assert result["answer"] == OUT_OF_SCOPE_MESSAGE
    assert result["citations"] == []


def test_off_topic_with_loose_citations_is_still_refused(monkeypatch):
    # Retrieval keyword-matches a weak citation, but the grounded model reports
    # nothing relevant — the scope boundary must still hold.
    monkeypatch.setattr(copilot, "MCPClient", lambda *a, **k: _FakeMCP([{"source_id": "POL-CAP-01", "excerpt": "capital adequacy"}]))
    monkeypatch.setattr(
        copilot.LLMAdapter,
        "answer_with_citations",
        lambda self, q, c: {"answer": "The retrieved sources do not answer this question.", "mode": "stub"},
    )
    result = run_copilot("What is the capital of the UK?", store=None)
    assert result["out_of_scope"] is True
    assert result["answer"] == OUT_OF_SCOPE_MESSAGE


def test_on_topic_question_is_answered_normally(monkeypatch):
    monkeypatch.setattr(copilot, "MCPClient", lambda *a, **k: _FakeMCP([{"source_id": "POL-AML-01", "excerpt": "EDD"}]))
    monkeypatch.setattr(
        copilot.LLMAdapter,
        "answer_with_citations",
        lambda self, q, c: {"answer": "Enhanced due diligence required [POL-AML-01].", "mode": "stub"},
    )
    result = run_copilot("enhanced due diligence for high-risk clients", store=None)
    assert result.get("out_of_scope") is None
    assert "POL-AML-01" in result["answer"]
    assert len(result["citations"]) == 1
