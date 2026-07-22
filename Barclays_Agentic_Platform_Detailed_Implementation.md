# Barclays Enterprise Agentic AI Platform — Detailed Implementation Plan

## Executive Summary

This document provides a comprehensive, production-grade implementation roadmap for the Barclays Investment Banking Agentic AI Platform. The platform orchestrates specialized LLM agents via LangGraph to automate regulatory compliance, client onboarding, and knowledge retrieval across the Investment Banking division. The system integrates GraphRAG on Neo4j, Azure AI Services, Azure Kubernetes Service (AKS), Docker containerization, Apache Kafka, and end-to-end security controls across all LLM endpoints.

**Target Outcome:** A stateful, auditable, multi-agent platform capable of processing compliance checks, client KYC/AML workflows, and regulatory Q&A at scale with full provenance tracking, human-in-loop gating, and real-time monitoring.

---

## Part 1: Architecture Overview

### 1.1 System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                     User Layer                                  │
│  ┌──────────────────┬──────────────────┬──────────────────┐    │
│  │ Compliance UI    │ Copilot Portal   │ Onboarding UI    │    │
│  │ (React/SPA)      │ (Chat Interface) │ (Wizard Flow)    │    │
│  └──────────────────┴──────────────────┴──────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
                              ↓ HTTPS / Auth
┌─────────────────────────────────────────────────────────────────┐
│              API Gateway & Auth Layer                           │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ Azure AD / RBAC / Token Validation / Rate Limiting       │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│              Kubernetes (AKS) Cluster                           │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │ LangGraph Orchestration Layer                            │  │
│  │ ┌──────────────────────────────────────────────────────┐ │  │
│  │ │ • Compliance Agent (StateGraph)                      │ │  │
│  │ │ • Onboarding Agent (StateGraph)                      │ │  │
│  │ │ • Copilot Agent (MessageGraph)                       │ │  │
│  │ │ • Tool Router & Dispatcher                           │ │  │
│  │ └──────────────────────────────────────────────────────┘ │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ Microservices Layer                                      │  │
│  │ ┌──────────────┬──────────────┬──────────────────────┐  │  │
│  │ │ API Service  │ Ingestion    │ GraphRAG Retrieval   │  │  │
│  │ │              │ Service      │ Service              │  │  │
│  │ └──────────────┴──────────────┴──────────────────────┘  │  │
│  ├──────────────────────────────────────────────────────────┤  │
│  │ External Integrations                                    │  │
│  │ ┌──────────────┬──────────────┬──────────────────────┐  │  │
│  │ │ Azure OpenAI │ Azure Search │ Audit Logger         │  │  │
│  │ │ (GPT-4)      │ (Vector DB)  │ (Event Hub)          │  │  │
│  │ └──────────────┴──────────────┴──────────────────────┘  │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
            ↓              ↓              ↓              ↓
┌──────────────────┬─────────────────┬──────────────┬──────────────┐
│ Neo4j Cluster    │ Apache Kafka    │ Azure Key    │ Azure Monitor│
│ (GraphDB)        │ (Event Stream)  │ Vault        │ (Observ.)   │
│                  │                 │ (Secrets)    │             │
└──────────────────┴─────────────────┴──────────────┴──────────────┘
```

### 1.2 LangGraph Workflow Architecture

#### Compliance Agent (StateGraph)
```
Input: ComplianceRequest
State: ComplianceAgentState
  {
    request_id: str
    user_id: str
    client_id: str
    document_ids: list[str]
    retrieved_policies: list[Dict]
    rule_violations: list[Dict]
    decision: str
    risk_score: float
    requires_review: bool
    provenance: list[Dict]
    audit_events: list[Dict]
  }

Nodes:
  1. validate_request → schema validation, rate-limit check
  2. retrieve_policies → Neo4j GraphRAG lookup (compliance policies)
  3. extract_entities → LLM-based entity extraction from documents
  4. evaluate_rules → deterministic rule engine (NO LLM)
  5. generate_decision → LLM generates structured compliance decision
  6. review_gate → human-in-loop for high-risk (score > threshold)
  7. persist_audit → immutable audit log to Event Hub
  8. publish_result → Kafka topic for downstream consumers

Transitions:
  validate_request → retrieve_policies
  retrieve_policies → extract_entities
  extract_entities → evaluate_rules
  evaluate_rules → generate_decision
  generate_decision → review_gate (conditional)
  review_gate → persist_audit
  persist_audit → publish_result
```

#### Onboarding Agent (StateGraph)
```
Input: OnboardingRequest
State: OnboardingAgentState
  {
    request_id: str
    client_id: str
    kyc_data: Dict
    aml_risk_score: float
    beneficial_owners: list[Dict]
    company_info: Dict
    retrieved_regulations: list[Dict]
    sanctions_check: Dict
    final_status: str
    provenance: list[Dict]
  }

Nodes:
  1. parse_kyc_input → normalize client KYC data
  2. retrieve_regulations → GraphRAG lookup (AML/KYC rules)
  3. check_sanctions → call external sanctions database tool
  4. analyze_pep → PEP (Politically Exposed Person) check via LLM
  5. assess_risk → structured risk assessment (deterministic + LLM)
  6. generate_profile → LLM creates client compliance profile
  7. escalate_if_needed → route to human reviewer if high-risk
  8. persist_record → store onboarded client in Neo4j + audit log

Transitions:
  parse_kyc_input → retrieve_regulations
  retrieve_regulations → check_sanctions
  check_sanctions → analyze_pep
  analyze_pep → assess_risk
  assess_risk → generate_profile
  generate_profile → escalate_if_needed (conditional)
  escalate_if_needed → persist_record
