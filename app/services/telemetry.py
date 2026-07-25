"""Observability: OpenTelemetry tracing + LLM token/cost accounting.

Every agent run, tool call, and LLM request becomes an OpenTelemetry span.
Spans are kept in a bounded in-memory ring for the ``/api/telemetry``
endpoints; set ``OTEL_CONSOLE=true`` to also print spans, and swap the
exporter for Azure Monitor (``azure-monitor-opentelemetry``) when deployed.

Token usage from the LLM adapter is aggregated into running counters so
cost per model is visible without a external APM.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor, SpanExporter, SpanExportResult


class _RingExporter(SpanExporter):
    """Keeps the most recent spans in memory for the telemetry API."""

    def __init__(self, limit: int = 500) -> None:
        self._spans: List[Dict[str, Any]] = []
        self._limit = limit
        self._lock = threading.Lock()

    def export(self, spans: List[ReadableSpan]) -> SpanExportResult:
        with self._lock:
            for span in spans:
                duration_ms = None
                if span.end_time and span.start_time:
                    duration_ms = round((span.end_time - span.start_time) / 1_000_000, 2)
                self._spans.append(
                    {
                        "name": span.name,
                        "trace_id": format(span.context.trace_id, "032x"),
                        "span_id": format(span.context.span_id, "016x"),
                        "parent_id": format(span.parent.span_id, "016x") if span.parent else None,
                        "duration_ms": duration_ms,
                        # Wall-clock bounds (ns since epoch) — kept so a trace can
                        # be reassembled into a waterfall (offset of each span
                        # relative to its trace root).
                        "start_ns": span.start_time,
                        "end_ns": span.end_time,
                        "attributes": dict(span.attributes or {}),
                        "status": span.status.status_code.name,
                    }
                )
            if len(self._spans) > self._limit:
                self._spans = self._spans[-self._limit :]
        return SpanExportResult.SUCCESS

    def recent(self, limit: int = 100) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._spans[-limit:])

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._spans)


_ring = _RingExporter()
_provider = TracerProvider(resource=Resource.create({"service.name": "ib-agentic-platform"}))
_provider.add_span_processor(SimpleSpanProcessor(_ring))
if os.getenv("OTEL_CONSOLE", "").lower() == "true":
    _provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))

# Azure Monitor / Application Insights export (cloud observability).
# Enabled automatically when APPLICATIONINSIGHTS_CONNECTION_STRING is set
# (Azure Container Apps deployment); local runs stay local-only.
_appinsights = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
if _appinsights:
    try:
        from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        _provider.add_span_processor(
            BatchSpanProcessor(AzureMonitorTraceExporter(connection_string=_appinsights))
        )
    except Exception:  # exporter not installed / bad connection string
        pass

trace.set_tracer_provider(_provider)

tracer = trace.get_tracer("ib.agents")


def instrument_fastapi(app: Any) -> None:
    """Record each incoming HTTP request as a **server** span.

    Without this the app only emits internal spans (agent/tool/LLM), which land
    in Application Insights' ``dependencies`` table — so the request-based
    dashboards (Overview, Performance, Application Map) show nothing. This makes
    HTTP requests appear in the ``requests`` table with the internal spans
    nested under them. Health-probe URLs are excluded so they don't flood the
    trace store. Best-effort: a missing/incompatible package never breaks boot.
    """
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, tracer_provider=_provider, excluded_urls="health")
    except Exception:
        pass


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Convenience wrapper: ``with span("agent.onboarding", client_id=...)``."""
    with tracer.start_as_current_span(name) as current:
        for key, value in attributes.items():
            if value is not None:
                current.set_attribute(key, value)
        yield current


def recent_spans(limit: int = 100) -> List[Dict[str, Any]]:
    return _ring.recent(limit)


def langsmith_enabled() -> bool:
    """Whether LangSmith agent-graph tracing is switched on (env-gated, off by
    default). Enable with ``LANGSMITH_TRACING=true`` + ``LANGSMITH_API_KEY``;
    point ``LANGSMITH_ENDPOINT`` at a self-hosted/BYOC instance so trace data
    stays inside the bank's own environment. LangGraph exports automatically —
    this flag is only for reporting/labelling."""
    return os.getenv("LANGSMITH_TRACING", os.getenv("LANGCHAIN_TRACING_V2", "")).lower() == "true"


def langgraph_config(run_name: str, **metadata: Any) -> Dict[str, Any]:
    """Config for a LangGraph ``.invoke()`` that names the run and attaches the
    request's ids as LangSmith metadata/tags. This makes the agent-graph trace
    in LangSmith carry the *same* ``request_id``/``trace_id`` as the Azure
    Monitor infrastructure trace, so one can be pivoted to the other. Harmless
    when LangSmith is disabled — the metadata is simply never exported.
    """
    meta = {key: value for key, value in metadata.items() if value is not None}
    return {"run_name": run_name, "tags": ["ib-agentic", run_name], "metadata": meta}


