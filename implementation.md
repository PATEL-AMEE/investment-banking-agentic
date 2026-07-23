# Implementation Plan — Enterprise Agentic AI Platform (Barclays Investment Banking)

Based on the use case: a multi-agent Agentic AI platform (LangGraph + MCP) automating regulatory compliance, client onboarding, and knowledge retrieval, with GraphRAG on Neo4j, Azure AI, Kubernetes/Docker/Kafka, and full LLM security controls.

---

## Phase 0 — Discovery & Requirements

1. Confirm the three target workflows: regulatory compliance checking, client onboarding, knowledge retrieval/Q&A.
2. Identify stakeholders: compliance analysts (end users), CTO/CRO (sponsors), information security team (gatekeepers).
3. Catalog regulatory scope: which jurisdictions/regulations the Compliance Agent must cover first (e.g., MiFID II, AML/KYC rules).
4. Define success metrics up front: RAG faithfulness/relevancy targets, hallucination-rate ceiling, retrieval accuracy uplift target (the use case cites a 34% improvement goal from fine-tuning).
5. Get security and data-governance sign-off on scope before any client/PII data is touched.

**Exit criteria:** signed-off scope doc, target metrics, list of regulations/policies in v1.

---

## Phase 1 — Data & Knowledge Foundation

1. Collect the regulatory/policy corpus (internal policy manuals, jurisdictional rules, regulatory texts) — this becomes the grounding source for RAG.
2. Collect client/onboarding data (KYC/AML records, client profiles, transaction history) under proper access controls.
3. Collect a contracts/documents corpus for clause extraction and classification training.
4. Anonymize/redact sensitive fields before any of this touches a model (tie into Phase 6 security work, but redaction pipeline needs to exist first).
5. Stand up Neo4j and model the regulatory knowledge graph (entities: regulations, clauses, jurisdictions, obligations; relationships: supersedes, references, applies-to).
6. Load documents into Azure AI Search for vector/keyword retrieval.
7. Build a small labeled evaluation set (question + gold answer pairs) for later RAGAS testing.

**Exit criteria:** Neo4j graph populated and queryable; Azure AI Search index live; eval set drafted.

**Maps to your live dashboard:** this is the "Clients / Policies / Regulations / Documents" counts and the in-memory GraphStore you're already seeing.

---

## Phase 2 — Core Infrastructure

1. Provision Azure Kubernetes Service (AKS) cluster(s) for dev/test/prod.
2. Containerize services with Docker (one container family per agent/service).
3. Stand up Apache Kafka for event streaming between agents/services.
4. Wire up Azure Monitor for logging, metrics, and tracing from day one (cheaper to bake in early than retrofit).
5. Set up CI/CD and environment promotion (dev → test → prod) with code-review gates.

**Exit criteria:** empty "hello world" service deployed end-to-end through the pipeline, observable in Azure Monitor.

---

## Phase 3 — NLP & Retrieval Pipeline

1. Build NLP extraction pipeline (spaCy + Hugging Face Transformers) for named-entity recognition, contract clause extraction, and document classification.
2. Wire NLP output into the Azure AI Search index (structured metadata alongside raw text).
3. Build the GraphRAG retrieval layer: given a query, traverse Neo4j for related regulatory nodes and combine with Azure AI Search hits.
4. Benchmark retrieval quality against the Phase 1 eval set before connecting to any agent.

**Exit criteria:** given a test question, the pipeline returns correct, ranked source passages — no generation yet, just retrieval.

---

## Phase 4 — Agent Development

Build one agent at a time; ship each independently before wiring orchestration.

1. **Compliance Agent** (build first — highest value, and what your current dashboard already demonstrates): takes a client/transaction, runs it against retrieved regulations, returns a risk score, confidence, and pass/warn/fail decision.
2. **Knowledge Retrieval Agent**: general-purpose regulatory Q&A over the GraphRAG/Search layer.
3. **Document Analysis Agent**: ingests contracts/documents, applies the NLP pipeline, extracts structured findings.
4. **Client Profiling Agent**: builds/maintains a risk profile per client from onboarding + transaction data.
5. **Onboarding Agent**: orchestrates the end-to-end client onboarding checklist, calling the other agents as needed.

Each agent should log a confidence score with every decision — this is what later drives the review queue.

**Exit criteria:** each agent independently testable via a direct API call, with decisions logged.

---

## Phase 5 — Orchestration Layer

1. Introduce LangGraph to define the multi-agent graph (nodes = agents, edges = handoff conditions).
2. Implement MCP as the protocol for context-sharing and tool invocation between agents.
3. Add a **Supervisor Agent**: routes incoming requests to the right worker agent(s), aggregates multi-agent results, arbitrates conflicting outputs, and triggers human review when confidence is low.
4. Wire the review queue: any decision below a confidence threshold gets flagged "required" for a human reviewer (matches the "Review queue: Pending/Resolved" panel on your dashboard).