```

#### Copilot Agent (MessageGraph)
```
Input: UserQuery (natural language)
State: CopilotAgentState
  {
    user_id: str
    session_id: str
    messages: list[Message]  # conversation history
    retrieved_docs: list[Dict]
    response: str
    citations: list[Dict]
    confidence_score: float
  }

Nodes:
  1. parse_query → extract intent and entities
  2. retrieve_context → GraphRAG + Azure Search for relevant policies/docs
  3. augment_context → enrich with recent decisions and precedents
  4. generate_response → LLM generates response grounded in retrieved context
  5. add_citations → attach provenance (doc IDs, policy refs, confidence)
  6. gate_output → check for sensitive info, flag if confidence < threshold
  7. persist_conversation → store message exchange for audit

Transitions:
  parse_query → retrieve_context
  retrieve_context → augment_context
  augment_context → generate_response
  generate_response → add_citations
  add_citations → gate_output
  gate_output → persist_conversation
```

### 1.3 Tool Registry (LangGraph Tool Calling)

Each tool has:
- **Input Schema:** Strict Pydantic model
- **Output Schema:** Structured response
- **Timeout:** Maximum execution time
- **Retry Policy:** Exponential backoff, max 3 attempts
- **Idempotency:** Request ID tracking

```yaml
Tools:
  document_analyzer:
    description: Extract entities, relationships, and evidence from documents
    input:
      document_id: str
      document_text: str
      extraction_type: Enum [entities, relationships, evidence]
    output:
      extracted_data: Dict
      confidence_scores: Dict
      provenance: List[str]
    timeout: 60s

  rule_evaluator:
    description: Deterministic compliance rule evaluation
    input:
      rule_id: str
      context: Dict
      entity_data: Dict
    output:
      rule_passed: bool
      violation_details: Optional[Dict]
      remediation_steps: List[str]
    timeout: 10s
    retry: 3

  graph_retriever:
    description: Query Neo4j for policies, precedents, relationships
    input:
      query_type: Enum [policy_search, entity_lookup, precedent_search]
      search_terms: List[str]
      filters: Dict
    output:
      results: List[Dict]
      total_count: int
      search_depth: int
    timeout: 30s
    cache: True (1hr TTL)

  vector_search:
    description: Azure Search for semantic similarity (GraphRAG)
    input:
      query_text: str
      top_k: int = 10
      filters: Dict
    output:
      documents: List[Dict]
      scores: List[float]
      retrieved_at: datetime
    timeout: 15s

  sanctions_check:
    description: Check client against sanctions lists
    input:
      client_name: str
      client_country: str
      beneficial_owners: List[Dict]
    output:
      is_sanctioned: bool
      matches: List[Dict]
      check_timestamp: datetime
    timeout: 20s

  kafka_publish:
    description: Publish decision to Kafka topic for async processing
    input:
      topic: str
      key: str
      payload: Dict
    output:
      message_id: str
      partition: int
      offset: int
    timeout: 10s
    retry: 5

  audit_logger:
    description: Immutable append-only audit log
    input:
      event_type: str
      actor_id: str
      resource_id: str
      action: str
      result: str
      metadata: Dict
    output:
      log_id: str
      timestamp: datetime
      immutable: bool
    timeout: 5s
```

---

## Part 2: Detailed Implementation Phases

### Phase 0: Discovery & Kickoff (1–2 weeks)

**Objectives:**
- Align Investment Banking stakeholders (Compliance, Risk, Operations)
- Identify data sources (policy documents, client KYC stores, regulatory databases)
- Define SLAs, security baselines, and audit requirements

**Key Activities:**

1. **Stakeholder Interviews**
   - Compliance team: Policy versioning, approval workflows, audit trail requirements
   - Risk team: Risk scoring models, thresholds for human review, escalation criteria
   - Operations: Current KYC/AML processes, volume projections, turnaround times
   - Security: Data classification, encryption standards, key rotation policies

2. **Data Inventory**
   - Policy documents (PDF, Word, internal Wiki)
   - Client KYC database schema
   - Sanctions list providers (OFAC, EU list, HMT)
   - Historical decision records (for validation and precedent)
   - Regulatory change logs

3. **Success Criteria & SLAs**
   - Compliance checks: 95% accuracy, <5s latency, 100% audit trail
   - Onboarding: 80% auto-approved, <30min turnaround for 100 clients/day
   - Copilot: <2s response time for policy Q&A, 90% confidence threshold

**Deliverables:**
- Finalized `Requirements.md` (scope, constraints, data sources)
- High-level architecture diagram (stakeholder-approved)
- Project backlog with epics, features, and acceptance criteria
- Data governance matrix (classification, retention, compliance)

**Acceptance Criteria:**
- Written stakeholder sign-off on scope, timeline, and resource allocation
- Data sources confirmed and access provisioned
- Success metrics and SLA thresholds documented

---

### Phase 1: Design & Architecture (2–3 weeks)

**Objectives:**
- Finalize data model (Neo4j schema)
- Define LangGraph workflow architectures and state contracts
- Design API specifications and security controls
- Plan observability and audit infrastructure

**Key Activities:**

1. **Neo4j Graph Schema Design**

```cypher
// Nodes
CREATE CONSTRAINT ON (p:Policy) ASSERT p.policy_id IS UNIQUE;
CREATE CONSTRAINT ON (c:Compliance_Rule) ASSERT c.rule_id IS UNIQUE;
CREATE CONSTRAINT ON (r:Regulation) ASSERT r.regulation_id IS UNIQUE;
CREATE CONSTRAINT ON (cl:Client) ASSERT cl.client_id IS UNIQUE;
CREATE CONSTRAINT ON (doc:Document) ASSERT doc.document_id IS UNIQUE;
CREATE CONSTRAINT ON (ev:Evidence) ASSERT ev.evidence_id IS UNIQUE;
CREATE CONSTRAINT ON (d:Decision) ASSERT d.decision_id IS UNIQUE;

