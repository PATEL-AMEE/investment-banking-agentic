# Tracing — Enterprise Agentic AI Platform (Barclays Investment Banking)

How request-level tracing works across the multi-agent pipeline, and how it
differs from the audit trail.

## Tracing vs. audit trail — not the same thing

Both "record what happened," but they serve different audiences and
requirements.

| | Audit trail | Tracing |
|---|---|---|
| **Purpose** | Compliance record — what decision was made and why | Engineering visibility — how a request moved through the system, and how fast |
| **Audience** | Regulators, security team, compliance officers | Engineers, SREs, on-call |
| **Format** | Immutable, hash-chained + HMAC-signed log entries | Timestamped spans, short-retention |
| **Answers** | "Why did the agent approve/flag this client?" | "Why was this request slow, or where did it fail?" |
| **Lives in** | `app/services/audit.py` (Phase 7) | `app/services/telemetry.py` (this document) |

They **share the same trace id** for correlation but are separate systems with
separate retention and access rules.

## How a trace works, step by step

1. **Trace starts at entry.** When a staff member asks the Copilot a question
   (or the dashboard triggers a compliance check), `run_supervisor` opens the
   `agent.supervisor` span and a unique 128-bit **trace id** (32 hex chars) is
   generated for that request.
2. **The trace id propagates through every hop.** As the request moves
   — Supervisor → worker agent(s) → MCP `tools/call` (GraphRAG lookup, Azure AI
   Search, NLP pipeline, LLM generation) — the OpenTelemetry context carries the
   trace id with it, including across the supervisor's parallel LangGraph
   branches, so every span lands in the *same* trace.
3. **Each step creates a span.** A span records one unit of work: start, end,
   status, and metadata (which agent, tool, model). Spans nest under the parent
   trace, giving a hierarchy, not a flat list.
4. **Spans are collected centrally.** OpenTelemetry exports them to an in-memory
   ring (for the `/api/telemetry` endpoints) and, when
   `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, to **Azure Monitor /
   Application Insights** for distributed tracing.

The result is a **waterfall view** of one request, e.g.:

```
agent.supervisor .................... 0ms      +1650ms
  mcp.tools/call.compliance_inspect .. 55ms     + 300ms
    agent.compliance ................. 60ms     + 240ms
      tool.graph_retriever (Neo4j) ... 70ms     + 220ms
  mcp.tools/call.copilot_query ....... 55ms     + 900ms
    agent.copilot .................... 60ms     + 890ms
      llm.chat (Azure OpenAI/Vertex) . 120ms    + 780ms
```

This is how you identify that GraphRAG traversal is the bottleneck on complex
regulatory queries, or that a specific agent is timing out.

## Two layers of tracing

1. **Infrastructure-level (OpenTelemetry + Azure Monitor).** The technical path:
   which services were called, how long each took, where errors occurred.
   Powers on-call debugging and performance tuning. Built in
   `app/services/telemetry.py`; every agent, tool, MCP hop, NLP step, and LLM
   call is already instrumented with `span(...)`.
2. **Agent-level (LangGraph + LangSmith).** Because LangGraph defines the
   multi-agent graph, it can trace which nodes/edges actually executed for a
   request — did the Supervisor call only Compliance, or also Client Profiling
   and Document Analysis? Paired with LangSmith to visualise the reasoning path,
   tool calls, and inter-agent state. Enabled by environment (see below), no
   code change required.

Both layers share the same trace id, so an engineer can correlate "which agents
ran and why" (LangGraph/LangSmith) with "how long each step took and where it
failed" (Azure Monitor) — and separately consult the audit trail for the
compliance record of the final decision.

## What this repo implements

| Capability | Where |
|---|---|
| Span at every hop (supervisor, workers, MCP, NLP, LLM, retrieval) | `telemetry.span(...)`, used across `app/agents/*` and `app/services/*` |
| Single trace id per request, nested across the parallel fan-out | verified in `tests/test_tracing.py` |
| Trace id exposed to the caller | `run_supervisor` → `result["traceId"]`; `POST /api/agents/ask` |
| **Audit ↔ trace correlation** (same id on both) | `AuditLog.record` auto-stamps `trace_id`; query with `audit_log.list(trace_id=...)` |
| Recent-requests list | `GET /api/telemetry/traces` |
| Per-request **waterfall** + correlated audit events | `GET /api/telemetry/trace/{trace_id}` |
| Raw spans / LLM cost accounting | `GET /api/telemetry/spans`, `GET /api/telemetry/llm` |
| Role-restricted trace access (Phase 7) | all `/api/telemetry/*` require `dashboard.read` |
| PII-free spans | spans carry metadata only (durations, agent/tool/model names, ids, status) — never raw query or client text |

### Correlation in practice

```
# 1. Ask the supervisor something; note the trace id it returns.
POST /api/agents/ask {"query": "...", "clientId": "C123"}  ->  {"traceId": "8e06f7c6...", ...}

# 2. See the performance waterfall + the compliance events for that same request.
GET  /api/telemetry/trace/8e06f7c6...
```

## Activation for cloud observability

- **Azure Monitor / Application Insights** (infra tracing): set
  `APPLICATIONINSIGHTS_CONNECTION_STRING`; `telemetry.py` attaches the
  `AzureMonitorTraceExporter` automatically (Phase 2). Local runs stay
  in-memory; set `OTEL_CONSOLE=true` to also print spans.
- **LangSmith** (agent-graph tracing, Phase 5): set `LANGCHAIN_TRACING_V2=true`,
  `LANGCHAIN_API_KEY=<key>`, and `LANGCHAIN_PROJECT=ib-agentic`. LangChain/
  LangGraph then export the node/edge execution trace to LangSmith with no code
  change.
- **Latency alerts**: in Azure Monitor, alert per step (e.g. GraphRAG traversal
  over a threshold) so degradation is caught before it affects analysts.

## Retention & governance

- **Separate retention.** Traces are for debugging (days to weeks); the audit
  trail is compliance evidence (long-lived, WORM in prod). They are never
  merged — only cross-referenced by trace id.
- **PII.** Spans log metadata, not raw client data; the audit trail additionally
  masks PII before writing (`dlp.mask_pii`).
- **Access.** Trace dashboards are role-restricted (`dashboard.read`), the same
  as the rest of the engineering surface (Phase 7).
