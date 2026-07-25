"""Observability: OpenTelemetry tracing + metrics + LLM token/cost accounting.

Every agent run, tool call, and LLM request becomes an OpenTelemetry span.
Spans are kept in a bounded in-memory ring for the ``/api/telemetry``
endpoints; set ``OTEL_CONSOLE=true`` to also print spans, and swap the
exporter for Azure Monitor (``azure-monitor-opentelemetry``) when deployed.

Alongside the spans, the ``record_*`` helpers below emit **OpenTelemetry
metrics** (counters/histograms). Metrics matter because the in-memory span
ring is per-process, bounded, and lost on restart — with 2-5 replicas behind
the HPA it only ever shows a fraction of production. Metrics are
pre-aggregated and exported centrally to Azure Monitor as ``customMetrics``,
which is what makes cheap *metric* alerts (rather than log-search alerts)
possible on failure rate, cost, and latency.

Token usage from the LLM adapter is also aggregated into in-process counters
so cost per model is visible on the local telemetry page without an APM.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
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
_RESOURCE = Resource.create({"service.name": "ib-agentic-platform"})
_provider = TracerProvider(resource=_RESOURCE)
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


# ------------------------------------------------------------------- metrics
# Exported to Application Insights ``customMetrics`` on a 60s interval when
# APPLICATIONINSIGHTS_CONNECTION_STRING is set. With no reader configured the
# MeterProvider still accepts recordings, it just never exports them — so the
# record_* helpers below are always safe to call (local runs, tests, CI).
_metric_readers: List[PeriodicExportingMetricReader] = []
if _appinsights:
    try:
        from azure.monitor.opentelemetry.exporter import AzureMonitorMetricExporter

        _metric_readers.append(
            PeriodicExportingMetricReader(
                AzureMonitorMetricExporter(connection_string=_appinsights),
                export_interval_millis=60_000,
            )
        )
    except Exception:  # exporter not installed / bad connection string
        pass

_meter_provider = MeterProvider(resource=_RESOURCE, metric_readers=_metric_readers)
metrics.set_meter_provider(_meter_provider)
_meter = metrics.get_meter("ib.agents")

# One counter per thing worth alerting on. Dimensions (model, provider,
# outcome, …) are attributes so a single alert rule can be split by model.
_llm_calls = _meter.create_counter(
    "llm.calls", unit="1", description="LLM chat completions by outcome (success/error/fallback)"
)
_llm_tokens = _meter.create_counter("llm.tokens", unit="1", description="LLM tokens by kind (prompt/completion)")
_llm_cost = _meter.create_counter("llm.cost_usd", unit="USD", description="Estimated LLM spend")
_llm_latency = _meter.create_histogram("llm.latency", unit="ms", description="LLM call latency")
_guardrail_events = _meter.create_counter(
    "guardrail.events", unit="1", description="DLP/prompt-guardrail hits by flag"
)
_copilot_answers = _meter.create_counter(
    "copilot.answers", unit="1", description="Copilot answers by outcome (answered/out_of_scope/blocked)"
)
_copilot_citations = _meter.create_histogram(
    "copilot.citations", unit="1", description="Citations retrieved per copilot answer"
)
_copilot_faithfulness = _meter.create_histogram(
    "copilot.faithfulness", unit="1", description="Lexical faithfulness of copilot answers (0-1)"
)
_copilot_relevancy = _meter.create_histogram(
    "copilot.answer_relevancy", unit="1", description="Answer relevancy of copilot answers (0-1)"
)


# Indicative Azure OpenAI list prices in USD per 1M tokens, matched on the
# deployment name. These are for *cost visibility and budget alerting*, not
# billing — confirm against your own Azure pricing/agreement, or override with
# LLM_PRICE_INPUT_PER_1M / LLM_PRICE_OUTPUT_PER_1M for the deployment you run.
_MODEL_PRICING_PER_1M: Dict[str, tuple[float, float]] = {
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
}
_DEFAULT_PRICING = (0.15, 0.60)


def _pricing_for(model: str) -> tuple[float, float]:
    """(input, output) USD per 1M tokens for a deployment name."""
    override_in = os.getenv("LLM_PRICE_INPUT_PER_1M")
    override_out = os.getenv("LLM_PRICE_OUTPUT_PER_1M")
    if override_in and override_out:
        try:
            return float(override_in), float(override_out)
        except ValueError:
            pass
    name = (model or "").lower()
    # Longest match first so "gpt-4o-mini" doesn't resolve to "gpt-4o".
    for key in sorted(_MODEL_PRICING_PER_1M, key=len, reverse=True):
        if key in name:
            return _MODEL_PRICING_PER_1M[key]
    return _DEFAULT_PRICING


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Indicative USD cost of one call (see ``_MODEL_PRICING_PER_1M``)."""
    price_in, price_out = _pricing_for(model)
    return (prompt_tokens * price_in + completion_tokens * price_out) / 1_000_000