// Key Relationships
MATCH (p:Policy), (c:Compliance_Rule)
WHERE p.policy_id = c.parent_policy_id
CREATE (p)-[:CONTAINS {version: p.version, effective_date: p.effective_date}]->(c);

MATCH (c:Compliance_Rule), (r:Regulation)
CREATE (c)-[:IMPLEMENTS {mapping_date: datetime()}]->(r);

MATCH (cl:Client), (doc:Document)
CREATE (cl)-[:SUBMITTED {submission_date: datetime()}]->(doc);

MATCH (doc:Document), (ev:Evidence)
CREATE (doc)-[:CONTAINS_EVIDENCE {confidence: 0.95}]->(ev);

MATCH (ev:Evidence), (d:Decision)
CREATE (ev)-[:SUPPORTS {weight: 0.8}]->(d);

// GraphRAG Indexes
CREATE INDEX policy_fulltext ON (p:Policy) FOR (p.policy_text);
CREATE INDEX rule_keyword ON (c:Compliance_Rule) FOR (c.rule_description);
CREATE INDEX regulation_keyword ON (r:Regulation) FOR (r.regulation_text);
```

2. **Shared AgentState Contract (Pydantic Models)**

```python
# Base state for all agents
class AgentState(BaseModel):
    request_id: str  # UUID for traceability
    user_id: str
    session_id: str
    client_id: Optional[str]
    transaction_context: Dict  # {service, module, operation}
    retrieved_context: List[Dict]  # GraphRAG results
    tool_calls: List[Dict]  # [{tool_name, input, output, timestamp}]
    decision: Optional[str]
    risk_score: float
    provenance: List[Dict]  # [{source, confidence, retrieval_path}]
    audit_events: List[Dict]  # [{event_type, timestamp, actor, action, result}]
    requires_review: bool
    flagged_fields: List[str]  # PII, sensitive data
    created_at: datetime
    updated_at: datetime

class ComplianceAgentState(AgentState):
    document_ids: List[str]
    retrieved_policies: List[Dict]
    rule_violations: List[Dict]
    remediation_steps: List[str]

class OnboardingAgentState(AgentState):
    kyc_data: Dict
    aml_risk_score: float
    beneficial_owners: List[Dict]
    sanctions_check: Dict
    final_status: Enum [APPROVED, REJECTED, PENDING_REVIEW]

class CopilotAgentState(BaseModel):
    user_id: str
    session_id: str
    messages: List[Message]  # {role, content, timestamp}
    retrieved_docs: List[Dict]
    response: str
    citations: List[Dict]  # {doc_id, policy_ref, page, confidence}
    confidence_score: float
```

3. **OpenAPI Specification** (partial extract)

```yaml
openapi: 3.0.3
info:
  title: Barclays Agentic AI Platform API
  version: 1.0.0
servers:
  - url: https://agentic-api.barclays.com/v1

paths:
  /api/agents/compliance/check:
    post:
      summary: Check document for compliance violations
      tags: [Compliance]
      security:
        - bearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/ComplianceCheckRequest'
      responses:
        '200':
          description: Compliance check completed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/ComplianceCheckResponse'
        '400':
          description: Invalid request
        '429':
          description: Rate limit exceeded
        '500':
          description: Internal server error

  /api/agents/onboarding/kyc:
    post:
      summary: Perform KYC/AML onboarding workflow
      tags: [Onboarding]
      security:
        - bearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/KYCRequest'
      responses:
        '200':
          description: KYC assessment completed
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/KYCResponse'

  /api/copilot/query:
    post:
      summary: Query compliance copilot
      tags: [Copilot]
      security:
        - bearerAuth: []
      requestBody:
        required: true
        content:
          application/json:
            schema:
              $ref: '#/components/schemas/CopilotQueryRequest'
      responses:
        '200':
          description: Copilot response
          content:
            application/json:
              schema:
                $ref: '#/components/schemas/CopilotQueryResponse'

components:
  schemas:
    ComplianceCheckRequest:
      type: object
      required: [document_id, document_text]
      properties:
        request_id:
          type: string
          format: uuid
        document_id:
          type: string
        document_text:
          type: string
        client_id:
          type: string
        metadata:
          type: object

    ComplianceCheckResponse:
      type: object
      properties:
        request_id:
          type: string
        decision:
          type: string
          enum: [APPROVED, REJECTED, ESCALATED]
        risk_score:
          type: number
          format: float
        violations:
          type: array
          items:
            $ref: '#/components/schemas/Violation'
        provenance:
          type: array
          items:
            $ref: '#/components/schemas/ProvenanceRecord'
        timestamp:
          type: string
          format: date-time

    Violation:
      type: object
      properties:
        rule_id:
          type: string
        rule_name:
          type: string
        severity:
          type: string
          enum: [CRITICAL, HIGH, MEDIUM, LOW]
        details:
          type: string
        remediation:
          type: array
          items:
            type: string

    ProvenanceRecord:
      type: object
      properties:
        source:
          type: string
        confidence:
          type: number
          format: float
        retrieval_path:
          type: string
        timestamp:
          type: string
          format: date-time
