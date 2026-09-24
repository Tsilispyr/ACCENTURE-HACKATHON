#!/usr/bin/env bash
# Deploy the API to Azure Container Apps, with Application Insights wired in.
#
# Handout section 11 and the Definition of Done ask for a deployed application
# and Azure-native observability. This is the shortest path that produces both
# from the image this repo already builds: one resource group, one App Insights
# resource, one Container Apps environment, one app.
#
#   az login
#   bash deployment/azure/deploy.sh
#
# Everything is named from RESOURCE_PREFIX and is idempotent - re-running
# updates the revision rather than creating a second stack.
#
# ONE REPLICA, ON PURPOSE. The API keeps LangGraph checkpoints in a MemorySaver,
# which lives in process memory. A paused run waiting for human approval exists
# in exactly one replica, so a second replica would answer the resume request
# with "no such thread" roughly half the time. Scaling this out means wiring
# the Postgres checkpointer first - langgraph-checkpoint-postgres is already a
# dependency. Until then, min and max replicas are both 1 and that is a
# correctness constraint, not a cost saving.

if [ -z "${BASH_VERSION:-}" ]; then
    if command -v bash >/dev/null 2>&1; then exec bash "$0" "$@"; fi
    echo "ERROR: this script needs bash." >&2
    exit 1
fi

set -euo pipefail

PREFIX="${RESOURCE_PREFIX:-hackathon2}"
LOCATION="${AZURE_LOCATION:-westeurope}"
GROUP="${AZURE_RESOURCE_GROUP:-${PREFIX}-rg}"
ENVIRONMENT="${PREFIX}-env"
APP="${PREFIX}-api"
INSIGHTS="${PREFIX}-insights"
REGISTRY="${AZURE_REGISTRY:-${PREFIX}acr}"
IMAGE_TAG="${IMAGE_TAG:-0.1.0}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="$ROOT/.env"

command -v az >/dev/null 2>&1 || {
    echo "ERROR: the Azure CLI is not installed. https://aka.ms/azure-cli" >&2
    exit 1
}
az account show >/dev/null 2>&1 || {
    echo "ERROR: not logged in. Run: az login" >&2
    exit 1
}
[ -f "$ENV_FILE" ] || {
    echo "ERROR: no .env. Run: bash scripts/preflight.sh" >&2
    exit 1
}

say() { echo "[azure] $*"; }

say "resource group $GROUP in $LOCATION"
az group create --name "$GROUP" --location "$LOCATION" --output none

# --- Application Insights, FIRST ------------------------------------------
# Created before the app, because the app needs its connection string at
# startup: observability.configure() reads the environment once, at import.
say "application insights $INSIGHTS"
az extension add --name application-insights --only-show-errors --yes >/dev/null 2>&1 || true
az monitor app-insights component create \
    --app "$INSIGHTS" --location "$LOCATION" --resource-group "$GROUP" \
    --application-type web --output none

CONNECTION="$(az monitor app-insights component show \
    --app "$INSIGHTS" --resource-group "$GROUP" \
    --query connectionString --output tsv)"
say "connection string retrieved"

# --- the image -------------------------------------------------------------
say "container registry $REGISTRY"
az acr create --name "$REGISTRY" --resource-group "$GROUP" \
    --sku Basic --admin-enabled true --output none 2>/dev/null || true

# Build in ACR rather than locally: no local Docker needed, and the image is
# built for linux/amd64 regardless of what this machine is.
say "building the image in ACR (this takes a few minutes)"
az acr build --registry "$REGISTRY" --image "${APP}:${IMAGE_TAG}" --file Dockerfile "$ROOT"

# --- secrets ---------------------------------------------------------------
# Read from .env and passed as Container Apps SECRETS, not plain env vars, so
# they are not readable from the portal's environment variable listing.
secret_args=()
env_args=()
while IFS='=' read -r key value; do
    case "$key" in ''|\#*) continue ;; esac
    value="${value%\"}"; value="${value#\"}"
    [ -z "$value" ] && continue
    case "$key" in
        *API_KEY|*SECRET|*PASSWORD|*CONNECTION_STRING)
            lower="$(echo "$key" | tr '[:upper:]_' '[:lower:]-')"
            secret_args+=("${lower}=${value}")
            env_args+=("${key}=secretref:${lower}")
            ;;
        *)
            env_args+=("${key}=${value}")
            ;;
    esac
done < "$ENV_FILE"

secret_args+=("appinsights-connection=${CONNECTION}")
env_args+=("APPLICATIONINSIGHTS_CONNECTION_STRING=secretref:appinsights-connection")
env_args+=("OTEL_SERVICE_NAME=${APP}")
# Chroma needs no server, so the deployed app has no database dependency.
env_args+=("VECTOR_BACKEND=chroma")

say "container apps environment $ENVIRONMENT"
az extension add --name containerapp --only-show-errors --yes >/dev/null 2>&1 || true
az containerapp env create \
    --name "$ENVIRONMENT" --resource-group "$GROUP" --location "$LOCATION" \
    --output none 2>/dev/null || true

say "deploying $APP"
az containerapp create \
    --name "$APP" --resource-group "$GROUP" --environment "$ENVIRONMENT" \
    --image "${REGISTRY}.azurecr.io/${APP}:${IMAGE_TAG}" \
    --registry-server "${REGISTRY}.azurecr.io" \
    --target-port 8000 --ingress external \
    --min-replicas 1 --max-replicas 1 \
    --secrets "${secret_args[@]}" \
    --env-vars "${env_args[@]}" \
    --output none 2>/dev/null \
|| az containerapp update \
    --name "$APP" --resource-group "$GROUP" \
    --image "${REGISTRY}.azurecr.io/${APP}:${IMAGE_TAG}" \
    --set-env-vars "${env_args[@]}" \
    --output none

URL="https://$(az containerapp show --name "$APP" --resource-group "$GROUP" \
    --query properties.configuration.ingress.fqdn --output tsv)"

echo
say "deployed"
echo "  API     : $URL"
echo "  Health  : $URL/healthz     (expect azure_monitor_enabled: true)"
echo "  Docs    : $URL/docs"
echo "  Traces  : Azure portal -> $INSIGHTS -> Transaction search"
echo
say "verify:  curl -s $URL/healthz"