def record_llm_success(
    model: str, provider: str, prompt_tokens: int, completion_tokens: int, latency_ms: float
) -> None:
    """A completed LLM call: tokens, latency, and estimated cost."""
    dims = {"model": model, "provider": provider}
    cost = estimate_cost_usd(model, prompt_tokens, completion_tokens)
    _llm_calls.add(1, {**dims, "outcome": "success"})
    _llm_tokens.add(prompt_tokens, {**dims, "kind": "prompt"})
    _llm_tokens.add(completion_tokens, {**dims, "kind": "completion"})
    _llm_cost.add(cost, dims)
    _llm_latency.record(latency_ms, {**dims, "outcome": "success"})
    llm_usage.record(model, prompt_tokens, completion_tokens, latency_ms, cost_usd=cost)


def record_llm_failure(
    model: str,
    provider: str,
    latency_ms: float,
    status_code: int | None = None,
    error_type: str = "unknown",
) -> None:
    """A failed LLM call. ``status_code`` separates 429 throttling (capacity —
    raise the deployment's TPM quota) from 5xx/timeouts (provider outage), which
    need different responses and so deserve different alerts."""
    dims = {
        "model": model,
        "provider": provider,
        "error_type": error_type,
        "status_code": str(status_code) if status_code is not None else "none",
    }
    _llm_calls.add(1, {**dims, "outcome": "error"})
    _llm_latency.record(latency_ms, {"model": model, "provider": provider, "outcome": "error"})
    llm_usage.record_error(model, error_type, status_code)


def record_llm_fallback(model: str, provider: str, reason: str) -> None:
    """A request answered by the deterministic stub instead of a model.

    This is the platform's most important signal: on any provider error the LLM
    adapter returns canned text rather than raising, so a compliance answer that
    no model wrote is indistinguishable to the user. ``reason`` is
    ``mock-fallback`` (provider failed) or ``mock`` (no provider configured) —
    the first is an incident, the second is expected in local/CI runs.
    """
    _llm_calls.add(1, {"model": model, "provider": provider, "outcome": "fallback", "reason": reason})
    llm_usage.record_fallback(reason)


def record_guardrail(flags: Iterable[str]) -> None:
    """DLP guardrail outcome for one query (prompt-injection / PII masking)."""
    for flag in flags:
        # PII flags carry the detected type ("pii_masked:email"); keep the type
        # as its own dimension so the counter stays low-cardinality.
        kind, _, detail = flag.partition(":")
        _guardrail_events.add(1, {"flag": kind, "detail": detail or "none"})


def record_copilot_outcome(outcome: str, citation_count: int, generation_mode: str = "unknown") -> None:
    """One copilot answer: whether it answered, and how well-grounded it was.

    Citation count is the production groundedness proxy — an "answered" outcome
    with zero citations should never happen, and a falling mean means retrieval
    has regressed even while every request still returns HTTP 200.
    """
    _copilot_answers.add(1, {"outcome": outcome, "generation_mode": generation_mode})
    _copilot_citations.record(citation_count, {"outcome": outcome})


def record_copilot_quality(faithfulness: float, answer_relevancy: float, generation_mode: str = "unknown") -> None:
    """Inline RAG-quality scoring of one production answer (cheap deterministic
    proxies, same as the eval harness' lexical engine). Exported as metrics so
    faithfulness/hallucination are tracked continuously and alertable — the
    on-demand eval harness only runs against the golden set, but every live
    answer flows through here. hallucination_rate is 1 - faithfulness, so the
    faithfulness histogram alone alerts on both."""
    dims = {"generation_mode": generation_mode}
    _copilot_faithfulness.record(faithfulness, dims)
    _copilot_relevancy.record(answer_relevancy, dims)