```

4. **Security & Compliance Design**
   - **Encryption at Rest:** Azure Key Vault (AES-256)
   - **Encryption in Transit:** TLS 1.3
   - **Authentication:** Azure AD with OAuth 2.0 / OIDC
   - **Authorization:** RBAC (Compliance Officer, Risk Manager, Analyst, Admin)
   - **Audit Logging:** Immutable append-only Event Hub
   - **PII Redaction:** Automated masking in logs and outputs
   - **Data Classification:** Public, Confidential, Restricted
   - **Compliance Mapping:** GDPR, MiFID II, PSD2, UK FCA, ECB

**Deliverables:**
- Approved Neo4j schema (Cypher script)
- Finalized OpenAPI specification (3.0.3)
- LangGraph workflow diagrams (Mermaid format)
- Shared `AgentState` Pydantic models
- Security & Compliance design document
- Data classification and retention policy

**Acceptance Criteria:**
- Architecture review completed with CTO, Security, Compliance teams
- OpenAPI spec reviewed by backend and integration teams
- Security controls mapped to regulatory requirements

---

### Phase 2: Proof of Concept (3–4 weeks)

**Objectives:**
- Build minimal, runnable PoC demonstrating full workflow
- Validate LangGraph orchestration pattern
- Prove Neo4j + Azure AI integration
- Demonstrate end-to-end auditability

**Scope:**
- 1,000 policy documents ingested into Neo4j
- 100 sample clients with KYC data
- Simple Compliance Agent (3 nodes) and Copilot Agent (2 nodes)
- Mock Azure OpenAI LLM endpoint (use local Ollama or GPT-3.5 for cost)
- Basic React UI dashboard
- Audit logging to Azure Event Hub

**Key Milestones:**

1. **Week 1: Data Ingestion & GraphRAG Setup**
   - Load policies, regulations, and client data into Neo4j
   - Create fulltext and vector indexes
   - Set up Azure Search with semantic ranking
   - Test GraphRAG retrieval (latency, recall, precision)

2. **Week 2: LangGraph Orchestration & Tool Integration**
   - Implement ComplianceAgent StateGraph (5 nodes)
   - Implement CopilotAgent MessageGraph (4 nodes)
   - Register all tools (graph_retriever, rule_evaluator, kafka_publish, audit_logger)
   - Mock Azure OpenAI calls locally

3. **Week 3: API & UI Layer**
   - Generate API server stubs from OpenAPI spec using openapi-generator-cli
   - Implement `/api/agents/compliance/check` endpoint
   - Implement `/api/copilot/query` endpoint
   - Build simple React dashboard to visualize workflows and audit logs

4. **Week 4: Integration & Testing**
   - End-to-end test: document upload → compliance check → audit trail
   - Load test: 50 concurrent requests
   - Security test: token validation, RBAC enforcement
   - Stakeholder demo and feedback

**PoC Deliverables:**
- Working Docker images (agent service, API gateway, UI)
- Helm chart for AKS deployment
- Sample dataset (policies.json, clients.json, decisions.json)
- PoC demo script (bash/PowerShell)
- README with reproduction steps

**PoC Sample Commands:**

```bash
# 1. Load data into Neo4j
docker run --rm -v $(pwd)/data:/data neo4j-admin-import \
  --nodes=Compliance_Rule=/data/compliance_rules.csv \
  --nodes=Policy=/data/policies.csv \
  --nodes=Client=/data/clients.csv

# 2. Deploy to AKS
az aks get-credentials --resource-group rg-agentic --name aks-agentic
helm upgrade --install agentic-poc ./charts/agentic-poc \
  --namespace agentic \
  --create-namespace \
  --values charts/values.poc.yaml

# 3. Port-forward to test
kubectl port-forward -n agentic svc/api-gateway 8080:8080

# 4. Test compliance check
curl -X POST http://localhost:8080/api/agents/compliance/check \
  -H "Authorization: Bearer $TOKEN" \
  -d @- << EOF
{
  "document_id": "doc_001",
  "document_text": "Client ABC Corp...",
  "client_id": "client_123"
}
EOF