def current_trace_id() -> str | None:
    """The active request's trace id (32-hex), or ``None`` outside any span.

    This is the single id shared by the trace and the audit trail, so an
    engineer can pivot from a compliance audit entry to the performance
    waterfall for the very same request, and back.
    """
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


def _root_of(spans: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The trace root: the parentless span, else the earliest-started one."""
    parentless = [s for s in spans if s["parent_id"] is None]
    pool = parentless or spans
    return min(pool, key=lambda s: s.get("start_ns") or 0)


def list_traces(limit: int = 25) -> List[Dict[str, Any]]:
    """Recent distinct traces, newest first — the 'recent requests' list.

    One row per trace: its root operation, wall-clock span, how many spans it
    fanned out into, and whether any span errored.
    """
    by_trace: Dict[str, List[Dict[str, Any]]] = {}
    for s in _ring.snapshot():
        by_trace.setdefault(s["trace_id"], []).append(s)

    traces: List[Dict[str, Any]] = []
    for trace_id, spans in by_trace.items():
        root = _root_of(spans)
        starts = [s["start_ns"] for s in spans if s.get("start_ns") is not None]
        ends = [s["end_ns"] for s in spans if s.get("end_ns") is not None]
        total_ms = round((max(ends) - min(starts)) / 1_000_000, 2) if starts and ends else root.get("duration_ms")
        traces.append(
            {
                "trace_id": trace_id,
                "root": root["name"],
                "span_count": len(spans),
                "total_ms": total_ms,
                "status": "ERROR" if any(s["status"] == "ERROR" for s in spans) else "OK",
                "started_at_ns": min(starts) if starts else None,
                "request_id": root.get("attributes", {}).get("request_id"),
            }
        )
    traces.sort(key=lambda t: t.get("started_at_ns") or 0, reverse=True)
    return traces[:limit]


def get_trace(trace_id: str) -> Dict[str, Any]:
    """Waterfall for one request: every span with its offset from the root.

    Spans are ordered by start time and annotated with ``offset_ms`` (how long
    after the request began this step started) and ``depth`` (nesting level),
    which is exactly what a waterfall chart renders.
    """
    spans = [s for s in _ring.snapshot() if s["trace_id"] == trace_id]
    if not spans:
        return {"trace_id": trace_id, "found": False, "spans": []}

    root = _root_of(spans)
    base = root.get("start_ns") or 0
    by_id = {s["span_id"]: s for s in spans}

    def depth(s: Dict[str, Any]) -> int:
        level, parent = 0, s["parent_id"]
        # Walk up the parent chain within this trace (guard against cycles).
        while parent is not None and parent in by_id and level < len(spans):
            level += 1
            parent = by_id[parent]["parent_id"]
        return level

    ordered = sorted(spans, key=lambda s: s.get("start_ns") or 0)
    waterfall = [
        {
            "name": s["name"],
            "span_id": s["span_id"],
            "parent_id": s["parent_id"],
            "depth": depth(s),
            "offset_ms": round(((s.get("start_ns") or base) - base) / 1_000_000, 2),
            "duration_ms": s["duration_ms"],
            "status": s["status"],
            "attributes": s["attributes"],
        }
        for s in ordered
    ]
    starts = [s["start_ns"] for s in spans if s.get("start_ns") is not None]
    ends = [s["end_ns"] for s in spans if s.get("end_ns") is not None]
    total_ms = round((max(ends) - min(starts)) / 1_000_000, 2) if starts and ends else root.get("duration_ms")
    return {
        "trace_id": trace_id,
        "found": True,
        "root": root["name"],
        "total_ms": total_ms,
        "span_count": len(spans),
        "status": "ERROR" if any(s["status"] == "ERROR" for s in spans) else "OK",
        "spans": waterfall,
    }


# ------------------------------------------------------------- LLM accounting
class LLMUsage:
    """Aggregate token usage/latency per model (cost visibility)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_model: Dict[str, Dict[str, Any]] = {}

    def record(self, model: str, prompt_tokens: int, completion_tokens: int, latency_ms: float) -> None:
        with self._lock:
            stats = self._by_model.setdefault(
                model,
                {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_latency_ms": 0.0},
            )
            stats["calls"] += 1
            stats["prompt_tokens"] += prompt_tokens
            stats["completion_tokens"] += completion_tokens
            stats["total_latency_ms"] += latency_ms

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            models = {}
            for model, stats in self._by_model.items():
                models[model] = {
                    **stats,
                    "total_latency_ms": round(stats["total_latency_ms"], 1),
                    "avg_latency_ms": round(stats["total_latency_ms"] / stats["calls"], 1) if stats["calls"] else 0,
                    "total_tokens": stats["prompt_tokens"] + stats["completion_tokens"],
                }
            return {
                "models": models,
                "total_calls": sum(s["calls"] for s in models.values()),
                "total_tokens": sum(s["total_tokens"] for s in models.values()),
            }


llm_usage = LLMUsage()
