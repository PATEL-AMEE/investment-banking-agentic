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