# 5. View audit logs
kubectl logs -n agentic deployment/api-gateway -f --all-containers=true
```

**Acceptance Criteria:**
- PoC deployable to AKS in <30 minutes
- Stakeholders can reproduce end-to-end compliance check
- All audit events logged and queryable
- No security vulnerabilities (OWASP Top 10)

---

### Phase 3: Core Implementation (8–12 weeks)

**Objectives:**
- Implement production-grade services with full feature set
- Scale ingestion, retrieval, and decision pipelines
- Integrate all security and compliance controls
- Achieve performance SLAs

**Teams & Ownership:**

| Role | Count | Responsibilities |
|------|-------|------------------|
| Backend Engineer (LangGraph, APIs) | 2 | Orchestration, tool integration, API design |
| Data Engineer (Neo4j, GraphRAG) | 2 | Schema optimization, retrieval tuning, indexing |
| Frontend Engineer (Copilot UI) | 1–2 | Copilot interface, dashboard, real-time updates |
| DevOps / Platform Engineer | 1–2 | AKS, Kafka, monitoring, security scanning |
| Security Engineer (compliance) | 0.5 | Audit controls, PII redaction, penetration testing |

**Sprint Breakdown (12 weeks = 3 sprints of 4 weeks each):**

#### Sprint 1 (Weeks 1–4): Foundation & Ingestion

**Sprint Goal:** Establish core infrastructure, ingestion pipeline, and foundational services

**Stories:**

1. **Ingestion Pipeline v1**
   - Implement document parser (PDF, OCR, text extraction)
   - Build entity extractor (NER via spaCy or LLM)
   - Implement Neo4j entity/relationship builder
   - Create ingestion scheduling (daily/hourly)
   - **AC:** Ingest 5,000 policy documents with 95%+ confidence entity extraction

2. **API Gateway & Auth**
   - Implement API Gateway in FastAPI
   - Integrate Azure AD authentication
   - Implement RBAC middleware
   - Add request/response logging and rate limiting
   - **AC:** 100 req/s throughput, <5ms latency, 99.9% availability

3. **Neo4j Cluster Setup & Indexing**
   - Deploy Neo4j Enterprise (HA mode) on AKS
   - Create fulltext and vector indexes
   - Implement graph query caching layer (Redis)
   - Test Cypher query performance
   - **AC:** Queries complete in <500ms (p99), index rebuild <1hr

4. **Tool Registry & Execution Framework**
   - Implement tool registration system
   - Build input validation (Pydantic schemas)
   - Add timeout and retry logic
   - Implement idempotency tracking
   - **AC:** All tools callable via LangGraph, pass schema validation

5. **Audit Logging Infrastructure**
   - Set up Azure Event Hub for immutable logs
   - Implement audit event serialization
   - Add PII redaction rules (regex + LLM)
   - Create audit log query API
   - **AC:** 100% of decisions logged, <100ms latency, 99.99% durability

**Sprint 1 Deliverables:**
- Ingestion service (Docker image)
- API Gateway service
- Neo4j infrastructure
- Tool registry library
- Audit logging service
- Integration tests (70% coverage)

#### Sprint 2 (Weeks 5–8): Agents & GraphRAG

**Sprint Goal:** Implement LangGraph agents, GraphRAG retrieval, and core decision workflows

**Stories:**

1. **Compliance Agent Implementation**
   - Implement ComplianceAgentStateGraph (validate → retrieve → analyze → decide → review → persist)
   - Integrate document_analyzer tool
   - Integrate rule_evaluator tool (deterministic rules engine)
   - Implement human-in-loop review gate (Kafka → review queue)
   - **AC:** Process 100 documents/hour, 95% accuracy, <10s latency

2. **GraphRAG Retrieval Service**
   - Implement semantic search (Azure Search + embeddings)
   - Add keyword search fallback
   - Build retrieval ranking and re-ranking logic
   - Cache top-k results (TTL-based)
   - **AC:** Recall@10 > 0.90 for policy queries, <1s latency

3. **Onboarding Agent Implementation**
   - Implement OnboardingAgentStateGraph
   - Integrate sanctions_check tool (call external sanctions provider)
   - Implement PEP analysis via LLM
   - Build KYC risk scoring logic
   - **AC:** Process 500 KYC records/day, 80% auto-approval rate

4. **Copilot Agent Implementation**
   - Implement CopilotAgentMessageGraph
   - Add multi-turn conversation support
   - Implement context augmentation (recent decisions + precedents)
   - Add citation/provenance generation
   - **AC:** <2s response time, 90%+ confidence threshold

5. **Azure OpenAI Integration**
   - Set up Azure OpenAI Service account (GPT-4)
   - Implement token-efficient prompting
   - Add cost monitoring and budget alerts
   - Implement fallback to GPT-3.5-turbo
   - **AC:** <$0.05 per decision (monitored spend)

**Sprint 2 Deliverables:**
- Compliance Agent service
- Onboarding Agent service
- Copilot Agent service
- GraphRAG retrieval service
- Azure OpenAI integration
- Agent orchestration tests (80% coverage)

#### Sprint 3 (Weeks 9–12): Production Readiness & Optimization

**Sprint Goal:** Hardening, optimization, observability, and production deployment preparation

**Stories:**

1. **Performance Optimization**
   - Profile LangGraph execution (flamegraph analysis)
   - Optimize Neo4j queries (query plans, index tuning)
   - Implement caching layers (Redis for retrieval, decision cache)
   - Load test: 1,000 concurrent users, 10,000 req/sec
   - **AC:** p99 latency < 5s, throughput > 1,000 req/s, CPU < 70%

2. **Observability & Monitoring**
   - Implement structured logging (JSON format, correlation IDs)
   - Deploy OpenTelemetry (traces, metrics, logs)
   - Create Grafana dashboards (latency, throughput, errors)
   - Set up alerting rules (SLA breaches, error spikes)
   - **AC:** Full trace visibility, <5min alert latency

3. **Security Hardening**
   - Conduct penetration testing (OWASP Top 10)
   - Implement secret rotation (Azure Key Vault)
   - Add data exfiltration detection (anomaly detection)
   - Encrypt Neo4j at-rest (via AKS persistent volumes)
   - **AC:** Zero critical findings post-penetration testing

4. **Documentation & Runbooks**
   - Write API client SDK (TypeScript, Python)
   - Create deployment runbook (AKS, Kafka, Neo4j)
   - Write troubleshooting guide
   - Document SLA and incident response procedures
   - **AC:** New team member can deploy in <1 hour

5. **Integration Testing**
   - LangGraph ↔ Neo4j end-to-end tests
   - LangGraph ↔ Azure AI end-to-end tests
   - Kafka topic integration tests
   - Audit trail validation tests
   - **AC:** All critical paths passing, >90% coverage

**Sprint 3 Deliverables:**
- Optimized, production-ready services
- Full observability stack (OpenTelemetry + Grafana)
- Security assessment report
- Deployment runbooks and SOP documentation
- API client SDKs (TypeScript, Python)

---

### Phase 4: Integration & Testing (3–4 weeks)

**Objectives:**
- Validate cross-service integration
- Perform security and compliance validation
- Execute performance and load testing
- Fix critical and high-priority issues

**Key Activities:**

1. **Integration Testing**
   - Compliance Agent → Neo4j → Azure OpenAI → Kafka → Audit Log
   - Onboarding Agent → external sanctions provider
   - Copilot Agent → retrieval service → caching layer
   - Distributed tracing: trace requests end-to-end

2. **Security Testing**
   - Penetration testing (OWASP Top 10, API security)
   - Data exposure risk testing (logs, backups, network traffic)
   - Access control validation (RBAC enforcement)
   - Encryption validation (TLS in-transit, AES at-rest)

3. **Performance & Load Testing**
   - GraphRAG retrieval latency at 1,000 QPS
   - Compliance check throughput (target: 100/min per pod)
   - Kafka pub/sub latency (target: <100ms)
   - Neo4j concurrent connection limits

4. **Compliance Validation**
   - Audit trail completeness (100% decision coverage)
   - PII redaction accuracy
   - Data retention policy enforcement
   - GDPR right-to-be-forgotten implementation

**Deliverables:**
- Integration test report
- Security assessment report (remediation backlog)
- Load test results and performance tuning recommendations
- Compliance validation report

**Acceptance Criteria:**
- No critical or exploitable security findings
- All high/medium findings mitigated or accepted
- Performance targets met (p99 < 5s, throughput > 1,000 req/s)
- 100% audit trail coverage for all decisions

---

### Phase 5: Security, Compliance & Governance (concurrent with Phases 3–4; final sign-off 2 weeks)

**Objectives:**
- Achieve security and compliance sign-off
- Implement governance controls
- Document operational playbooks

**Key Activities:**

1. **Encryption & Key Management**
   - Azure Key Vault setup: keys for Neo4j, Kafka, Azure OpenAI
   - Key rotation policy (90-day automatic rotation)
   - Backup encryption (KMS-managed)
   - Test key recovery procedures

2. **Data Classification & Handling**
   - Tag all data with classification level
   - Implement automatic output redaction (PII, financial data)
   - Human-in-loop gating for high-risk or sensitive decisions
   - Create data handling playbooks

3. **Audit & Compliance**
   - Audit retention policy: 7 years (regulatory requirement)
   - Immutable log storage (Azure Blob with object lock)
   - Audit log analysis and anomaly detection
   - Compliance sampling (monthly review of 100 decisions)

4. **Operational Playbooks**
   - Data breach response procedures
   - Model misuse / drift detection and response
   - Incident escalation matrix
   - Change management and rollback procedures

**Deliverables:**
- Security Assessment & Compliance Sign-Off (SOC 2, ISO 27001 alignment)
- Key Management Plan
- Data Classification & Handling Guide
- Operational Playbooks (incident response, escalation, etc.)

**Acceptance Criteria:**
- Written compliance and security team sign-off
- All operational playbooks documented and tested
- No outstanding P1 findings

---

### Phase 6: Staging & Production Rollout (2–3 weeks)

**Objectives:**
- Deploy to staging environment with production-like data
- Execute user acceptance testing (UAT)
- Perform controlled rollout to production

**Key Activities:**

1. **Staging Deployment**
   - Deploy all services to staging AKS cluster
   - Load anonymized production data (100,000 policy records, 10,000 clients)
   - Validate end-to-end functionality
   - Stress test at expected production load

2. **User Acceptance Testing (UAT)**
   - Compliance analysts review 50 compliance decisions
   - Risk managers review 20 KYC assessments
   - Copilot user testing: 10 analysts for 1 week
   - Collect feedback and defect reports

3. **Production Rollout**
   - **Day 1 (Monday):** Canary deployment (5% of traffic)
   - **Day 2–3:** Monitor canary (error rate, latency, business KPIs)
   - **Day 4:** Gradual ramp (25% → 50% → 75% → 100%)
   - **Week 2:** Full traffic, stabilization window
   - **Rollback plan:** Blue/green deployment with 1-click rollback

4. **Training & Handover**
   - Train Support team on common issues and troubleshooting
   - Train Operations team on monitoring, alerting, and on-call procedures
   - Conduct knowledge transfer sessions (architecture, workflows, runbooks)
   - Set up war room for first 2 weeks post-launch

**Deliverables:**
- Production Helm charts and manifests
- Secrets templates (encrypted)
- Runbooks: deployment, rollback, scaling, incident response
- Monitoring dashboards and alert rules (Grafana)
- UAT sign-off report
- Training materials and handover checklist

**Acceptance Criteria:**
- UAT sign-off from Compliance, Risk, and Operations
- Zero critical production incidents in first 2 weeks
- <99.9% uptime SLA maintained
- <5s p99 latency maintained

---

### Phase 7: Operate & Continuous Improvement (Ongoing)

**Objectives:**
- Maintain SLAs and performance
- Continuous improvement based on user feedback
- Regular policy re-ingestion and model updates

**Key Activities:**

1. **Operations & Monitoring**
   - Daily monitoring review (latency, throughput, errors)
   - Weekly SLA reporting to stakeholders
   - Monthly cost analysis and optimization
   - Capacity planning (scale-up triggers)

2. **Maintenance Cadence**
   - Policy re-ingestion: weekly automated refresh
   - Model updates: monthly (new compliance rules, regulatory changes)
   - Security patches: within 48 hours of availability
   - Infrastructure upgrades: quarterly during maintenance windows

3. **Enhancement Backlog**
   - Collect user feedback (monthly surveys, interviews)
   - Prioritize feature requests against business value
   - Iterate on UI/UX based on usage patterns
   - Expand agent capabilities (e.g., new regulatory domains)

**Deliverables:**
- Monthly SLA reports
- Quarterly business reviews (cost, adoption, ROI)
- Enhancement roadmap
- Knowledge transfer documentation for new team members

---

## Part 3: Sample Commands & Checklists

### OpenAPI Code Generation

```bash
# Install openapi-generator-cli (requires Java 11+)
npm install -g @openapitools/openapi-generator-cli

