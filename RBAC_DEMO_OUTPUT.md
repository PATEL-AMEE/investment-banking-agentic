# RBAC — Live Demo Output

Captured live from `http://localhost:8000` on 2026-07-23, after the RBAC
implementation (central policy in `app/services/rbac.py`, enforced at the
API layer, the supervisor gateway, and the MCP tool layer).

**How it works:** role → permitted agents/endpoints → checked at the
supervisor/gateway layer **before** execution → logged either way on the
cryptographic audit trail.

---

## 1. Same endpoints, three different roles

### Onboarding Officer — token `roles=["onboarding_officer"]`

| Request | Result |
|---|---|
| `POST /api/agents/onboarding/kyc` | **200 ALLOWED** |
| `GET /api/agents/profile/C123` | **200 ALLOWED** |
| `POST /api/agents/inspect` | **403 DENIED** — cannot run compliance decisions |

### Compliance Analyst — token `roles=["compliance_analyst"]`

| Request | Result |
|---|---|
| `POST /api/agents/inspect` | **200 ALLOWED** |
| `POST /api/copilot/query` | **200 ALLOWED** |
| `GET /api/agents/profile/C123` | **403 DENIED** — cannot trigger client profiling |

### Executive / CRO — token `roles=["executive"]` (view-only)

| Request | Result |
|---|---|
| `GET /api/dashboard/summary` | **200 ALLOWED** |
| `GET /api/audit/logs` | **200 ALLOWED** |
| `POST /api/copilot/query` | **403 DENIED** — no LLM access at all |
| `POST /api/eval/run` | **403 DENIED** |

---

## 2. Supervisor gateway — per-intent check before routing

Onboarding officer asks: *"Run a compliance check and give me the risk
profile"* (client `C456`):

```json
{
  "routed_to": ["client_profile"],
  "denied":    ["compliance_inspect"],
  "answer":    "Client profile: Globex Ltd — risk high (0.78)."
}
```

The profile half ran; the compliance half was rejected **before it reached
the compliance agent or the LLM**.

---

## 3. Every check on the audit trail — allowed AND denied

```
AUDIT-001028  comp.analyst        permission:client.profile      denied
AUDIT-001029  cro                 permission:dashboard.read      allowed
AUDIT-001031  cro                 permission:copilot.query       denied
AUDIT-001033  onboarding.officer  permission:agents.ask          allowed
AUDIT-001034  onboarding.officer  permission:compliance.inspect  denied   intent:compliance_inspect
AUDIT-001035  onboarding.officer  permission:client.profile      allowed  intent:client_profile
AUDIT-001038  onboarding.officer  permission:audit.read          denied
AUDIT-001039  cro                 permission:audit.read          allowed

Chain valid: True   entries: 1040   HMAC-signed: 929
```

`AUDIT-001038` is the demo script itself being denied: the onboarding
officer's token tried to read the audit log, RBAC blocked it, and the
denial became evidence on the chain — who accessed what, when, and whether
it was permitted.

---

## 4. Agent-to-agent scoping (MCP `tools/call`)

| Caller identity | Tool | Result |
|---|---|---|
| `AGENT_COPILOT_001` | `policy_search` | ✅ allowed (declared scope) |
| `AGENT_COPILOT_001` | `compliance_inspect` | ❌ `"Agent AGENT_COPILOT_001 is not permitted to call tool"` + audited denial |

Internal service identities may only invoke the MCP tools their workflow
declares; unknown/external MCP hosts keep the full surface because they
already cleared transport-layer RBAC at `POST /api/mcp`.

---

## Role model reference

| Role | Can | Cannot |
|---|---|---|
| `compliance_analyst` | compliance inspect, copilot Q&A, sanctions, NLP, audit read | client profiling, KYC, eval |
| `onboarding_officer` | KYC, client profiling, sanctions, copilot Q&A | compliance inspect, eval, audit read |
| `risk_manager` | full analyst surface + resolve reviews + eval | — |
| `executive` | dashboards, audit trail, review queue (view-only) | any agent/LLM call |

Legacy names (`analyst`, `reviewer`, `Copilot.Analyst`,
`Compliance.Reviewer`) alias onto these, so existing tokens keep working.

## Try it yourself

```powershell
# get a token for a role
$t = (Invoke-RestMethod -Uri http://localhost:8000/api/auth/token -Method Post -ContentType application/json `
     -Body '{"username":"me","roles":["onboarding_officer"]}').access_token

# allowed for this role
Invoke-RestMethod -Uri http://localhost:8000/api/agents/onboarding/kyc -Method Post -ContentType application/json `
  -Body '{"clientName":"Acme Corp","jurisdiction":"GB"}' -Headers @{Authorization="Bearer $t"}

# denied for this role (403)
Invoke-RestMethod -Uri http://localhost:8000/api/agents/inspect -Method Post -ContentType application/json `
  -Body '{"clientId":"C123","txData":{"amount":100}}' -Headers @{Authorization="Bearer $t"}
```

> Note: requests **without any token** still bypass RBAC in local dev so the
> demo UIs keep working. Set `REQUIRE_AUTH=true` to force authentication on
> every request.
