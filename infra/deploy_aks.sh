#!/usr/bin/env bash
#
# Deploy the Investment Banking Agentic AI platform to Azure Kubernetes
# Service (AKS). Companion to Azure_AKS_Implementation_Guide.md — this
# script automates the happy path:
#
#   resource group -> ACR -> AKS (ACR-attached) -> az acr build (cloud
#   build, no local Docker needed) -> Log Analytics + Azure Monitor
#   (Container Insights) -> kubectl apply -k infra/k8s -> external IP.
#
# Prerequisites:
#   - Azure CLI + kubectl installed (az aks install-cli)
#   - Logged in: az login  (and: az account set --subscription <id>)
#
# Overridable via env vars, e.g.:
#   AZURE_OPENAI_ENDPOINT=... AZURE_OPENAI_API_KEY=... bash infra/deploy_aks.sh
#
set -euo pipefail

# --- Azure resource configuration -------------------------------------------
# Free-tier defaults: 1 x B2s node (2 vCPU — fits free-trial quota), local
# Docker build fallback (free trials block ACR Tasks), monitoring optional.
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-investment-banking-aks}"
LOCATION="${LOCATION:-northeurope}"
ACR_NAME="${ACR_NAME:-acrinvestmentbanking}"   # must be globally unique
AKS_CLUSTER="${AKS_CLUSTER:-aks-investment-banking}"
NODE_COUNT="${NODE_COUNT:-1}"
NODE_SIZE="${NODE_SIZE:-Standard_B2s}"
IMAGE_NAME="${IMAGE_NAME:-investment-banking-agentic-api}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
# Set ENABLE_MONITORING=false to skip Log Analytics/Container Insights and
# keep credit burn minimal (in-app telemetry endpoints still work).
ENABLE_MONITORING="${ENABLE_MONITORING:-true}"
LOG_ANALYTICS_WORKSPACE="${LOG_ANALYTICS_WORKSPACE:-law-investment-banking}"
APP_INSIGHTS_NAME="${APP_INSIGHTS_NAME:-appi-investment-banking}"