# Generate Node.js Express server
openapi-generator-cli generate \
  -i openapi.yaml \
  -g nodejs-express-server \
  -o generated/server \
  --config codegen-config.json

# Generate TypeScript Axios client
openapi-generator-cli generate \
  -i openapi.yaml \
  -g typescript-axios \
  -o generated/client \
  --package-name @barclays/agentic-ai-client

# Generate Python client
openapi-generator-cli generate \
  -i openapi.yaml \
  -g python \
  -o generated/python-client
```

### Neo4j Setup & Import

```bash
# Create Neo4j cluster (3-node HA setup on AKS)
helm repo add neo4j https://neo4j.com/helm-charts
helm install neo4j neo4j/neo4j \
  --namespace databases \
  --create-namespace \
  --values neo4j-values.yaml

# Import CSV data (run inside Neo4j pod)
kubectl exec -n databases neo4j-0 -- neo4j-admin import \
  --nodes=Compliance_Rule=/data/compliance_rules.csv \
  --nodes=Policy=/data/policies.csv \
  --nodes=Regulation=/data/regulations.csv \
  --nodes=Client=/data/clients.csv \
  --nodes=Document=/data/documents.csv \
  --relationships=CONTAINS=/data/policy_rules_rels.csv \
  --relationships=IMPLEMENTS=/data/rule_regulation_rels.csv \
  --relationships=SUBMITTED=/data/client_document_rels.csv

