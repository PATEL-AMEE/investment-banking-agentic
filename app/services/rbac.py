"""Central role-based access control policy.

One place defines the platform's role model and what each role may do; the
API layer, the supervisor agent, and the MCP server all enforce from here so
a permission change lands everywhere at once.

Model (role -> permitted agents/endpoints, enforced before execution):

- ``compliance_analyst`` — queries the compliance and knowledge-retrieval
  agents; cannot trigger client-profiling or onboarding mutations.
- ``onboarding_officer`` — runs onboarding/KYC and client profiling; cannot
  run compliance inspections or the evaluation harness.
- ``risk_manager`` — full analyst surface plus human-review resolution and
  the evaluation harness.
- ``executive`` — view-only reporting: dashboards, audit trail, reviews.
- ``service_agent`` — machine identity for external MCP hosts.

Every check made against a real identity is written to the tamper-evident
audit trail as an ``rbac_check`` event — allowed or denied — so there is a
record of who accessed what, when, and whether it was permitted.

Agent-to-agent calls are scoped separately (``AGENT_TOOL_SCOPES``): each
internal service identity may only invoke the MCP tools its workflow needs,
so one agent cannot silently reach another agent's privileged function.
"""
from __future__ import annotations

from typing import Dict, FrozenSet, Iterable, List, Set

# ------------------------------------------------------------------ permissions
# Permission strings name an agent capability or endpoint surface.
PERM_AGENTS_ASK = "agents.ask"
PERM_COMPLIANCE_INSPECT = "compliance.inspect"
PERM_ONBOARDING_KYC = "onboarding.kyc"
PERM_CLIENT_PROFILE = "client.profile"
PERM_SANCTIONS_CHECK = "sanctions.check"
PERM_COPILOT_QUERY = "copilot.query"
PERM_NLP_ANALYZE = "nlp.analyze"
PERM_DOCUMENTS_UPLOAD = "documents.upload"
PERM_MCP_CALL = "mcp.call"
PERM_EVAL_RUN = "eval.run"
PERM_REVIEWS_READ = "reviews.read"
PERM_REVIEWS_RESOLVE = "reviews.resolve"
PERM_AUDIT_READ = "audit.read"
PERM_DASHBOARD_READ = "dashboard.read"
# Staff may flag a Copilot answer as wrong/good — feeds the Phase 9 eval loop.
PERM_FEEDBACK_SUBMIT = "feedback.submit"
# Client-facing surface: submit an application and read one's OWN status.
# Resource scoping (own client_id only) is enforced at the endpoint on top
# of these role permissions.
PERM_APPLICATION_SUBMIT = "application.submit"
PERM_APPLICATION_STATUS = "application.status.read"

_ANALYST_SURFACE: FrozenSet[str] = frozenset(
    {
        PERM_AGENTS_ASK,
        PERM_COMPLIANCE_INSPECT,
        PERM_SANCTIONS_CHECK,
        PERM_COPILOT_QUERY,
        PERM_NLP_ANALYZE,
        PERM_DOCUMENTS_UPLOAD,
        PERM_REVIEWS_READ,
        PERM_AUDIT_READ,
        PERM_DASHBOARD_READ,
        PERM_MCP_CALL,
        PERM_FEEDBACK_SUBMIT,
    }
)

