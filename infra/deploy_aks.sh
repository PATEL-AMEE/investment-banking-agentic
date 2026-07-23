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
# Application secrets are sourced from Azure Key Vault via the CSI
# secrets-store driver by default; set USE_KEY_VAULT=false to fall back to a
# raw Kubernetes Secret. Vault names are global — override on collision.
USE_KEY_VAULT="${USE_KEY_VAULT:-true}"
KEY_VAULT_NAME="${KEY_VAULT_NAME:-kv-investment-banking}"
# Event streaming: ENABLE_EVENT_HUBS=true provisions an Azure Event Hubs
# namespace (Standard tier — Kafka endpoint; ~£10/mo base, runs on trial
# credit) plus one hub per canonical topic, and points the app's Kafka event
# bus at it. Default off: the app falls back to its in-process bus.
ENABLE_EVENT_HUBS="${ENABLE_EVENT_HUBS:-false}"
EVENT_HUBS_NAMESPACE="${EVENT_HUBS_NAMESPACE:-ehns-investment-banking}"  # globally unique

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
for ns in Microsoft.ContainerRegistry Microsoft.ContainerService Microsoft.OperationalInsights microsoft.insights Microsoft.KeyVault; do
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

# --- Event Hubs (Kafka-compatible event streaming) ---------------------------
KAFKA_BOOTSTRAP_SERVERS=""
KAFKA_CONNECTION_STRING=""
if [[ "$ENABLE_EVENT_HUBS" == "true" ]]; then
  echo ">> Event Hubs namespace: $EVENT_HUBS_NAMESPACE (Kafka endpoint)"
  az provider register --namespace Microsoft.EventHub --wait
  az eventhubs namespace create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$EVENT_HUBS_NAMESPACE" \
    --location "$LOCATION" \
    --sku Standard >/dev/null
  # One hub per canonical topic (mirrors app/services/event_bus.py).
  for topic in documents.ingested clients.onboarded compliance.decisions \
               reviews.escalated reviews.resolved agents.supervisor.routed; do
    az eventhubs eventhub create \
      --resource-group "$RESOURCE_GROUP" \
      --namespace-name "$EVENT_HUBS_NAMESPACE" \
      --name "$topic" \
      --partition-count 1 >/dev/null 2>&1 || true
  done
  KAFKA_CONNECTION_STRING=$(az eventhubs namespace authorization-rule keys list \
    --resource-group "$RESOURCE_GROUP" \
    --namespace-name "$EVENT_HUBS_NAMESPACE" \
    --name RootManageSharedAccessKey \
    --query primaryConnectionString -o tsv)
  KAFKA_BOOTSTRAP_SERVERS="$EVENT_HUBS_NAMESPACE.servicebus.windows.net:9093"
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

# --- Secrets: Azure Key Vault via CSI driver (preferred) ---------------------
# Uploads each configured secret to Key Vault, generates a SecretProviderClass
# whose secretObjects sync the vault secrets into the `agentic-api-secrets`
# Kubernetes Secret the Deployment already consumes via envFrom, and mounts
# the CSI volume (mounting is what triggers the sync).
KV_OBJECTS=""
KV_SECRET_OBJECTS=""

add_kv_secret() {
  local env_key="$1" kv_name="$2" value="$3"
  [[ -z "$value" ]] && return 0
  az keyvault secret set --vault-name "$KEY_VAULT_NAME" \
    --name "$kv_name" --value "$value" --output none || return 1
  KV_OBJECTS+="        - |"$'\n'"          objectName: $kv_name"$'\n'"          objectType: secret"$'\n'
  KV_SECRET_OBJECTS+="      - objectName: $kv_name"$'\n'"        key: $env_key"$'\n'
}

