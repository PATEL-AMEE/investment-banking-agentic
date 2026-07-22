#!/usr/bin/env bash
#
# Deploy the Investment Banking Agentic AI API to Azure Container Apps.
#
# This script builds the container image in the cloud with `az acr build`
# (no local Docker required) and provisions everything with the Azure CLI.
#
# Prerequisites:
#   - Azure CLI installed:  https://aka.ms/installazurecli
#   - Logged in:            az login   (and: az account set --subscription <id>)
#
# Every setting below can be overridden via environment variables, e.g.:
#   NEO4J_URI="neo4j+s://xxxx.databases.neo4j.io" \
#   NEO4J_PASSWORD="****" \
#   ENABLE_AZURE_AD=true AZURE_AD_TENANT_ID=... AZURE_AD_CLIENT_ID=... \
#   bash infra/deploy.sh
#
set -euo pipefail

# --- Azure resource configuration -------------------------------------------
RESOURCE_GROUP="${RESOURCE_GROUP:-rg-investment-banking-agentic}"
LOCATION="${LOCATION:-northeurope}"
# NOTE: ACR names must be globally unique across Azure. Override ACR_NAME if
# creation fails with a name-taken error.
ACR_NAME="${ACR_NAME:-acrinvestmentbanking}"
CONTAINER_APP_ENV="${CONTAINER_APP_ENV:-cae-investment-banking}"
CONTAINER_APP_NAME="${CONTAINER_APP_NAME:-ca-investment-banking}"
IMAGE_NAME="${IMAGE_NAME:-investment-banking-agentic-api}"
IMAGE_TAG="${IMAGE_TAG:-latest}"

# --- Application settings (passed to the container) -------------------------
# Leave NEO4J_URI empty to run on the in-memory store (data resets on restart).
NEO4J_URI="${NEO4J_URI:-}"
NEO4J_USER="${NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${NEO4J_PASSWORD:-}"
# Set ENABLE_AZURE_AD=true and supply tenant/client ids to require auth.
ENABLE_AZURE_AD="${ENABLE_AZURE_AD:-false}"
AZURE_AD_TENANT_ID="${AZURE_AD_TENANT_ID:-}"
AZURE_AD_CLIENT_ID="${AZURE_AD_CLIENT_ID:-}"

IMAGE_REF="$ACR_NAME.azurecr.io/$IMAGE_NAME:$IMAGE_TAG"

echo ">> Resource group: $RESOURCE_GROUP ($LOCATION)"
az group create --name "$RESOURCE_GROUP" --location "$LOCATION" >/dev/null

echo ">> Container registry: $ACR_NAME"
az acr create --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" --sku Basic >/dev/null

az extension add --name containerapp --upgrade >/dev/null 2>&1 || true
az provider register --namespace Microsoft.App >/dev/null
az provider register --namespace Microsoft.OperationalInsights >/dev/null

echo ">> Building image in the cloud (az acr build): $IMAGE_REF"
az acr build --registry "$ACR_NAME" --image "$IMAGE_NAME:$IMAGE_TAG" .

echo ">> Container Apps environment: $CONTAINER_APP_ENV"
az containerapp env create \
  --name "$CONTAINER_APP_ENV" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" >/dev/null

# --- Assemble secrets and environment variables -----------------------------
SECRETS=()
ENV_VARS=("ENABLE_AZURE_AD=$ENABLE_AZURE_AD")

if [[ -n "$NEO4J_URI" ]]; then
  echo ">> Neo4j backend enabled: $NEO4J_URI"
  ENV_VARS+=("NEO4J_URI=$NEO4J_URI" "NEO4J_USER=$NEO4J_USER")
  if [[ -n "$NEO4J_PASSWORD" ]]; then
    SECRETS+=("neo4j-password=$NEO4J_PASSWORD")
    ENV_VARS+=("NEO4J_PASSWORD=secretref:neo4j-password")
  fi
else
  echo ">> No NEO4J_URI set - using in-memory store (data resets on restart)."
fi

if [[ "$ENABLE_AZURE_AD" == "true" ]]; then
  echo ">> Azure AD authentication enabled."
  ENV_VARS+=("AZURE_AD_TENANT_ID=$AZURE_AD_TENANT_ID" "AZURE_AD_CLIENT_ID=$AZURE_AD_CLIENT_ID")
fi

echo ">> Deploying container app: $CONTAINER_APP_NAME"
CREATE_ARGS=(
  --name "$CONTAINER_APP_NAME"
  --resource-group "$RESOURCE_GROUP"
  --environment "$CONTAINER_APP_ENV"
  --image "$IMAGE_REF"
  --target-port 8000
  --ingress external
  --registry-server "$ACR_NAME.azurecr.io"
  --registry-identity system
  --cpu 0.5 --memory 1.0Gi
  --min-replicas 1 --max-replicas 2
  --env-vars "${ENV_VARS[@]}"
)
if [[ ${#SECRETS[@]} -gt 0 ]]; then
  CREATE_ARGS+=(--secrets "${SECRETS[@]}")
fi

az containerapp create "${CREATE_ARGS[@]}" >/dev/null

FQDN=$(az containerapp show \
  --name "$CONTAINER_APP_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --query properties.configuration.ingress.fqdn -o tsv)

echo ""
echo ">> Deployed successfully."
echo ">>   App:      https://$FQDN"
echo ">>   Health:   https://$FQDN/health"
echo ">>   API docs: https://$FQDN/docs"
echo ">>   Reviewer: https://$FQDN/reviewer"