# Create indexes (via Cypher)
kubectl port-forward -n databases neo4j-0 7687:7687
cypher-shell -u neo4j -p $NEO4J_PASSWORD << 'EOF'
  CREATE CONSTRAINT policy_unique ON (p:Policy) ASSERT p.policy_id IS UNIQUE;
  CREATE CONSTRAINT rule_unique ON (c:Compliance_Rule) ASSERT c.rule_id IS UNIQUE;
  CREATE INDEX policy_fulltext ON (p:Policy) FOR (p.policy_text);
  CREATE INDEX rule_keyword ON (c:Compliance_Rule) FOR (c.rule_description);
EOF
```

### Kafka Topic Creation

```bash
# Create Kafka topics for async processing
kafka-topics.sh --bootstrap-server kafka:9092 --create \
  --topic compliance-decisions \
  --partitions 6 \
  --replication-factor 3 \
  --config retention.ms=604800000 \
  --config min.insync.replicas=2

kafka-topics.sh --bootstrap-server kafka:9092 --create \
  --topic onboarding-events \
  --partitions 3 \
  --replication-factor 3

kafka-topics.sh --bootstrap-server kafka:9092 --create \
  --topic audit-events \
  --partitions 12 \
  --replication-factor 3 \
  --config compression.type=snappy
```

### Azure Resources Creation

```bash
# Create resource group
az group create \
  --name rg-agentic-prod \
  --location northeurope

# Create Key Vault
az keyvault create \
  --name kv-agentic-prod \
  --resource-group rg-agentic-prod \
  --location northeurope \
  --enable-purge-protection \
  --enable-soft-delete-retention 90

# Create Managed Identity
az identity create \
  --resource-group rg-agentic-prod \
  --name mi-agentic-aks

# Assign Key Vault access to Managed Identity
az keyvault set-policy \
  --name kv-agentic-prod \
  --object-id $(az identity show --resource-group rg-agentic-prod \
    --name mi-agentic-aks --query principalId -o tsv) \
  --key-permissions get list \
  --secret-permissions get list

# Create AKS cluster
az aks create \
  --resource-group rg-agentic-prod \
  --name aks-agentic-prod \
  --node-count 3 \
  --vm-set-type VirtualMachineScaleSets \
  --zones 1 2 3 \
  --network-plugin azure \
  --network-policy azure \
  --enable-managed-identity \
  --enable-addons monitoring \
  --enable-secret-rotation \
  --enable-oidc-issuer

# Create Azure Search (for GraphRAG embeddings)
az search service create \
  --name search-agentic-prod \
  --resource-group rg-agentic-prod \
  --sku standard \
  --location northeurope

# Create Azure OpenAI deployment
az cognitiveservices account deployment create \
  --name openai-agentic-prod \
  --resource-group rg-agentic-prod \
  --deployment-id gpt4-prod \
  --model name=gpt-4 version=0613 \
  --sku-name standard \
  --sku-capacity 50
```

### AKS Deployment Checklist

```bash
# 1. Get AKS credentials
az aks get-credentials \
  --resource-group rg-agentic-prod \
  --name aks-agentic-prod \
  --overwrite-existing

# 2. Create namespaces
kubectl create namespace agentic
kubectl create namespace databases
kubectl create namespace events
kubectl create namespace monitoring

# 3. Create secrets
kubectl create secret generic azure-openai-secret \
  --from-literal=api-key=$AZURE_OPENAI_KEY \
  --from-literal=endpoint=$AZURE_OPENAI_ENDPOINT \
  -n agentic

# 4. Deploy Neo4j
helm install neo4j neo4j/neo4j \
  -n databases \
  -f neo4j-values.yaml

# 5. Deploy Kafka
helm install kafka bitnami/kafka \
  -n events \
  -f kafka-values.yaml

# 6. Deploy services (Helm)
helm upgrade --install agentic-platform ./charts/agentic-platform \
  -n agentic \
  --create-namespace \
  --values values-prod.yaml

# 7. Verify deployment
kubectl rollout status deployment/api-gateway -n agentic
kubectl rollout status deployment/compliance-agent -n agentic
kubectl rollout status deployment/copilot-agent -n agentic

# 8. Port-forward for testing
kubectl port-forward -n agentic svc/api-gateway 8080:8080

# 9. Monitor logs
kubectl logs -n agentic deployment/api-gateway -f --all-containers=true

