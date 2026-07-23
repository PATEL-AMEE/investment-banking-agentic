# Azure AKS Implementation Guide — Enterprise Agentic AI Platform

Start-to-end guide for implementing and deploying the Investment Banking
Agentic AI Platform (per `Use_case.md`) on **Azure Kubernetes Service**,
with Docker, Apache Kafka, Azure Monitor observability, and end-to-end
security controls across all LLM endpoints.

> Quick path: `az login`, then `bash infra/deploy_aks.sh` automates §5–§8.
> The sections below explain every step so you can run them manually,
> adapt them, or present them.

---

## Table of Contents

1. [Architecture overview](#1-architecture-overview)
2. [Use-case capability → code map](#2-use-case-capability--code-map)
3. [Prerequisites](#3-prerequisites)
4. [Local development & verification](#4-local-development--verification)
5. [Provision Azure resources](#5-provision-azure-resources)
6. [Build the container image](#6-build-the-container-image)
7. [Deploy to AKS](#7-deploy-to-aks)
8. [Secrets & Azure Key Vault](#8-secrets--azure-key-vault)
9. [Azure OpenAI (Copilot generation)](#9-azure-openai-copilot-generation)
10. [Kafka event streaming](#10-kafka-event-streaming)
11. [Vertex AI fine-tuned adapters](#11-vertex-ai-fine-tuned-adapters)
12. [RAGAS evaluation harness](#12-ragas-evaluation-harness)
13. [Security controls & OWASP LLM Top 10](#13-security-controls--owasp-llm-top-10)
14. [Observability with Azure Monitor](#14-observability-with-azure-monitor)
15. [Smoke tests](#15-smoke-tests)
16. [Cost control & teardown](#16-cost-control--teardown)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. Architecture overview

```
                         ┌──────────────────────────── AKS cluster ───────────────────────────┐
   Analyst / Reviewer    │  namespace: agentic-platform                                       │
        │ HTTPS          │  ┌──────────────────────────────────────────────────────────────┐  │
        ▼                │  │  agentic-api (Deployment, 2–5 replicas via HPA)              │  │
  ┌────────────┐  LB     │  │  FastAPI ── LangGraph agents:                                │  │
  │  Azure LB  ├────────▶│  │   • compliance    • onboarding   • copilot                   │  │
  └────────────┘         │  │   • document-analysis (+NLP)     • client-profiling          │  │
                         │  │  MCP server (/api/mcp): agents exposed as JSON-RPC tools;    │  │
   External MCP host ───▶│  │  inter-agent calls go through tools/call (audited)           │  │
                         │  │  Security: DLP (regex/Presidio) · prompt-injection guard ·   │  │
                         │  │  RBAC (Azure AD / JWT) · HMAC-signed hash-chained audit      │  │
                         │  └───────┬──────────────┬─────────────────┬─────────────────────┘  │
                         └──────────┼──────────────┼─────────────────┼────────────────────────┘
                                    │              │                 │
                     GraphRAG       ▼              ▼                 ▼
                  ┌───────────┐  ┌─────────────────────┐   ┌───────────────────┐
                  │  Neo4j    │  │ LLM providers        │   │ Kafka / Event Hubs│
                  │ (Aura or  │  │ Azure OpenAI │Vertex │   │ (event streaming) │
                  │  in-mem)  │  │ fine-tuned adapters  │   └───────────────────┘
                  └───────────┘  └─────────────────────┘
                                    Observability: Azure Monitor (Container Insights)
                                    + Application Insights (OTel traces: agents, tools, LLM)
```

## 2. Use-case capability → code map

| Use-case claim | Where it lives |
|---|---|
| LangGraph multi-agent orchestration | `app/agents/*.py` (StateGraphs), `app/services/workflow.py` |
| **MCP inter-agent protocol** | `app/mcp/server.py` (JSON-RPC 2.0 tools), `app/mcp/client.py`; HTTP at `POST /api/mcp`; copilot→retrieval hop runs over `tools/call` |
| GraphRAG on Neo4j + retrieval | `app/services/retrieval.py`, `app/services/neo4j_store.py`, `neo4j_schema.cypher` |
| Copilot (Azure OpenAI + RAG) | `app/agents/copilot.py`, `app/services/llm_adapter.py` |
| AKS + Docker + Kafka + Azure Monitor | `Dockerfile`, `infra/k8s/*`, `infra/deploy_aks.sh`, `app/services/event_bus.py`, `app/services/telemetry.py` |
| **NLP pipelines (spaCy / HF Transformers)** | `app/services/nlp_pipeline.py` (NER, clause extraction, classification); `nlp_enrich` node in `app/agents/document_analysis.py`; `POST /api/nlp/analyze` |
| **Security: injection defence, Presidio PII, RBAC, crypto audit** | `app/services/dlp.py` (regex + optional Presidio), `require_llm_access` in `app/api.py`, `app/services/audit.py` (SHA-256 chain + HMAC signatures) |
| **Vertex AI fine-tuned adapters** | `vertex` provider + `VERTEX_TUNED_ENDPOINT` in `app/services/llm_adapter.py` |
| **RAGAS-style evaluation** | `app/eval/harness.py`, `data/eval/golden_qa.json`, `scripts/run_evaluation.py`, `POST /api/eval/run` |
| Pen-testing / OWASP LLM Top 10 | §13 of this guide (control mapping) |

## 3. Prerequisites

- **Azure subscription** with quota for an AKS node pool (default: 1 ×
  `Standard_B2s`, which fits free-trial vCPU quota).
- **Azure CLI** ≥ 2.60 (`az login` completed) and **kubectl**
  (`az aks install-cli`).
- **Python 3.11+** locally for development and tests.
- Docker: only needed on free-trial subscriptions (ACR Tasks are blocked
  there; the deploy script falls back to a local `docker build` + push
  automatically). Paid subscriptions build in the cloud with `az acr build`.
- Optional: Azure OpenAI access, Neo4j Aura instance, Google Cloud project
  (Vertex AI), Event Hubs namespace.

### 3b. Free-tier / free-trial playbook

Everything in this project runs on free offerings; the only genuinely
non-free component is AKS **compute** (nodes + load balancer), which the
$200 free-trial credit covers for a demo period.

| Component | Free option |
|---|---|
| LLM | **GitHub Models** free tier (`LLM_BASE_URL=https://models.github.ai/inference`, fine-grained PAT with *Models: read*); Azure OpenAI free trials have 0 TPM quota |
| Graph store | In-memory `GraphStore` (leave `NEO4J_URI` unset) or **Neo4j Aura Free** |
| Kafka | In-process event bus (leave `KAFKA_BOOTSTRAP_SERVERS` unset); events still visible at `/api/events/recent` |
| NLP / DLP / eval | Rule-based engines — free and offline by default |
| Observability | In-app endpoints (`/api/telemetry/*`) are free; Log Analytics has a 5 GB/month free grant — or set `ENABLE_MONITORING=false` |
| **Hosting (free)** | **Azure Container Apps** scale-to-zero (`infra/deploy.sh`) — monthly free grant of 180k vCPU-s / 360k GiB-s / 2M requests |
| **Hosting (credit)** | AKS via `infra/deploy_aks.sh` — 1 × B2s ≈ $1/day + LB ≈ $0.60/day, paid from trial credit; **stop the cluster when idle** (`az aks stop`) |

Free-trial AKS deploy (auto-falls back to local Docker build when ACR
Tasks are blocked):

```bash
NODE_COUNT=1 ENABLE_MONITORING=false bash infra/deploy_aks.sh
az aks stop --resource-group rg-investment-banking-aks --name aks-investment-banking   # when done
```

## 4. Local development & verification

```bash
python -m venv .venv
.venv/Scripts/activate            # Windows; use source .venv/bin/activate on Linux/macOS
pip install -r requirements.txt   # core: FastAPI, LangGraph, Neo4j, Kafka, OTel
# Optional ML extras (Presidio, spaCy, transformers, RAGAS):
# pip install -r requirements-ml.txt && python -m spacy download en_core_web_sm

pytest -q                         # 64 tests, offline, mock LLM
uvicorn app.api:app --reload      # http://localhost:8000/docs
python scripts/run_evaluation.py  # RAG evaluation report -> data/eval/results/
```

The platform degrades gracefully: with no env configured it runs fully
offline (in-memory graph store, mock LLM, rule-based NLP, regex DLP). Every
production integration is switched on by environment variables:

| Variable | Purpose |
|---|---|
| `AZURE_OPENAI_ENDPOINT` / `AZURE_OPENAI_API_KEY` / `AZURE_OPENAI_DEPLOYMENT` | Azure OpenAI generation |
| `VERTEX_PROJECT` / `VERTEX_ACCESS_TOKEN` / `VERTEX_LOCATION` / `VERTEX_MODEL` | Vertex AI generation |
| `VERTEX_TUNED_ENDPOINT` | Serve a **fine-tuned adapter** endpoint instead of the base model |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | Any OpenAI-compatible gateway |
| `LLM_MODE=mock` | Force deterministic offline responses |
| `NEO4J_URI` / `NEO4J_USER` / `NEO4J_PASSWORD` | Neo4j GraphRAG backend |
| `NLP_ENGINE` = `rule-based` \| `spacy` \| `transformers` | NLP pipeline engine |
| `DLP_ENGINE` = `regex` \| `presidio` | PII redaction engine |
| `AUDIT_SIGNING_KEY` | HMAC key for signed audit entries (Key Vault in prod) |
| `ENABLE_AZURE_AD` + `AZURE_AD_TENANT_ID`/`AZURE_AD_CLIENT_ID` | Azure AD auth |
| `REQUIRE_AUTH=true` | Reject anonymous access (always set in prod) |
| `LOCAL_JWT_SECRET` | Dev JWT signing secret |
| `KAFKA_BOOTSTRAP_SERVERS` | Kafka / Event Hubs endpoint |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | Export OTel traces to Azure Monitor |

## 5. Provision Azure resources

```bash
RESOURCE_GROUP=rg-investment-banking-aks
LOCATION=northeurope
ACR_NAME=acrinvestmentbanking        # must be globally unique
AKS_CLUSTER=aks-investment-banking

az group create --name $RESOURCE_GROUP --location $LOCATION

# Container registry (cloud image builds + pulls)
az acr create --resource-group $RESOURCE_GROUP --name $ACR_NAME --sku Basic

# Log Analytics workspace (Azure Monitor / Container Insights)
az monitor log-analytics workspace create \
  --resource-group $RESOURCE_GROUP --workspace-name law-investment-banking
WORKSPACE_ID=$(az monitor log-analytics workspace show \
  --resource-group $RESOURCE_GROUP --workspace-name law-investment-banking \
  --query id -o tsv)

# AKS cluster with monitoring add-on, managed identity, ACR pull rights
az aks create \
  --resource-group $RESOURCE_GROUP \
  --name $AKS_CLUSTER \
  --node-count 2 \
  --node-vm-size Standard_B2s \
  --attach-acr $ACR_NAME \
  --enable-managed-identity \
  --enable-addons monitoring \
  --workspace-resource-id $WORKSPACE_ID \
  --generate-ssh-keys

# Application Insights (agent/LLM traces)
az monitor app-insights component create \
  --app appi-investment-banking \
  --location $LOCATION \
  --resource-group $RESOURCE_GROUP \
  --workspace $WORKSPACE_ID \
  --application-type web
```

## 6. Build the container image

The `Dockerfile` runs as a non-root user (uid 10001) to satisfy the pod
`securityContext` in `infra/k8s/deployment.yaml`.

```bash
# Cloud build — no local Docker needed
az acr build --registry $ACR_NAME \
  --image investment-banking-agentic-api:latest .

# (Alternative, with local Docker)
# docker build -t $ACR_NAME.azurecr.io/investment-banking-agentic-api:latest .
# az acr login --name $ACR_NAME
# docker push $ACR_NAME.azurecr.io/investment-banking-agentic-api:latest
```

## 7. Deploy to AKS

Manifests live in `infra/k8s/`: namespace, ConfigMap, Deployment (probes,
resource limits, hardened securityContext), LoadBalancer Service, and an
HPA (2–5 replicas at 70% CPU).

```bash
az aks get-credentials --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER

kubectl apply -k infra/k8s

# Secrets (see §8 for the Key Vault path)
cp infra/k8s/secret.example.yaml infra/k8s/secret.yaml   # fill in values
kubectl apply -f infra/k8s/secret.yaml

# Point the deployment at your ACR image and roll out
kubectl set image deployment/agentic-api \
  api=$ACR_NAME.azurecr.io/investment-banking-agentic-api:latest \
  -n agentic-platform
kubectl rollout status deployment/agentic-api -n agentic-platform

# Public endpoint
kubectl get service agentic-api -n agentic-platform   # EXTERNAL-IP column
```

For production ingress, replace the LoadBalancer Service with ClusterIP +
an ingress controller (AGIC or NGINX) terminating TLS with a certificate
from Key Vault.

## 8. Secrets & Azure Key Vault

Raw Kubernetes Secrets are fine for a demo; production should source them
from **Azure Key Vault** via the CSI secrets-store driver:

```bash
az keyvault create --name kv-investment-banking --resource-group $RESOURCE_GROUP
az keyvault secret set --vault-name kv-investment-banking --name audit-signing-key --value "$(openssl rand -hex 32)"
az keyvault secret set --vault-name kv-investment-banking --name azure-openai-api-key --value "<key>"

az aks enable-addons --addons azure-keyvault-secrets-provider \
  --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER
```

Then mount the vault with a `SecretProviderClass` and map the secrets into
the `agentic-api-secrets` Secret consumed by the Deployment's `envFrom`.
Key rules:

- `AUDIT_SIGNING_KEY` must be strong, rotated, and never the dev default —
  it underwrites the cryptographic audit trail.
- `REQUIRE_AUTH=true` and `ENABLE_AZURE_AD=true` in every non-dev
  environment (set in `infra/k8s/configmap.yaml`).

## 9. Azure OpenAI (Copilot generation)

```bash
az cognitiveservices account create \
  --name aoai-investment-banking \
  --resource-group $RESOURCE_GROUP \
  --location swedencentral \
  --kind OpenAI --sku S0

az cognitiveservices account deployment create \
  --name aoai-investment-banking \
  --resource-group $RESOURCE_GROUP \
  --deployment-name gpt-4o-mini \
  --model-name gpt-4o-mini --model-version "2024-07-18" \
  --model-format OpenAI --sku-name Standard --sku-capacity 10

az cognitiveservices account show --name aoai-investment-banking \
  --resource-group $RESOURCE_GROUP --query properties.endpoint
az cognitiveservices account keys list --name aoai-investment-banking \
  --resource-group $RESOURCE_GROUP --query key1
```

Put endpoint + key into the `agentic-api-secrets` Secret
(`AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_KEY`) and restart the
deployment. The adapter (`app/services/llm_adapter.py`) resolves providers
in order Azure OpenAI → Vertex AI → OpenAI-compatible → mock, and **always
falls back to mock on provider failure** so compliance workflows never
break when the LLM is down.

## 10. Kafka event streaming

The event bus (`app/services/event_bus.py`) publishes domain events
(document ingested, review escalated/resolved) in-process and mirrors them
to Kafka when `KAFKA_BOOTSTRAP_SERVERS` is set.

**Option A — Azure Event Hubs (Kafka-compatible, managed; recommended):**

```bash
az eventhubs namespace create --name ehns-investment-banking \
  --resource-group $RESOURCE_GROUP --location $LOCATION --sku Standard
az eventhubs eventhub create --name platform-events \
  --namespace-name ehns-investment-banking --resource-group $RESOURCE_GROUP
# Kafka endpoint: ehns-investment-banking.servicebus.windows.net:9093
# (SASL_SSL with the namespace connection string as password)
```

**Option B — Strimzi on AKS (self-managed, in-cluster):**

```bash
kubectl create namespace kafka
kubectl apply -f https://strimzi.io/install/latest?namespace=kafka -n kafka
# then apply a Kafka CR (1-broker for dev, 3 for HA) per Strimzi docs
```

Set `KAFKA_BOOTSTRAP_SERVERS` in the ConfigMap and restart. Verify with
`GET /api/events/recent`.

## 11. Vertex AI fine-tuned adapters

To serve LoRA-tuned models on niche regulatory topics (the fine-tuning
track of the use case):

1. In Google Cloud: prepare a JSONL supervised-tuning dataset from the
   compliance corpus (`{"contents": [...]}` pairs), run a Vertex AI tuning
   job on a Gemini base model, and deploy the tuned model to an
   **endpoint**. Note the endpoint id.
2. Give the platform credentials — short-lived token for a demo
   (`gcloud auth print-access-token`) or a service-account token minted by
   a sidecar/cron in production.
3. Configure the adapter (Secret values):

```
VERTEX_PROJECT=bank-compliance-llm
VERTEX_LOCATION=us-central1
VERTEX_ACCESS_TOKEN=<oauth token>
VERTEX_TUNED_ENDPOINT=<endpoint id>    # omit to use the base model (VERTEX_MODEL)
```

When Azure OpenAI variables are unset, the adapter routes chat through
`.../endpoints/<id>:generateContent`, reporting the model as
`tuned-endpoint:<id>` in `GET /api/telemetry/llm`. Measure the tuned
model's retrieval-accuracy lift with the evaluation harness (§12) — run it
once with the base model and once with `VERTEX_TUNED_ENDPOINT` set, and
compare aggregates.

## 12. RAGAS evaluation harness

`app/eval/harness.py` scores the copilot RAG/GraphRAG chain against the
golden dataset (`data/eval/golden_qa.json`):

- **faithfulness** / **hallucination_rate** — is each answer sentence
  grounded in the retrieved citations?
- **answer_relevancy** — cosine similarity between question and answer.
- **context_precision / context_recall** — retrieved citation ids vs the
  expected sources per case.

```bash
python scripts/run_evaluation.py                 # local, writes data/eval/results/
curl -X POST http://<EXTERNAL-IP>/api/eval/run   # in-cluster, audited run
```

The custom metrics are deterministic and run offline in CI. For LLM-judged
evaluation, install `ragas` (`requirements-ml.txt`) and feed the same
per-case fields (`question`, `answer`, `contexts`, `reference`) to RAGAS's
`faithfulness`/`answer_relevancy` metrics — the dataset shape is aligned
on purpose. Wire the script into CI to fail a release when aggregate
faithfulness drops below your threshold.

## 13. Security controls & OWASP LLM Top 10

| OWASP LLM risk | Platform control |
|---|---|
| LLM01 Prompt injection | `guard_prompt` screens every copilot query (pattern defence, refusal path in the agent graph); system prompts instruct the model to treat retrieved text as data, not instructions |
| LLM02 Insecure output handling | Answers are grounded in retrieved citations only; deterministic business rules stay outside the LLM; structured decision records |
| LLM03 Training-data poisoning | Fine-tuning corpora are curated exports (§11); ingestion masks PII before indexing |
| LLM04 Model DoS | Request timeouts (45 s), HPA limits, LLM token caps (`max_tokens`) |
| LLM05 Supply chain | Pinned `requirements.txt`, cloud image builds from source, non-root container |
| LLM06 Sensitive info disclosure | DLP masks PII before retrieval indexing, before generation, and before audit persistence (regex always; Presidio when `DLP_ENGINE=presidio`) |
| LLM07 Insecure plugin design | MCP tools declare JSON Schemas; arguments are validated; unknown tools/args are JSON-RPC errors; every `tools/call` is audited |
| LLM08 Excessive agency | Low-confidence/high-risk decisions route to human review (reviewer RBAC); agents can only call registered tools |
| LLM09 Overreliance | Provenance on every decision; citation ids in every answer; evaluation harness tracks hallucination rate |
| LLM10 Model theft | LLM endpoints require analyst/reviewer roles (`require_llm_access`); keys live in Key Vault; egress restricted to provider endpoints |

Additional controls: HMAC-signed, hash-chained, append-only audit trail
(`GET /api/audit/verify` reports `valid`/`signed`); Azure AD RBAC in prod
with local JWT parity in dev; hardened pod securityContext
(non-root, no privilege escalation, all capabilities dropped).

For the penetration-testing track: run the checks in
`tests/test_phase3_governance.py` and `tests/test_phase6_new_capabilities.py`
as the regression baseline, then scope an external test at the
`/api/copilot/query`, `/api/mcp`, and `/api/documents/upload` surfaces.

## 14. Observability with Azure Monitor

- **Container Insights** (enabled at cluster creation) — node/pod CPU,
  memory, restarts: Azure Portal → AKS cluster → Insights.
- **Application Insights** — the app exports OpenTelemetry spans
  (`agent.*`, `tool.*`, `mcp.tools/call.*`, `llm.chat`, `nlp.analyze`,
  `eval.run`) when `APPLICATIONINSIGHTS_CONNECTION_STRING` is set:
  Portal → Application Insights → Transaction search / Application map.
- **In-app**: `GET /api/telemetry/spans` (recent spans),
  `GET /api/telemetry/llm` (token usage + latency per model),
  `GET /api/events/recent` (domain events), `/dashboard` (live UI).

Useful KQL (Log Analytics):

```kusto
dependencies
| where name startswith "llm.chat"
| summarize count(), avg(duration) by tostring(customDimensions["llm.model"]), bin(timestamp, 1h)
```

## 15. Smoke tests

```bash
IP=$(kubectl get svc agentic-api -n agentic-platform -o jsonpath='{.status.loadBalancer.ingress[0].ip}')

curl http://$IP/health                                  # {"status":"ok"}
curl http://$IP/api/mcp/tools                           # MCP tool catalogue
curl -X POST http://$IP/api/copilot/query \
  -H 'Content-Type: application/json' \
  -d '{"query":"What is required for high-risk clients?"}'
curl -X POST http://$IP/api/nlp/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text":"This Agreement is governed by the laws of England. Payment of £1,000,000 is payable within 30 days."}'
curl -X POST http://$IP/api/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"sanctions_check","arguments":{"client_name":"Acme Corp"}}}'
curl http://$IP/api/audit/verify                        # {"valid":true,...,"signed":N}
curl -X POST http://$IP/api/eval/run                    # RAG evaluation report
```

With auth enabled, first obtain a token (`POST /api/auth/token` in dev, or
an Azure AD token in prod) and pass `Authorization: Bearer <token>`;
LLM-backed endpoints require the `analyst` or `reviewer` role.

## 16. Cost control & teardown

Approximate monthly cost (North Europe, default sizing): 2×`Standard_B2s`
nodes ≈ $60–75, Basic ACR ≈ $5, Log Analytics per-GB ingestion, Standard
LB ≈ $20; Azure OpenAI billed per token. Trim with `NODE_COUNT=1`,
`Standard_B2s`, and stopping the cluster when idle:

```bash
az aks stop  --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER
az aks start --resource-group $RESOURCE_GROUP --name $AKS_CLUSTER
```

Full teardown:

```bash
az group delete --name $RESOURCE_GROUP --yes --no-wait
```

## 17. Troubleshooting

| Symptom | Fix |
|---|---|
| `az acr create` name conflict | ACR names are global — set `ACR_NAME` to something unique |
| `az aks create` → `BadRequest: VM size ... not allowed` | Free-trial region restriction. Find a region where B-series is unrestricted (`az vm list-skus -l <region> --size Standard_B2 --query "[?length(restrictions)==\`0\`].name"`) and redeploy with `LOCATION=<region> NODE_SIZE=<sku>` (e.g. `LOCATION=swedencentral NODE_SIZE=Standard_B2als_v2`) |
| `az acr build` → `TasksOperationsNotAllowed` | Free-trial limitation: build locally with Docker and `docker push` (see §6 alternative) |
| Pods `ImagePullBackOff` | Cluster not attached to ACR: `az aks update -g $RESOURCE_GROUP -n $AKS_CLUSTER --attach-acr $ACR_NAME` |
| Pod `CreateContainerConfigError` | `agentic-api-secrets` missing — apply `infra/k8s/secret.yaml` (the ref is `optional`, but check the ConfigMap too) |
| EXTERNAL-IP stuck `<pending>` | Subscription lacks quota for a public LB IP; check `kubectl describe svc agentic-api -n agentic-platform` |
| Copilot answers say "LLM unavailable" | Provider env vars unset/wrong; check `GET /api/telemetry/llm` and pod env (`kubectl exec ... -- env \| grep -E 'AZURE_OPENAI\|VERTEX\|LLM_'`) |
| 403 on `/api/copilot/query`, `/api/mcp` | Caller's token lacks the `analyst`/`reviewer` role (`require_llm_access`) |
| `/api/audit/verify` returns `valid:false` | The JSONL trail was edited or the signing key changed mid-file — investigate before rotating; the report names the first invalid entry |
| OneDrive resurrects stale files | Known risk on this workspace: verify `app/*.py` mtimes after sync conflicts |