def force_flush_metrics(timeout_millis: int = 5_000) -> bool:
    """Push buffered metrics immediately (used by tests and shutdown)."""
    try:
        return _meter_provider.force_flush(timeout_millis=timeout_millis)
    except Exception:
        return False


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

        # Don't trace health probes, static assets, favicon, or the telemetry
        # polling endpoints — they're not requests worth recording and would
        # otherwise flood the trace store and the tracing UI.
        FastAPIInstrumentor.instrument_app(
            app, tracer_provider=_provider, excluded_urls="health,/ready,static,favicon,/api/telemetry"
        )
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

    ``run_name`` is the label the trace shows up under in LangSmith, so it
    should read as a business operation (e.g. ``compliance_triage``). Passing
    this config only to the *top-level* ``.invoke()`` (and letting sub-agents
    run within it) keeps everything nested under one trace rather than
    scattering separate roots. An ``environment`` tag/metadata field
    (``production`` when deployed to Azure, else ``local``) is added so the
    LangSmith project can be filtered to just real deployed traffic.
    """
    meta = {key: value for key, value in metadata.items() if value is not None}
    environment = "production" if os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING") else "local"
    meta["environment"] = environment
    return {"run_name": run_name, "tags": ["ib-agentic", environment, run_name], "metadata": meta}


def current_trace_id() -> str | None:
    """The active request's trace id (32-hex), or ``None`` outside any span.

    This is the single id shared by the trace and the audit trail, so an
    engineer can pivot from a compliance audit entry to the performance
    waterfall for the very same request, and back.
    """
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else None


# Span-name prefixes that mark a trace as real agent-pipeline work (vs. a bare
# static/page/health HTTP request). Used to keep the tracing UI meaningful.
_PIPELINE_PREFIXES = ("agent.", "mcp.", "llm.", "nlp.", "tool.")


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
        # Only surface traces that actually exercised the agent pipeline —
        # skip static-asset serves, page loads, and telemetry polling, which
        # are HTTP-only noise in this "recent requests" view.
        if not any(s["name"].startswith(_PIPELINE_PREFIXES) for s in spans):
            continue
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
    """Aggregate token usage/latency/errors per model (cost visibility).

    In-process only: resets on restart and is per-replica, so it backs the local
    telemetry page. Azure Monitor ``customMetrics`` is the cross-replica,
    durable view — see the ``record_*`` helpers above.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_model: Dict[str, Dict[str, Any]] = {}
        self._fallbacks: Dict[str, int] = {}

    def _stats_for(self, model: str) -> Dict[str, Any]:
        return self._by_model.setdefault(
            model,
            {
                "calls": 0,
                "errors": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_latency_ms": 0.0,
                "cost_usd": 0.0,
                "last_error": None,
            },
        )

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: float,
        cost_usd: float = 0.0,
    ) -> None:
        with self._lock:
            stats = self._stats_for(model)
            stats["calls"] += 1
            stats["prompt_tokens"] += prompt_tokens
            stats["completion_tokens"] += completion_tokens
            stats["total_latency_ms"] += latency_ms
            stats["cost_usd"] += cost_usd

    def record_error(self, model: str, error_type: str, status_code: int | None = None) -> None:
        with self._lock:
            stats = self._stats_for(model)
            stats["errors"] += 1
            stats["last_error"] = f"{error_type}:{status_code}" if status_code else error_type

    def record_fallback(self, reason: str) -> None:
        with self._lock:
            self._fallbacks[reason] = self._fallbacks.get(reason, 0) + 1

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            models = {}
            for model, stats in self._by_model.items():
                calls = stats["calls"]
                attempts = calls + stats["errors"]
                models[model] = {
                    **stats,
                    "total_latency_ms": round(stats["total_latency_ms"], 1),
                    "avg_latency_ms": round(stats["total_latency_ms"] / calls, 1) if calls else 0,
                    "total_tokens": stats["prompt_tokens"] + stats["completion_tokens"],
                    "cost_usd": round(stats["cost_usd"], 4),
                    "error_rate": round(stats["errors"] / attempts, 4) if attempts else 0.0,
                }
            fallbacks = dict(self._fallbacks)
            total_calls = sum(s["calls"] for s in models.values())
            total_errors = sum(s["errors"] for s in models.values())
            degraded = fallbacks.get("mock-fallback", 0)
            return {
                "models": models,
                "total_calls": total_calls,
                "total_errors": total_errors,
                "total_tokens": sum(s["total_tokens"] for s in models.values()),
                "total_cost_usd": round(sum(s["cost_usd"] for s in models.values()), 4),
                "fallbacks": fallbacks,
                # Share of answers served by the stub after a provider failure —
                # the headline "are we silently degraded?" number.
                "degraded_rate": round(degraded / (total_calls + total_errors + degraded), 4)
                if (total_calls + total_errors + degraded)
                else 0.0,
            }


llm_usage = LLMUsage()