ROLE_PERMISSIONS: Dict[str, FrozenSet[str]] = {
    "compliance_analyst": _ANALYST_SURFACE,
    "onboarding_officer": frozenset(
        {
            PERM_AGENTS_ASK,
            PERM_ONBOARDING_KYC,
            PERM_CLIENT_PROFILE,
            PERM_SANCTIONS_CHECK,
            PERM_COPILOT_QUERY,
            PERM_DOCUMENTS_UPLOAD,
            PERM_DASHBOARD_READ,
            PERM_APPLICATION_STATUS,
            PERM_FEEDBACK_SUBMIT,
        }
    ),
    "risk_manager": _ANALYST_SURFACE
    | frozenset(
        {PERM_CLIENT_PROFILE, PERM_ONBOARDING_KYC, PERM_REVIEWS_RESOLVE, PERM_EVAL_RUN, PERM_APPLICATION_STATUS}
    ),
    "executive": frozenset({PERM_DASHBOARD_READ, PERM_AUDIT_READ, PERM_REVIEWS_READ, PERM_FEEDBACK_SUBMIT}),
    "service_agent": frozenset({PERM_MCP_CALL, PERM_AGENTS_ASK, PERM_COPILOT_QUERY, PERM_SANCTIONS_CHECK}),
    # External client (the bank's customer): the trigger for the onboarding
    # chain, but completely outside the internal staff tools. They can submit
    # and check their own status — never risk scores, screening results,
    # regulation citations, or internal reasoning.
    "client": frozenset({PERM_APPLICATION_SUBMIT, PERM_APPLICATION_STATUS}),
}

# Legacy / Azure AD app-role names mapped onto the canonical roles so existing
# tokens (and the current test contract) keep working unchanged.
ROLE_ALIASES: Dict[str, str] = {
    "analyst": "compliance_analyst",
    "Copilot.Analyst": "compliance_analyst",
    "reviewer": "risk_manager",
    "Compliance.Reviewer": "risk_manager",
}


def canonical_roles(roles: Iterable[str]) -> Set[str]:
    """Resolve aliases; unknown role names pass through (and grant nothing)."""
    return {ROLE_ALIASES.get(role, role) for role in roles}


def permissions_for(roles: Iterable[str]) -> Set[str]:
    granted: Set[str] = set()
    for role in canonical_roles(roles):
        granted |= ROLE_PERMISSIONS.get(role, frozenset())
    return granted


def has_permission(roles: Iterable[str], permission: str) -> bool:
    return permission in permissions_for(roles)


def check_permission(
    user_id: str,
    roles: List[str],
    permission: str,
    *,
    resource: str | None = None,
    audit: bool = True,
) -> bool:
    """Policy decision point: check and (always) audit the outcome.

    Returns True/False; the caller decides how to reject (HTTP 403, denied
    intent, MCP error). Every check lands on the signed audit trail so
    allowed and denied access are equally evidenced.
    """
    allowed = has_permission(roles, permission)
    if audit:
        from app.services.audit import audit_log

        audit_log.record(
            event_type="rbac_check",
            actor_id=user_id or "user-unknown",
            action=f"permission:{permission}",
            result="allowed" if allowed else "denied",
            resource_id=resource,
            metadata={"roles": ",".join(roles) or "none"},
        )
    return allowed


# ------------------------------------------------------- agent-to-agent scopes
# Internal service identities and the MCP tools their workflow legitimately
# needs. A scoped agent invoking anything else is denied and audited.
AGENT_TOOL_SCOPES: Dict[str, FrozenSet[str]] = {
    "AGENT_SUPERVISOR_001": frozenset(
        {"compliance_inspect", "client_profile", "sanctions_check", "nlp_analyze", "copilot_query", "policy_search"}
    ),
    "AGENT_COPILOT_001": frozenset({"policy_search"}),
    "AGENT_COMPLIANCE_001": frozenset({"policy_search", "sanctions_check"}),
    "AGENT_ONBOARDING_001": frozenset({"policy_search", "sanctions_check"}),
    "AGENT_PROFILING_001": frozenset({"policy_search"}),
}


def agent_may_call(actor_id: str, tool: str) -> bool:
    """Scope check for agent-to-agent MCP ``tools/call``.

    Known internal identities are restricted to their declared scope.
    Unknown/external actors (e.g. an MCP host that reached ``POST /api/mcp``)
    already cleared transport-layer RBAC, so they keep the full tool surface.
    """
    scope = AGENT_TOOL_SCOPES.get(actor_id)
    return True if scope is None else tool in scope
