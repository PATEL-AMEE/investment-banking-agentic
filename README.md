# Investment Banking Agentic AI Platform

This project implements a production-like prototype for an investment banking agentic AI workflow using FastAPI, a LangGraph-inspired orchestration layer, a graph-style in-memory store, and Azure deployment assets.

## What is included
- FastAPI backend with:
  - POST /api/agents/inspect — compliance check workflow
  - POST /api/agents/onboarding/kyc — KYC/AML onboarding agent (sanctions + PEP screening, risk scoring, escalation)
  - POST /api/documents/upload
  - POST /api/copilot/query — policy Q&A with retrieved citations
  - GET /api/audit/logs — append-only audit trail (optionally filtered by request_id)
  - GET /api/dashboard/summary — live aggregate (real client decisions, review queue, audit) for the dashboard
  - GET /dashboard — live analytics dashboard UI (real output from the running app)
  - GET /overview — overview dashboard UI (projected design-target figures)
  - GET /health
- Demo data loaded from the CSV files in the data directory.
- A simple workflow engine that evaluates client risk and returns provenance.
- Docker and Azure Container Apps deployment scaffolding.

## Run locally
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn app.api:app --reload
```

### Optional: run a local Neo4j backend
1. Start Neo4j using Docker Compose:
```powershell
docker compose -f docker-compose.neo4j.yml up -d
```
2. Seed the local Neo4j instance:
```powershell
.\.venv\Scripts\python.exe scripts/seed_neo4j.py
```
3. Start the app with Neo4j environment variables:
```powershell
$env:NEO4J_URI = "bolt://localhost:7687"
$env:NEO4J_USER = "neo4j"
$env:NEO4J_PASSWORD = "test"
uvicorn app.api:app --reload
```
4. Verify the inspect flow against Neo4j:
```powershell
.\.venv\Scripts\python.exe scripts/verify_neo4j_inspect.py
```

## Run with Docker
```bash
docker build -t investment-banking-agentic-api .
docker run -p 8000:8000 investment-banking-agentic-api
```

## Deploy to Azure (Container Apps)

Builds the image in the cloud with `az acr build` — **no local Docker required**.

Prerequisites:
- Azure CLI installed (https://aka.ms/installazurecli)
- `az login` (and `az account set --subscription <id>` if you have more than one)

Demo deploy (in-memory store, no auth):
```bash
bash infra/deploy.sh
```

Production deploy (persistent Neo4j Aura + Azure AD auth):
```bash
NEO4J_URI="neo4j+s://xxxxxxxx.databases.neo4j.io" \
NEO4J_USER="neo4j" \
NEO4J_PASSWORD="<your-aura-password>" \
ENABLE_AZURE_AD=true \
AZURE_AD_TENANT_ID="<tenant-id>" \
AZURE_AD_CLIENT_ID="<app-registration-client-id>" \
bash infra/deploy.sh
```

The Neo4j password is stored as a Container App secret. On success the script
prints the public URL (`/health`, `/docs`, `/reviewer`). Override `RESOURCE_GROUP`,
`LOCATION`, `ACR_NAME`, etc. via environment variables as needed (ACR names must be
globally unique).

## Notes for the course
- The implementation uses a production-like architecture pattern and strong audit/provenance handling.
- Azure OpenAI and Azure AI Search can be integrated next by replacing the mock workflow nodes with service clients.
