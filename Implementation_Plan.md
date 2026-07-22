# Implementation Plan — Agentic AI Platform

This document provides a phase-by-phase implementation plan for the Agentic AI Platform described in `Requirements.md`. It includes goals, milestones, deliverables, owners, estimated durations, acceptance criteria, and sample commands/checklists for core tasks (OpenAPI generation, Neo4j import, AKS deployment, Kafka topics, Azure resources).

LangGraph-first implementation stance
- The platform should be implemented as a LangGraph StateGraph (or MessageGraph for conversational flows), not as a loose chain of prompts. Each workflow should have explicit nodes and a typed state object with fields such as request_id, user_id, session_id, client_id, transaction_context, retrieved_context, tool_calls, decision, provenance, and audit_events.
- All external actions such as Neo4j lookup, Azure OpenAI calls, Kafka publishing, and document upload should be exposed as tools with strict input/output schemas, retries, timeout budgets, and idempotency.
- Deterministic business rules should remain outside the LLM. The model should generate a structured decision record, while the orchestrator applies thresholds, routing, and human review decisions.
- Every node transition should persist provenance so the system can explain why a decision was made and replay the workflow later for audit or debugging.

Phase 0 — Discovery & Kickoff
- Goal: Align stakeholders, confirm scope, collect constraints and data sources.
- Duration: 1–2 weeks
- Activities:
  - Stakeholder interviews (Compliance, Onboarding, Infra, Security).
  - Inventory data sources (policy documents, existing KYC stores).
  - Define success criteria and SLAs.
- Deliverables:
  - Confirmed `Requirements.md` (final review)
  - High-level architecture diagram
  - Project backlog and initial milestones
- Acceptance:
  - Stakeholder sign-off on scope and timeline.

Phase 1 — Design & Architecture
- Goal: Produce detailed designs for data model, LangGraph workflows, API contracts, security, and observability.
- Duration: 2–3 weeks
- Activities:
  - Finalize graph schema and key constraints (Neo4j).
  - Define GraphRAG retrieval patterns and indexes.
  - Create OpenAPI spec (`openapi.yaml`) and review with backend teams.
  - Design LangGraph tool schemas and state models, including node names, transitions, timeout/retry policy, and audit fields.
  - Define a shared `AgentState` contract for compliance, onboarding, and copilot workflows to improve maintainability.
  - Security design: encryption, key management, RBAC mapping.
- Deliverables:
  - `openapi.yaml` (reviewed)
  - Neo4j schema in Cypher (`neo4j_schema.cypher`)
  - LangGraph workflow diagrams and tool specs
  - Security/Compliance design doc
- Acceptance:
  - Design review completed with architecture and security teams.

Phase 2 — Proof of Concept (PoC)
- Goal: Build a minimal, runnable PoC demonstrating ingestion -> GraphRAG -> LLM decision flow.
- Duration: 3–4 weeks
- Scope:
  - Ingestion pipeline for a subset of documents.
  - Neo4j test instance with sample data (use `data/*.csv`).
  - A basic LangGraph flow with explicit nodes such as `ingest -> enrich -> retrieve -> evaluate -> review -> persist`, calling a mock rule-evaluator and a test LLM endpoint.
  - A simple Copilot UI prototype that sends structured requests and displays provenance and decision metadata.
- Milestones:
  - Ingest sample documents and create nodes/relationships in Neo4j.
  - Run a sample GraphRAG retrieval and generate a decision record.
  - Demonstrate end-to-end traceability (request -> retrieval -> decision -> persisted record).
- Deliverables:
  - Working PoC repo and deployment manifest (Helm/Kustomize) for AKS.
  - PoC demo script and dataset.
- Acceptance:
  - Stakeholders can reproduce the PoC and validate example scenarios.

Phase 3 — Core Implementation
- Goal: Implement production-grade ingestion, GraphRAG, LangGraph orchestrations, and Copilot UI.
- Duration: 8–12 weeks (iterative sprints)
- Teams & Owners:
  - Backend (ingestion, APIs, LangGraph): 2–3 engineers
  - Data engineer (Neo4j, indexing): 1–2 engineers
  - Frontend (Copilot UI): 1–2 engineers
  - DevOps & Security: 1–2 engineers
- Activities & Subtasks:
  - Implement ingestion pipeline (OCR, extractor, entity extraction, evidence linking).
  - Build GraphRAG connectors to Neo4j and Azure Search; create retrieval scoring and caching.
  - Implement a LangGraph tool-calling framework and register tools (document-analyzer, rule-evaluator, kafka-publish, audit-logger).
  - Implement `POST /api/agents/inspect`, `POST /api/documents/upload`, and `POST /api/copilot/query` following `openapi.yaml`, with request IDs, session IDs, and provenance included in every response.
  - Implement authentication via Azure AD and RBAC checks.
  - Add immutable audit logging for prompts, retrieved docs, model outputs, and decisions, with redaction of PII before storage.
- Deliverables:
  - Production-ready services and container images.
  - Unit and integration test suites.
  - OpenAPI-driven server stubs and client SDKs.
- Acceptance:
  - Services pass integration tests and security scans; performance targets met in staging.

Phase 4 — Integration & Testing
- Goal: Validate integrations between services, systems, and security controls.
- Duration: 3–4 weeks
- Activities:
  - Integration tests: LangGraph <-> Neo4j, LangGraph <-> AzureAI, Services <-> Kafka.
  - Penetration testing and data exposure risk testing.
  - Load testing for GraphRAG retrieval latency and Kafka throughput.
  - Compliance sampling and audit log validation.
- Deliverables:
  - Test reports and remediation backlog.
  - Updated runbooks.