**Exit criteria:** a single request can be routed through the Supervisor to the correct agent(s) and return one coherent, aggregated response.

---

## Phase 6 — Copilot UI (Chat Interface)

1. Build the natural-language Q&A interface using Azure OpenAI Service, backed by LangGraph/Supervisor.
2. Ground every generated answer in the retrieved sources from Phase 3 (no un-grounded generation).
3. Surface cited sources alongside answers for analyst trust and auditability.
4. Add basic session handling and role-aware UI (different views for compliance vs. onboarding staff).

**Exit criteria:** a compliance analyst can ask a regulatory question in plain English and get a grounded, sourced answer.

---

## Phase 7 — Security & Governance

Should run in parallel with Phases 4–6, not bolted on at the end.

1. Implement PII redaction using Microsoft Presidio on all data entering the pipeline.
2. Implement prompt-injection defenses on every LLM-facing endpoint.
3. Implement role-based access control (RBAC) per agent/endpoint — enforce centrally via the Supervisor Agent.
4. Implement cryptographic audit trails logging every agent decision, tool call, and data access (matches the "Recent audit events" panel already live).
5. Run penetration testing and threat modeling against the OWASP Top 10 for LLM Applications before any production rollout.

**Exit criteria:** security team sign-off; audit trail verifiably tamper-evident; pen test findings resolved or accepted.

---

## Phase 8 — Fine-Tuning & Model Improvement

1. Identify niche regulatory topics where base-model retrieval accuracy is weak (from Phase 3/6 benchmarking).
2. Fine-tune domain-specific adapters via Vertex AI on the proprietary compliance corpus.
3. Re-run the eval set to confirm accuracy uplift (target: the ~34% improvement cited in the use case).
4. Establish a re-training cadence as regulations change.

**Exit criteria:** measurable accuracy improvement on the niche-topic subset of the eval set.

---

## Phase 9 — Evaluation & Quality Assurance

1. Build automated evaluation harnesses using RAGAS plus custom Python benchmarks.
2. Continuously measure faithfulness, answer relevancy, and hallucination rate across the RAG/GraphRAG chains.
3. Set hard gates: no release ships if hallucination rate exceeds an agreed threshold.
4. Feed evaluation failures back into Phase 1 data curation and Phase 8 fine-tuning.

**Exit criteria:** automated eval running on every release candidate, with a dashboard of trendlines over time.

---

## Phase 10 — Deployment, Observability & Rollout

1. Deploy full stack to AKS with Docker containers and Kafka event streaming in production configuration.
2. Confirm Azure Monitor dashboards cover every agent interaction in real time.
3. Run a pilot with a small group of compliance analysts; collect feedback on decision quality and UI usability.
4. Establish an architecture review board and code-review/security sign-off gate for every future LLM-integrated feature (per the use case's team-process bullet).
5. Expand rollout to onboarding and knowledge-retrieval use cases once compliance pilot is stable.
6. Present quarterly roadmap updates to CTO/CRO tying platform capability to risk-reduction outcomes.

**Exit criteria:** production rollout complete for Compliance Agent; roadmap in place for Onboarding and Knowledge Retrieval agents.

---

## Phase-to-Dashboard Cross-Reference

| Dashboard element you're seeing today | Implementation phase it belongs to |
|---|---|
| Clients / Policies / Regulations / Documents counts | Phase 1 — Data & Knowledge Foundation |
| In-memory GraphStore | Phase 1 (early build of the Neo4j GraphRAG layer) |
| Client risk decisions (risk, confidence, pass/warn/fail) | Phase 4 — Compliance Agent |
| Review queue (Pending/Resolved) | Phase 5 — Supervisor Agent + human-in-the-loop |
| Recent audit events (mcp_tool_call) | Phase 5 (MCP) + Phase 7 (audit trail) |
| Not yet visible: Onboarding/Document/Client-Profiling agents, Copilot UI, RBAC, PII redaction, RAGAS scores | Phases 4 (remaining agents), 6, 7, 9 |

---

## Suggested Build Order Summary

1. Data & graph foundation
2. Infrastructure skeleton (AKS/Docker/Kafka/Monitor)
3. Retrieval pipeline (NLP + GraphRAG + Search)
4. Compliance Agent (already in progress)
5. Remaining four agents
6. LangGraph + MCP + Supervisor Agent
7. Copilot UI
8. Security hardening (parallel track from step 4 onward)
9. Fine-tuning
10. Evaluation harness
11. Production rollout & pilot
