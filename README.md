# Investment Banking Agentic AI Platform

Enterprise Agentic AI platform for an investment banking division (see
`Use_case.md`): LangGraph agents orchestrating regulatory compliance,
client onboarding, and knowledge retrieval, communicating over **MCP**,
grounded by **GraphRAG** (Neo4j + vector retrieval), deployed on **Azure
Kubernetes Service** with Kafka event streaming, Azure Monitor
observability, and end-to-end security controls across all LLM endpoints.

## Capabilities

- **LangGraph agents** — compliance, onboarding/KYC, copilot, document
  analysis, client profiling (`app/agents/`).
- **MCP inter-agent protocol** — agents exposed as JSON-RPC 2.0 tools
  (`app/mcp/`); inter-agent calls run through audited `tools/call`;
  external MCP hosts connect at `POST /api/mcp`.
- **GraphRAG** — Neo4j knowledge graph + vector retrieval with graph
  enrichment (`app/services/retrieval.py`).
- **NLP pipeline** — NER, contract clause extraction, regulatory document
  classification (`app/services/nlp_pipeline.py`; rule-based by default,
  spaCy / HF Transformers via `requirements-ml.txt`).
- **Security** — prompt-injection defence, PII redaction (regex or
  Microsoft Presidio), RBAC on LLM endpoints, HMAC-signed hash-chained
  audit trail (`app/services/dlp.py`, `app/services/audit.py`).
- **LLM adapter** — Azure OpenAI → **Vertex AI (incl. fine-tuned adapter
  endpoints)** → OpenAI-compatible → deterministic mock
  (`app/services/llm_adapter.py`).
- **Evaluation harness** — RAGAS-style faithfulness / hallucination /
  relevancy / context precision-recall over the RAG chain
  (`app/eval/harness.py`, `scripts/run_evaluation.py`, `POST /api/eval/run`).
- **Observability** — OpenTelemetry spans for agents, tools, MCP, NLP, and
  LLM calls, exported to Azure Monitor / Application Insights.

## Key API endpoints

| Endpoint | Purpose |
|---|---|
| `POST /api/agents/inspect` | Compliance decision workflow |
| `POST /api/agents/onboarding/kyc` | KYC/AML onboarding (sanctions + PEP screening) |
| `POST /api/documents/upload` | Document ingestion + NLP enrichment |
| `POST /api/copilot/query` | Grounded policy Q&A (RBAC-gated) |
| `POST /api/nlp/analyze` | NER / clauses / classification |
| `POST /api/mcp` · `GET /api/mcp/tools` | MCP JSON-RPC endpoint + tool catalogue |
| `POST /api/eval/run` | RAG evaluation report |
| `GET /api/audit/logs` · `GET /api/audit/verify` | Signed audit trail + chain verification |
| `GET /dashboard` · `GET /docs` | Live dashboard · OpenAPI docs |

## Run locally

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Optional ML extras (spaCy NER, Presidio DLP, transformers, RAGAS):
# pip install -r requirements-ml.txt; python -m spacy download en_core_web_sm
pytest -q
uvicorn app.api:app --reload    # http://localhost:8000/docs
```

Runs fully offline by default (in-memory store, mock LLM, rule-based NLP).
Configure real providers via environment variables — see the configuration
table in `Azure_AKS_Implementation_Guide.md` §4.

### Optional: local Neo4j backend

```powershell
docker compose -f docker-compose.neo4j.yml up -d
.\.venv\Scripts\python.exe scripts/seed_neo4j.py
$env:NEO4J_URI = "bolt://localhost:7687"; $env:NEO4J_USER = "neo4j"; $env:NEO4J_PASSWORD = "test"
uvicorn app.api:app --reload
```

## Deploy to Azure

**AKS (primary — matches the use case):** full walkthrough in
[`Azure_AKS_Implementation_Guide.md`](Azure_AKS_Implementation_Guide.md);
automated happy path (cloud image build, cluster + monitoring, manifests):

```bash
az login
bash infra/deploy_aks.sh
```

**Azure Container Apps (free-tier quick-deploy — scale-to-zero free grant):**

```bash
bash infra/deploy.sh
```

**Free tier only?** See the guide's §3b free-tier playbook: GitHub Models
as the LLM, in-memory store, in-process events, rule-based NLP/DLP, and
Container Apps hosting are all free; AKS runs on trial credit (the deploy
script auto-falls back to a local Docker build where ACR Tasks are
blocked — stop the cluster with `az aks stop` when idle).

Kubernetes manifests live in `infra/k8s/` (namespace, ConfigMap,
hardened Deployment, LoadBalancer Service, HPA, secret template).

## Evaluation

```powershell
python scripts/run_evaluation.py   # writes a report to data/eval/results/
```

Golden Q&A dataset: `data/eval/golden_qa.json`.
