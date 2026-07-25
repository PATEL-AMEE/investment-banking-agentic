#!/usr/bin/env bash
#
# Provision Azure Monitor alert rules for the Investment Banking Agentic AI
# platform, plus the Action Group that notifies on them.
#
# These are declared here rather than clicked in the portal so the alerting
# posture is reviewable and reproducible. Written as `az` CLI calls to match
# infra/deploy.sh; every rule is idempotent (re-running updates in place).
#
# Prerequisites:
#   - Azure CLI installed and logged in: az login
#   - The scheduled-query extension:     az extension add --name scheduled-query
#   - An Application Insights resource receiving telemetry from the app
#     (APPLICATIONINSIGHTS_CONNECTION_STRING set on the workload)
#
# Usage:
#   ALERT_EMAIL="compliance-eng@example.com" bash infra/alerts.sh
#
set -euo pipefail

# Git Bash / MSYS rewrites arguments that look like Unix paths into Windows
# paths, which corrupts Azure resource ids (/subscriptions/... arrives as
# C:/Program Files/Git/subscriptions/...). Unused on Linux and in CI.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL="*"

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-investment-banking-agentic}"
APP_INSIGHTS_NAME="${APP_INSIGHTS_NAME:-appi-investment-banking}"
ACTION_GROUP_NAME="${ACTION_GROUP_NAME:-ag-agentic-oncall}"
ACTION_GROUP_SHORT_NAME="${ACTION_GROUP_SHORT_NAME:-agenticops}"
ALERT_EMAIL="${ALERT_EMAIL:-}"

# Budget threshold for the daily-spend alert, in USD.
DAILY_COST_USD="${DAILY_COST_USD:-25}"

if [[ -z "$ALERT_EMAIL" ]]; then
  echo "ERROR: set ALERT_EMAIL to the address that should receive alerts." >&2
  exit 1
fi

echo "==> Resolving Application Insights resource id"
APP_INSIGHTS_ID="$(az monitor app-insights component show \
  --resource-group "$RESOURCE_GROUP" \
  --app "$APP_INSIGHTS_NAME" \
  --query id -o tsv)"

echo "==> Ensuring action group: $ACTION_GROUP_NAME"
az monitor action-group create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACTION_GROUP_NAME" \
  --short-name "$ACTION_GROUP_SHORT_NAME" \
  --action email oncall "$ALERT_EMAIL" \
  --output none

ACTION_GROUP_ID="$(az monitor action-group show \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACTION_GROUP_NAME" \
  --query id -o tsv)"

# ---------------------------------------------------------------------------
# create_log_alert <name> <severity> <window> <frequency> <description> <kql>
#
# Severity: 0=critical, 1=error, 2=warning, 3=informational.
# Each rule fires when the query returns any row, so the KQL itself encodes the
# threshold. Log alerts (not metric alerts) are used where the condition needs a
# ratio or a dimension filter that a single metric can't express.
#
# Window and frequency are separate because they answer different questions:
# the window is how much history the condition looks at, the frequency is how
# often it is checked. Azure also rejects stateful rules evaluated less often
# than every 12h, so a daily budget window must still be checked hourly.
# ---------------------------------------------------------------------------
create_log_alert() {
  local name="$1" severity="$2" window="$3" frequency="$4" description="$5" query="$6"
  echo "==> Alert rule: $name (sev$severity, ${window} window / ${frequency} check)"
  az monitor scheduled-query create \
    --resource-group "$RESOURCE_GROUP" \
    --name "$name" \
    --scopes "$APP_INSIGHTS_ID" \
    --description "$description" \
    --severity "$severity" \
    --window-size "$window" \
    --evaluation-frequency "$frequency" \
    --condition "count 'placeholder' > 0" \
    --condition-query placeholder="$query" \
    --action-groups "$ACTION_GROUP_ID" \
    --output none
}

# 1. Silent degradation — answers served by the deterministic stub after a
#    provider failure. The highest-severity signal on the platform: users get a
#    plausible compliance answer that no model wrote.
create_log_alert "agentic-llm-degraded" 0 "PT5M" "PT5M" \
  "Compliance answers are being served by the fallback stub instead of the model." \
  'customMetrics
| where name == "llm.calls"
| extend outcome = tostring(customDimensions["outcome"]),
         reason  = tostring(customDimensions["reason"])
| summarize fallbacks = sumif(valueSum, outcome == "fallback" and reason == "mock-fallback"),
            total     = sum(valueSum)
| where total > 0 and todouble(fallbacks) / total > 0.05'

# 2. Provider errors, split by cause. 429 means the deployment is out of quota
#    (raise TPM); 5xx/timeouts mean a provider incident. Different fixes, so
#    they are separate rules rather than one "LLM is unhealthy" alert.
create_log_alert "agentic-llm-throttled-429" 1 "PT15M" "PT15M" \
  "Azure OpenAI is throttling requests (429) - the deployment needs more TPM quota." \
  'customMetrics