# 10. Check resource usage
kubectl top nodes
kubectl top pods -n agentic
```

### Compliance Check Workflow (curl examples)

```bash
# 1. Authenticate and get bearer token
TOKEN=$(curl -s -X POST https://login.microsoftonline.com/${TENANT_ID}/oauth2/v2.0/token \
  -d client_id=$CLIENT_ID \
  -d client_secret=$CLIENT_SECRET \
  -d scope='https://agentic-api.barclays.com/.default' \
  -d grant_type=client_credentials | jq -r .access_token)

# 2. Upload document for compliance check
curl -X POST https://agentic-api.barclays.com/v1/api/agents/compliance/check \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "request_id": "req_2024_001",
    "document_id": "doc_credit_agreement_001",
    "document_text": "This Credit Facility Agreement dated 1 January 2024...",
    "client_id": "client_ABC_CORP",
    "metadata": {
      "document_type": "credit_agreement",
      "jurisdiction": "GB",
      "business_unit": "INVESTMENT_BANKING"
    }
  }' | jq .

# 3. Retrieve decision result
curl -X GET https://agentic-api.barclays.com/v1/api/agents/compliance/check/req_2024_001 \
  -H "Authorization: Bearer $TOKEN" | jq .

# 4. Query compliance copilot
curl -X POST https://agentic-api.barclays.com/v1/api/copilot/query \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "analyst_john_doe",
    "session_id": "sess_2024_001",
    "query": "What are the current KYC requirements for non-resident corporates?",
    "context": {
      "jurisdiction": "GB",
      "client_type": "corporate"
    }
  }' | jq .

# 5. Retrieve audit trail for a decision
curl -X GET 'https://agentic-api.barclays.com/v1/api/audit/logs?request_id=req_2024_001' \
  -H "Authorization: Bearer $TOKEN" | jq '.audit_events[] | {event_type, timestamp, actor, action}'
```

---

## Part 4: Risk Mitigation & Success Factors

### Key Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|-----------|
| **LLM output unreliability** | High | Use retrieval grounding, redaction, human-in-loop gating, confidence thresholds |
| **Data quality & schema drift** | High | Scheduled re-ingestion validation, automated data quality checks, alert on anomalies |
| **Performance at scale** | High | Early load testing, GraphRAG result caching, query optimization, horizontal scaling |
| **Regulatory/Compliance drift** | Medium | Automated policy sync, change management workflow, quarterly compliance audits |
| **Azure cost overruns** | Medium | Implement token budgets, query cost monitoring, fallback to local LLM for dev/test |
| **Security/data exposure** | Critical | PII redaction, encryption, network segmentation, penetration testing, audit logging |
| **Neo4j / Kafka operational complexity** | Medium | Use managed Kubernetes operators, automated backups, runbooks for common issues |

### Success Factors

1. **Strong LangGraph discipline:** Use explicit nodes and typed state; avoid loose prompt chaining
2. **Comprehensive provenance:** Track every decision decision source (policy, evidence, model output, human reviewer)
3. **Human-in-loop gating:** Route high-risk or low-confidence decisions to compliance officers
4. **Observability from day 1:** Traces, metrics, logs for every request; quick root cause analysis
5. **Incremental adoption:** Start with compliance checks (lower risk), then expand to onboarding and copilot
6. **Clear RACI:** Define ownership for each service, alert escalation, and incident response
7. **Realistic LLM expectations:** Use LLM for analysis and summarization, deterministic rules for critical decisions
8. **Cost monitoring:** Track Azure OpenAI spend daily; implement budget alerts and fallback strategies

---

## Part 5: Grading & Success Criteria

### For Enterprise Deployment:

✅ **Architecture & Design:**
- LangGraph orchestration with explicit nodes and typed state
- Shared `AgentState` contract across all agents
- Tool registry with input/output schemas, timeouts, retries

✅ **Implementation Quality:**
- Production-ready services (error handling, logging, monitoring)
- Comprehensive test coverage (unit: 80%, integration: 70%)
- Security assessment passed (no critical findings)

✅ **Compliance & Auditability:**
- 100% of decisions logged with full provenance
- Immutable audit trail (Event Hub)
- PII redaction and data classification enforcement

✅ **Operational Excellence:**
- SLA targets met (p99 latency < 5s, throughput > 1,000 req/s)
- Runbooks for common operations and incident response
- Knowledge transfer and team handover completed

### For PoC / Academic Assessment:

✅ **Design & Documentation:**
- Clear architecture diagrams and workflow descriptions
- Well-defined API contracts (OpenAPI spec)
- Strong justification for technology choices

✅ **Core Functionality:**
- End-to-end workflow execution (ingestion → retrieval → decision → audit)
- LangGraph orchestration demonstrated
- Provenance and traceability implemented

✅ **Code Quality & Testing:**
- Clean, well-documented code
- Integration tests demonstrating key workflows
- No obvious security vulnerabilities

---

## Next Steps

1. **Week 1:** Review this implementation plan with Investment Banking leadership and security/compliance teams
2. **Week 2:** Approve Phase 0 scope and begin stakeholder interviews
3. **Week 3:** Finalize Phase 1 design deliverables (OpenAPI, Neo4j schema, LangGraph workflows)
4. **Week 4:** Spin up temporary AKS/Neo4j resources for PoC
5. **Week 5–8:** Execute Phase 2 PoC
6. **Week 9–20:** Execute Phase 3 core implementation (3 sprints)
7. **Week 21–24:** Phase 4–6 (integration, testing, rollout)
8. **Week 25+:** Phase 7 (operations and continuous improvement)

---

**Document Owner:** Agentic AI Platform Lead
**Last Updated:** 2024
**Next Review Date:** Monthly during Phase 3+