setup_key_vault() {
  echo ">> Key Vault secrets-provider addon"
  if [[ "$(az aks show --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" \
      --query 'addonProfiles.azureKeyvaultSecretsProvider.enabled' -o tsv 2>/dev/null)" != "true" ]]; then
    az aks enable-addons --addons azure-keyvault-secrets-provider \
      --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" --output none || return 1
  fi
  KV_CLIENT_ID=$(az aks show --resource-group "$RESOURCE_GROUP" --name "$AKS_CLUSTER" \
    --query 'addonProfiles.azureKeyvaultSecretsProvider.identity.clientId' -o tsv) || return 1
  TENANT_ID=$(az account show --query tenantId -o tsv) || return 1

  echo ">> Key Vault: $KEY_VAULT_NAME"
  if ! az keyvault show --name "$KEY_VAULT_NAME" --resource-group "$RESOURCE_GROUP" >/dev/null 2>&1; then
    # Access-policy mode: the creating user gets full secret permissions
    # automatically and the addon identity is granted read below — no ARM
    # role-assignment propagation delays.
    az keyvault create --name "$KEY_VAULT_NAME" --resource-group "$RESOURCE_GROUP" \
      --location "$LOCATION" --enable-rbac-authorization false --output none || return 1
  fi
  az keyvault set-policy --name "$KEY_VAULT_NAME" --resource-group "$RESOURCE_GROUP" \
    --secret-permissions get list --spn "$KV_CLIENT_ID" --output none || return 1

  echo ">> Uploading application secrets to Key Vault"
  add_kv_secret AUDIT_SIGNING_KEY audit-signing-key "$AUDIT_SIGNING_KEY" || return 1
  add_kv_secret LOCAL_JWT_SECRET local-jwt-secret "$LOCAL_JWT_SECRET" || return 1
  add_kv_secret AZURE_OPENAI_ENDPOINT azure-openai-endpoint "$AZURE_OPENAI_ENDPOINT" || return 1
  add_kv_secret AZURE_OPENAI_API_KEY azure-openai-api-key "$AZURE_OPENAI_API_KEY" || return 1
  add_kv_secret LLM_BASE_URL llm-base-url "$LLM_BASE_URL" || return 1
  add_kv_secret LLM_API_KEY llm-api-key "$LLM_API_KEY" || return 1
  add_kv_secret NEO4J_URI neo4j-uri "$NEO4J_URI" || return 1
  add_kv_secret NEO4J_PASSWORD neo4j-password "$NEO4J_PASSWORD" || return 1
  add_kv_secret APPLICATIONINSIGHTS_CONNECTION_STRING appinsights-connection-string "$APPINSIGHTS_CONNECTION" || return 1
  add_kv_secret KAFKA_BOOTSTRAP_SERVERS kafka-bootstrap-servers "$KAFKA_BOOTSTRAP_SERVERS" || return 1
  add_kv_secret KAFKA_CONNECTION_STRING kafka-connection-string "$KAFKA_CONNECTION_STRING" || return 1

  echo ">> Applying SecretProviderClass (agentic-api-keyvault)"
  kubectl apply -f - <<SPC || return 1
apiVersion: secrets-store.csi.x-k8s.io/v1
kind: SecretProviderClass
metadata:
  name: agentic-api-keyvault
  namespace: agentic-platform
spec:
  provider: azure
  parameters:
    usePodIdentity: "false"
    useVMManagedIdentity: "true"
    userAssignedIdentityID: "$KV_CLIENT_ID"
    keyvaultName: "$KEY_VAULT_NAME"
    tenantId: "$TENANT_ID"
    objects: |
      array:
$KV_OBJECTS
  secretObjects:
    - secretName: agentic-api-secrets
      type: Opaque
      data:
$KV_SECRET_OBJECTS
SPC

  # The CSI driver must own the synced Secret — drop any raw predecessor.
  kubectl delete secret agentic-api-secrets --namespace agentic-platform --ignore-not-found >/dev/null

  echo ">> Mounting the Key Vault CSI volume on the deployment"
  kubectl patch deployment agentic-api --namespace agentic-platform --type=strategic -p '{
    "spec": {"template": {"spec": {
      "volumes": [{"name": "keyvault-secrets", "csi": {
        "driver": "secrets-store.csi.k8s.io", "readOnly": true,
        "volumeAttributes": {"secretProviderClass": "agentic-api-keyvault"}}}],
      "containers": [{"name": "api", "volumeMounts": [{
        "name": "keyvault-secrets", "mountPath": "/mnt/secrets-store", "readOnly": true}]}]
    }}}}' || return 1
}

if [[ "$USE_KEY_VAULT" == "true" ]]; then
  if ! setup_key_vault; then
    echo ">> Key Vault setup failed — falling back to a raw Kubernetes Secret."
    USE_KEY_VAULT="false"
  fi
fi

if [[ "$USE_KEY_VAULT" != "true" ]]; then
  echo ">> Creating/updating application secrets (raw Kubernetes Secret)"
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
  [[ -n "$KAFKA_BOOTSTRAP_SERVERS" ]] && SECRET_ARGS+=("--from-literal=KAFKA_BOOTSTRAP_SERVERS=$KAFKA_BOOTSTRAP_SERVERS")
  [[ -n "$KAFKA_CONNECTION_STRING" ]] && SECRET_ARGS+=("--from-literal=KAFKA_CONNECTION_STRING=$KAFKA_CONNECTION_STRING")
  kubectl create secret generic agentic-api-secrets \
    --namespace agentic-platform \
    "${SECRET_ARGS[@]}" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

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