| where name == "llm.calls"
| extend status = tostring(customDimensions["status_code"])
| where status == "429"
| summarize throttled = sum(valueSum)
| where throttled > 10'

create_log_alert "agentic-llm-error-rate" 1 "PT15M" "PT15M" \
  "More than 10% of LLM calls are failing (provider outage or misconfiguration)." \
  'customMetrics
| where name == "llm.calls"
| extend outcome = tostring(customDimensions["outcome"])
| summarize errors = sumif(valueSum, outcome == "error"), total = sum(valueSum)
| where total > 20 and todouble(errors) / total > 0.10'

# 3. Latency. p95 rather than mean, because the mean stays healthy while a
#    fraction of requests walk into the adapter's 45s timeout.
create_log_alert "agentic-llm-latency-p95" 2 "PT15M" "PT15M" \
  "LLM p95 latency above 15s - requests are approaching the 45s adapter timeout." \
  'dependencies
| where name == "llm.chat"
| summarize p95 = percentile(duration, 95) by tostring(customDimensions["model"])
| where p95 > 15000'

# 4. Cost. Catches a prompt-size regression or a runaway retry loop before the
#    invoice does.
create_log_alert "agentic-llm-daily-cost" 2 "P1D" "PT1H" \
  "Estimated LLM spend exceeded the daily budget." \
  "customMetrics
| where name == \"llm.cost_usd\"
| summarize cost_usd = sum(valueSum)
| where cost_usd > ${DAILY_COST_USD}"

# 5. Retrieval quality. Every request still returns HTTP 200 when retrieval
#    breaks, so grounding has to be watched directly: an "answered" outcome with
#    no citations means the model was asked to answer with nothing to cite.
create_log_alert "agentic-ungrounded-answers" 1 "PT30M" "PT30M" \
  "Copilot answered without citations - retrieval or the corpus index is broken." \
  'customMetrics
| where name == "copilot.citations"
| extend outcome = tostring(customDimensions["outcome"])
| where outcome == "answered"
| summarize ungrounded = sumif(valueCount, valueMin == 0), total = sum(valueCount)
| where total > 5 and todouble(ungrounded) / total > 0.10'

# 6. Refusal spike. A jump in out-of-scope refusals usually means retrieval
#    stopped matching, not that users changed what they ask.
create_log_alert "agentic-refusal-spike" 2 "PT30M" "PT30M" \
  "Over half of copilot queries are being refused as out-of-scope." \
  'customMetrics
| where name == "copilot.answers"
| extend outcome = tostring(customDimensions["outcome"])
| summarize refused = sumif(valueSum, outcome == "out_of_scope"), total = sum(valueSum)
| where total > 10 and todouble(refused) / total > 0.50'

# 7. Prompt-injection attempts. Audit-relevant: a sustained rise is either an
#    attack or a guardrail false-positive regression, and both need a human.
create_log_alert "agentic-injection-attempts" 2 "PT15M" "PT15M" \
  "Elevated prompt-injection attempts against the copilot." \
  'customMetrics
| where name == "guardrail.events"
| extend flag = tostring(customDimensions["flag"])
| where flag == "prompt_injection"
| summarize attempts = sum(valueSum)
| where attempts > 5'

# 8. Dependency health. /ready returns 503 when the graph store is unreachable;
#    a failing readiness probe across replicas means the platform is down.
create_log_alert "agentic-graph-store-down" 0 "PT5M" "PT5M" \
  "The Neo4j graph store is unreachable - /ready is failing." \
  'requests
| where url endswith "/ready" and resultCode == "503"
| summarize failures = count()
| where failures > 3'

# 9. Groundedness / hallucination. Every live answer is scored inline for
#    faithfulness (fraction of the answer supported by its retrieved context);
#    hallucination_rate is 1 - faithfulness. A falling mean means answers are
#    drifting from their sources even while retrieval still returns citations —
#    a subtler failure than the ungrounded-answers rule above.
create_log_alert "agentic-hallucination-rate" 1 "PT30M" "PT30M" \
  "Mean copilot faithfulness dropped below 0.5 (hallucination rate over 50%)." \
  'customMetrics
| where name == "copilot.faithfulness"
| summarize mean_faithfulness = sum(valueSum) / sum(valueCount), answers = sum(valueCount)
| where answers > 5 and mean_faithfulness < 0.5'

echo
echo "Done. Alert rules provisioned in $RESOURCE_GROUP, notifying $ALERT_EMAIL."
echo "Review them with: az monitor scheduled-query list -g $RESOURCE_GROUP -o table"