- Acceptance:
  - No critical findings; all high/medium issues mitigated.

Phase 5 — Security, Compliance & Governance
- Goal: Ensure platform meets regulatory and internal security requirements.
- Duration: concurrent with Phases 3–4; final sign-off 2 weeks
- Activities:
  - Finalize encryption, key rotation policies, and Azure Key Vault integration.
  - Implement data classification, automatic output redaction, and human-in-loop gating.
  - Create audit retention policies and immutable log storage.
  - Document operational playbooks for data breaches and model misuse.
- Deliverables:
  - Security Assessment and Compliance sign-off
  - Governance playbooks and SOPs

Phase 6 — Staging & Production Rollout
- Goal: Deploy to staging, run acceptance tests, then roll out to production with controlled cutover.
- Duration: 2–3 weeks
- Activities:
  - Deploy to staging AKS cluster with production-like data (anonymized).
  - Run end-to-end acceptance tests and user acceptance testing (UAT) with analysts.
  - Gradual production rollout: canary or blue/green strategy.
  - Train support and operations teams.
- Deliverables:
  - Production deployment manifests and configuration (Helm charts, secrets templates)
  - Runbooks and rollback plans
  - Post-deployment monitoring dashboards and alerts
- Acceptance:
  - UAT sign-off and no critical post-deploy incidents in the stabilization window.

Phase 7 — Operate & Handover
- Goal: Transition to steady-state operations and continuous improvement.
- Duration: ongoing
- Activities:
  - Monitor performance, costs, and model usage.
  - Regular policy re-ingestion cadence; scheduled retraining/refresh of retrieval indexes.
  - Implement enhancement backlog and iterate on feature requests.
- Deliverables:
  - Handover checklist, runbooks, and knowledge transfer sessions
  - SLA reporting and monthly review cadence

Implementation Checklist & Sample Commands
- Generate server stubs / clients from `openapi.yaml` using OpenAPI Generator (requires Java & openapi-generator-cli):
  - Generate Node Express server:

```powershell
openapi-generator-cli generate -i openapi.yaml -g nodejs-express-server -o ./generated/server
```

  - Generate TypeScript Axios client:

```powershell
openapi-generator-cli generate -i openapi.yaml -g typescript-axios -o ./generated/client
```

- Neo4j: import CSV into a database (example using `neo4j-admin import` for an empty DB):

```powershell
neo4j-admin import --nodes=clients=data/clients.csv --nodes=documents=data/documents.csv --nodes=evidence=data/evidence.csv
```

- AKS deployment (example Helm flow):

```powershell
# Login
az login
az account set --subscription <SUBSCRIPTION_ID>
az aks get-credentials --resource-group <RG> --name <AKS_CLUSTER>

# Install via Helm
helm repo add mycharts https://example.com/charts
helm upgrade --install agentic-platform mycharts/agentic-platform -n platform --create-namespace --values charts/values.prod.yaml
```

- Create Kafka topic (example using kafka-topics.sh):

```powershell
# On Kafka broker node
kafka-topics.sh --create --topic review-tasks --bootstrap-server kafka:9092 --partitions 6 --replication-factor 3
```

- Create Azure resources: Key Vault, Managed Identity, and assign permissions (high level):

```powershell
az group create -n rg-agentic -l northeurope
az keyvault create -n kv-agentic -g rg-agentic -l northeurope
az identity create -g rg-agentic -n mi-agentic
# Assign RBAC to managed identity and set access policies in Key Vault as required
```

Recommended implementation choices for the course and grading rubric
- For maintainability, prefer one shared `AgentState` schema and one orchestrator per workflow instead of many independent scripts.
- For efficiency, keep retrieval depth bounded, cache repeated GraphRAG queries, and use a mock or local LLM path first to reduce Azure cost and dependency risk.
- For regulatory compliance, route low-confidence or high-risk outputs to human review, redact sensitive fields in logs, and track provenance for every retrieval and decision.
- For Azure free-tier realism, start with mock data and local/mock LLM responses, then swap in Azure AI/OpenAI later once the workflow and observability layers are stable.

Clarifications to resolve before implementation
- Is the target a full production-style prototype or a course PoC with strong documentation and traceability?
- Should the grading emphasis be placed more on workflow design and auditability than on production-scale deployment?
- Is Azure OpenAI access available in the student subscription, or should the initial implementation use a mock LLM interface that can later be replaced?

Milestones & Timeline (example 6-month phased roadmap)

| Phase | Weeks | Outcome |
|---|---:|---|
| Discovery | 1-2 | Scope and kickoff |
| Design | 2-3 | Final design and OpenAPI |
| PoC | 3-4 | Reproducible PoC |
| Implementation | 8-12 | Production services |
| Integration & Testing | 3-4 | Validated integrations |
| Rollout | 2-3 | Production launch |

Risks & Mitigations
- Sensitive outputs from LLMs: use retrieval grounding, redaction, and human-in-loop gating.
- Data quality and schema drift: implement scheduled re-ingestion and validation jobs with alerting.
- Performance at scale: perform early load tests on GraphRAG retrieval and Kafka flows; add caching layers where necessary.

Acceptance Criteria Summary
- All core APIs implemented and passing integration tests.
- Audit logs exist for 100% of decision records with retrievable provenance.
- Security review and compliance sign-off completed.

Next Steps
1. Review this implementation plan with stakeholders and adjust timelines/resources.
2. Approve the PoC scope and spin up temporary Azure/AKS resources for the PoC.
3. Start Design phase tasks: finalize OpenAPI, Neo4j schema, and LangGraph tool specs.

---
Document: `Implementation_Plan.md` — created in workspace root. Reply with any section you want expanded or a preferred server/client target for code generation and I will proceed.
