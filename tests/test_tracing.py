"""Request-level tracing: one trace id per request, nested spans, a waterfall,
and correlation with the audit trail.

See docs/tracing.md for the design. These assert the code-level guarantees:
tracing and the audit trail are separate systems that share a trace id.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import app, store
from app.agents.supervisor import run_supervisor
from app.services.audit import audit_log
from app.services import telemetry

client = TestClient(app)

# A copilot-routed policy question: fast, needs no client, still fans out
# supervisor -> MCP tools/call -> copilot agent -> LLM, so the trace nests.
_QUERY = "What does POL-AML-01 say about enhanced due diligence?"


def test_current_trace_id_is_none_outside_any_span():
    assert telemetry.current_trace_id() is None


def test_supervisor_run_exposes_a_trace_id():
    result = run_supervisor(_QUERY, store)
    assert result["traceId"]
    assert len(result["traceId"]) == 32  # 128-bit trace id, 32 hex chars
    assert result["requestId"].startswith("SUP-")


def test_spans_share_one_trace_and_nest_under_the_supervisor():
    result = run_supervisor(_QUERY, store)
    trace = telemetry.get_trace(result["traceId"])

    assert trace["found"] is True
    assert trace["root"] == "agent.supervisor"
    assert trace["span_count"] > 1
    # A real hierarchy, not a flat list: at least one span nests below the root.
    assert any(s["depth"] >= 1 for s in trace["spans"])
    # The root itself is at depth 0 and parentless.
    root = next(s for s in trace["spans"] if s["name"] == "agent.supervisor")
    assert root["depth"] == 0
    assert root["parent_id"] is None


def test_waterfall_is_ordered_with_offsets_and_durations():
    result = run_supervisor(_QUERY, store)
    spans = telemetry.get_trace(result["traceId"])["spans"]

    offsets = [s["offset_ms"] for s in spans]
    assert offsets == sorted(offsets)  # ordered by start time
    assert all(s["offset_ms"] >= 0 for s in spans)
    assert all(s["duration_ms"] is not None and s["duration_ms"] >= 0 for s in spans)


def test_audit_events_carry_the_same_trace_id():
    result = run_supervisor(_QUERY, store)
    trace_id = result["traceId"]

    correlated = audit_log.list(trace_id=trace_id)
    assert correlated, "a traced supervisor run should emit audit events"
    assert all(e["trace_id"] == trace_id for e in correlated)


def test_audit_record_outside_a_trace_has_no_trace_id():
    event = audit_log.record("test_event", "ACTOR", "act_without_trace")
    assert event["trace_id"] is None


def test_list_traces_includes_the_recent_run():
    result = run_supervisor(_QUERY, store)
    trace_ids = {t["trace_id"] for t in telemetry.list_traces(limit=50)}
    assert result["traceId"] in trace_ids


# --------------------------------------------------------------- HTTP surface
def test_trace_endpoints_return_waterfall_and_correlated_audit():
    ask = client.post("/api/agents/ask", json={"query": _QUERY}).json()
    trace_id = ask["traceId"]

    resp = client.get(f"/api/telemetry/trace/{trace_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["found"] is True
    assert body["spans"]
    # Over HTTP the FastAPI request span is the trace root, with the supervisor
    # nested under it — so both should appear in the same trace.
    names = [s["name"] for s in body["spans"]]
    assert any("/api/agents/ask" in n for n in names)
    assert "agent.supervisor" in names
    # The correlated compliance record travels with the same id.
    assert "audit_events" in body
    assert all(k in body["spans"][0] for k in ("name", "offset_ms", "duration_ms", "depth"))


def test_trace_endpoint_404s_for_unknown_id():
    resp = client.get("/api/telemetry/trace/" + "0" * 32)
    assert resp.status_code == 404


def test_traces_list_endpoint_lists_recent_requests():
    client.post("/api/agents/ask", json={"query": _QUERY})
    resp = client.get("/api/telemetry/traces")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert body["traces"][0]["root"]
