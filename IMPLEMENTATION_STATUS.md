# Implementation Status — vs. `implementation_1.md`

Status of each plan phase against the actual codebase, and — for anything that
needs infrastructure this environment can't run (no container runtime; Azure
free-trial limits) — the exact steps to activate it.

Legend: ✅ built in-repo · 🟡 scaffolded, needs infra to go live · ⛔ needs
external infra/data not available here · 📄 process/doc, no code.

| Phase | Status | Where it lives / what's needed |
|---|---|---|
| 0 — Discovery & requirements | 📄 | `Use_case.md`, this plan. No code. |
| 1 — Data & knowledge foundation | ✅ / 🟡 | In-memory `graph_store.py`, `neo4j_store.py`, `ingestion.py`, `retrieval.py`, `vector_store.py`. Neo4j + Azure AI Search need live services (🟡). |
| 2 — Core infrastructure (AKS/Docker/Kafka/Monitor) | 🟡 / ⛔ | `infra/k8s/*`, `deploy_aks.sh`, `docker-compose*.yml`, `KafkaEventBus` in `event_bus.py`, telemetry in `telemetry.py`. Needs a cluster + broker to run (⛔ here). |
| 3 — NLP & retrieval pipeline | ✅ | `nlp_pipeline.py`, `retrieval.py` (GraphRAG traversal + search blend). |
| 4 — Agents | ✅ | `compliance`, `copilot` (knowledge), `document_analysis`, `client_profiling`, `onboarding` — all log confidence. |
| 5 — Orchestration (Supervisor/MCP/RBAC/review queue) | ✅ | `supervisor.py`, `app/mcp/server.py`, `rbac.py`, review queue in the store + dashboard. |
| 6 — Internal staff app | ✅ | `static/chat`, `static/dashboard`, `static/reviewer`, `static/login`; grounded citations; **"flag as wrong" feedback (6.6) now added**. Staff SSO is modeled (`azure_ad.py`), activates with real Entra tenant (🟡). |
| 6b — External client portal | ✅ | `static/client` + `static/client/login`, separate identity (`client_identity.py`), own-record scoping, **status-change notifications (6b.5) now added**. |
| 7 — Security & governance | ✅ / 🟡 | `dlp.py` (regex PII + Presidio behind `DLP_ENGINE=presidio`), prompt-injection guard, central RBAC, tamper-evident audit (`audit.py`). Presidio runtime + external pen-test are the 🟡/⛔ parts. |
| 8 — Fine-tuning | ⛔ | Needs Vertex AI + proprietary corpus. Not started; out of scope for this environment. |
| 9 — Evaluation & QA | ✅ | `app/eval/harness.py`, `/api/eval/run` (faithfulness, hallucination, relevancy, precision/recall). RAGAS-judged pass behind `EVAL_ENGINE=ragas`. Feedback regression loop now folded in. |
| 10 — Deployment & rollout | 🟡 / ⛔ | Manifests + `deploy*.sh` exist; actual rollout needs the Phase 2 cluster and a pilot cohort. |

## Request-level tracing (this pass)

Distributed tracing across the multi-agent pipeline — see [`docs/tracing.md`](docs/tracing.md).

- **One trace id per request, nested across the fan-out.** Every hop
  (supervisor → MCP `tools/call` → worker agent → NLP/LLM/retrieval) is an
  OpenTelemetry span under a single trace; verified in `tests/test_tracing.py`.
- **Audit ↔ trace correlation.** `AuditLog.record` auto-stamps the active
  `trace_id`, so a compliance entry and the performance waterfall for the same
  request join on one id (`audit_log.list(trace_id=...)`).
- **Waterfall API.** `GET /api/telemetry/traces` (recent requests) and
  `GET /api/telemetry/trace/{id}` (per-step latency + correlated audit events);
  `run_supervisor` and `POST /api/agents/ask` now return `traceId`/`requestId`.
  All `/api/telemetry/*` are role-restricted (`dashboard.read`, Phase 7).
- **Cloud export** stays env-gated (🟡): Azure Monitor via
  `APPLICATIONINSIGHTS_CONNECTION_STRING` (Phase 2), LangSmith agent-graph
  tracing via `LANGCHAIN_TRACING_V2=true` (Phase 5).

## What was just added in code (this pass)

- **Phase 6b.5 — client status notifications.** `app/services/notifications.py`
  (dev `outbox` channel + audited; prod swaps `_deliver_email` for Azure
  Communication Services / SMTP via `NOTIFY_CHANNEL=email`). Emitted on every
  client-safe transition from `applications.notify_status_change`, deduped per
  status, wired at apply, document upload, and review resolution.
- **Phase 6.6 — Copilot "flag as wrong" feedback.** `app/services/feedback.py`,
  `POST/GET /api/copilot/feedback` (`feedback.submit` permission for staff
  roles), a flag control in `static/chat`, and a regression pass
  (`run_feedback_regression`) folded into `/api/eval/run` so flagged questions
  are re-scored until fixed.

## Activation steps for the infra-blocked phases

### Phase 2 — AKS + Kafka + Azure Monitor
1. Provision AKS: `infra/deploy_aks.sh` (see `Azure_AKS_Implementation_Guide.md`).
2. Deploy: `kubectl apply -k infra/k8s`.
3. Kafka: set `KAFKA_BOOTSTRAP_SERVERS`; `event_bus.py` switches from the
   in-memory bus to `KafkaEventBus` (Redpanda/MSK/Event Hubs-Kafka all work).
4. Azure Monitor: point the OTEL exporter in `telemetry.py` at your workspace.

### Phase 1/3 — Neo4j + Azure AI Search live
- Neo4j: set `NEO4J_URI`/`NEO4J_USER`/`NEO4J_PASSWORD` (Aura uses `neo4j+ssc`);
  `api.py` auto-selects `Neo4jStore` when `NEO4J_URI` is present.
- Azure AI Search: set `AZURE_SEARCH_ENDPOINT`/`AZURE_SEARCH_INDEX` +
  `USE_AZURE_SERVICES=true`; `azure_search_store.py` takes over from `vector_store`.

### Phase 6 — Staff SSO / Phase 6b — Client B2C
- Staff: `ENABLE_AZURE_AD=true` + `AZURE_AD_TENANT_ID`/`AZURE_AD_CLIENT_ID`;
  `get_current_user` then validates real Entra tokens instead of local JWTs.
- Client: swap `client_identity.py` for an Azure AD B2C user flow (the module
  boundary is already isolated for exactly this).

### Phase 7 — Presidio / pen-test
- Presidio: `pip install -r requirements-ml.txt`, set `DLP_ENGINE=presidio`.
- Pen-test: external engagement against both apps (OWASP LLM Top 10).

### Phase 8 — Fine-tuning
- Requires Vertex AI access + the cleared proprietary corpus; not runnable here.