# Convenience: pull LLM/Neo4j settings from the local .env when not already
# exported, so the cluster matches the local configuration (GitHub Models
# free tier by default).
if [[ -f .env ]]; then
  for key in LLM_BASE_URL LLM_API_KEY LLM_MODEL NEO4J_URI NEO4J_USER NEO4J_PASSWORD AZURE_OPENAI_ENDPOINT AZURE_OPENAI_API_KEY; do
    if [[ -z "${!key:-}" ]]; then
      value=$(grep -E "^${key}=" .env | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
      [[ -n "$value" ]] && export "$key=$value"
    fi
  done
fi

# --- Application secrets (empty values are simply omitted) -------------------
AZURE_OPENAI_ENDPOINT="${AZURE_OPENAI_ENDPOINT:-}"
AZURE_OPENAI_API_KEY="${AZURE_OPENAI_API_KEY:-}"
LLM_BASE_URL="${LLM_BASE_URL:-}"
LLM_API_KEY="${LLM_API_KEY:-}"
NEO4J_URI="${NEO4J_URI:-}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-}"
AUDIT_SIGNING_KEY="${AUDIT_SIGNING_KEY:-$(openssl rand -hex 32 2>/dev/null || echo change-me)}"
LOCAL_JWT_SECRET="${LOCAL_JWT_SECRET:-$(openssl rand -hex 32 2>/dev/null || echo change-me)}"

IMAGE_REF="$ACR_NAME.azurecr.io/$IMAGE_NAME:$IMAGE_TAG"

echo ">> Resource group: $RESOURCE_GROUP ($LOCATION)"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" >/dev/null

echo ">> Registering resource providers (first-time subscriptions)"
for ns in Microsoft.ContainerRegistry Microsoft.ContainerService Microsoft.OperationalInsights microsoft.insights; do
  az provider register --namespace "$ns" --wait
done

echo ">> Container registry: $ACR_NAME"
az acr create --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" --sku Basic >/dev/null 2>&1 || true

MONITOR_ARGS=()
APPINSIGHTS_CONNECTION=""
if [[ "$ENABLE_MONITORING" == "true" ]]; then
  echo ">> Log Analytics workspace: $LOG_ANALYTICS_WORKSPACE (Azure Monitor)"
  az monitor log-analytics workspace create \
    --resource-group "$RESOURCE_GROUP" \
    --workspace-name "$LOG_ANALYTICS_WORKSPACE" >/dev/null
  WORKSPACE_ID=$(az monitor log-analytics workspace show \
    --resource-group "$RESOURCE_GROUP" \
    --workspace-name "$LOG_ANALYTICS_WORKSPACE" --query id -o tsv)
  MONITOR_ARGS=(--enable-addons monitoring --workspace-resource-id "$WORKSPACE_ID")
fi

echo ">> AKS cluster: $AKS_CLUSTER ($NODE_COUNT x $NODE_SIZE) — takes ~5 min"
if ! az aks show --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" >/dev/null 2>&1; then
  az aks create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$AKS_CLUSTER" \
    --node-count "$NODE_COUNT" \
    --node-vm-size "$NODE_SIZE" \
    --attach-acr "$ACR_NAME" \
    --enable-managed-identity \
    "${MONITOR_ARGS[@]}" \
    --generate-ssh-keys >/dev/null
fi

if [[ "$ENABLE_MONITORING" == "true" ]]; then
  echo ">> Application Insights: $APP_INSIGHTS_NAME (agent/LLM traces)"
  az extension add --name application-insights --upgrade >/dev/null 2>&1 || true
  az monitor app-insights component create \
    --app "$APP_INSIGHTS_NAME" \
    --location "$LOCATION" \
    --resource-group "$RESOURCE_GROUP" \
    --workspace "$WORKSPACE_ID" \
    --application-type web >/dev/null 2>&1 || true
  APPINSIGHTS_CONNECTION=$(az monitor app-insights component show \
    --app "$APP_INSIGHTS_NAME" \
    --resource-group "$RESOURCE_GROUP" \
    --query connectionString -o tsv 2>/dev/null || echo "")
fi

build_local() {
  echo ">> Building image locally with Docker and pushing: $IMAGE_REF"
  az acr login --name "$ACR_NAME"
  docker build -t "$IMAGE_REF" .
  docker push "$IMAGE_REF"
}

if [[ "${LOCAL_BUILD:-false}" == "true" ]]; then
  build_local
else
  echo ">> Building image in the cloud (az acr build): $IMAGE_REF"
  # Free-trial subscriptions block ACR Tasks (TasksOperationsNotAllowed);
  # fall back to a local Docker build + push automatically.
  if ! az acr build --registry "$ACR_NAME" --image "$IMAGE_NAME:$IMAGE_TAG" .; then
    echo ">> Cloud build failed (free-trial ACR Tasks restriction?) — falling back to local Docker build."
    build_local
  fi
fi

echo ">> Fetching kubectl credentials"
az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" --overwrite-existing

echo ">> Applying Kubernetes manifests (infra/k8s)"
kubectl apply -k infra/k8s

echo ">> Creating/updating application secrets"
SECRET_ARGS=(
  "--from-literal=AUDIT_SIGNING_KEY=$AUDIT_SIGNING_KEY"
  "--from-literal=LOCAL_JWT_SECRET=$LOCAL_JWT_SECRET"
)
[[ -n "$AZURE_OPENAI_ENDPOINT" ]] && SECRET_ARGS+=("--from-literal=AZURE_OPENAI_ENDPOINT=$AZURE_OPENAI_ENDPOINT")
[[ -n "$AZURE_OPENAI_API_KEY" ]] && SECRET_ARGS+=("--from-literal=AZURE_OPENAI_API_KEY=$AZURE_OPENAI_API_KEY")
[[ -n "$LLM_BASE_URL" ]] && SECRET_ARGS+=("--from-literal=LLM_BASE_URL=$LLM_BASE_URL")
[[ -n "$LLM_API_KEY" ]] && SECRET_ARGS+=("--from-literal=LLM_API_KEY=$LLM_API_KEY")
[[ -n "$NEO4J_URI" ]] && SECRET_ARGS+=("--from-literal=NEO4J_URI=$NEO4J_URI")
[[ -n "$NEO4J_PASSWORD" ]] && SECRET_ARGS+=("--from-literal=NEO4J_PASSWORD=$NEO4J_PASSWORD")
[[ -n "$APPINSIGHTS_CONNECTION" ]] && SECRET_ARGS+=("--from-literal=APPLICATIONINSIGHTS_CONNECTION_STRING=$APPINSIGHTS_CONNECTION")
kubectl create secret generic agentic-api-secrets \
  --namespace agentic-platform \
  "${SECRET_ARGS[@]}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo ">> Pointing the deployment at the built image"
kubectl set image deployment/agentic-api api="$IMAGE_REF" --namespace agentic-platform
kubectl rollout restart deployment/agentic-api --namespace agentic-platform
kubectl rollout status deployment/agentic-api --namespace agentic-platform --timeout=300s

echo ">> Waiting for the LoadBalancer external IP..."
EXTERNAL_IP=""
for _ in $(seq 1 30); do
  EXTERNAL_IP=$(kubectl get service agentic-api --namespace agentic-platform \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null || echo "")
  [[ -n "$EXTERNAL_IP" ]] && break
  sleep 10
done

echo ""
echo ">> Deployed successfully."
echo ">>   App:       http://$EXTERNAL_IP/"
echo ">>   Health:    http://$EXTERNAL_IP/health"
echo ">>   API docs:  http://$EXTERNAL_IP/docs"
echo ">>   Dashboard: http://$EXTERNAL_IP/dashboard"
echo ">>   MCP tools: http://$EXTERNAL_IP/api/mcp/tools"
