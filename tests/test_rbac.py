"""RBAC tests: role model, gateway enforcement, agent scoping, audited checks.

Covers the platform's role-based access control end to end:
- role -> permission mapping (compliance_analyst / onboarding_officer /
  risk_manager / executive) enforced at the API layer,
- the supervisor's per-intent gateway check before any worker runs,
- agent-to-agent MCP tool scoping for internal service identities,
- ``rbac_check`` audit events for allowed AND denied decisions.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import app, store
from app.agents.supervisor import run_supervisor
from app.mcp.client import MCPClient
from app.services import rbac
from app.services.audit import audit_log

client = TestClient(app)


def _token(username: str, roles: list[str]) -> dict:
    response = client.post("/api/auth/token", json={"username": username, "roles": roles})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# ------------------------------------------------------------------ role model
def test_role_permission_mapping():
    assert rbac.has_permission(["compliance_analyst"], rbac.PERM_COMPLIANCE_INSPECT)
    assert not rbac.has_permission(["compliance_analyst"], rbac.PERM_CLIENT_PROFILE)
    assert not rbac.has_permission(["compliance_analyst"], rbac.PERM_ONBOARDING_KYC)

    assert rbac.has_permission(["onboarding_officer"], rbac.PERM_ONBOARDING_KYC)
    assert rbac.has_permission(["onboarding_officer"], rbac.PERM_CLIENT_PROFILE)
    assert not rbac.has_permission(["onboarding_officer"], rbac.PERM_COMPLIANCE_INSPECT)
    assert not rbac.has_permission(["onboarding_officer"], rbac.PERM_EVAL_RUN)

    assert rbac.has_permission(["risk_manager"], rbac.PERM_REVIEWS_RESOLVE)
    assert rbac.has_permission(["risk_manager"], rbac.PERM_EVAL_RUN)

    # Executive is view-only reporting.
    assert rbac.has_permission(["executive"], rbac.PERM_DASHBOARD_READ)
    assert rbac.has_permission(["executive"], rbac.PERM_AUDIT_READ)
    assert not rbac.has_permission(["executive"], rbac.PERM_COPILOT_QUERY)
    assert not rbac.has_permission(["executive"], rbac.PERM_COMPLIANCE_INSPECT)


def test_legacy_role_aliases_still_map():
    assert rbac.has_permission(["analyst"], rbac.PERM_COPILOT_QUERY)
    assert rbac.has_permission(["Copilot.Analyst"], rbac.PERM_COPILOT_QUERY)
    assert rbac.has_permission(["reviewer"], rbac.PERM_REVIEWS_RESOLVE)
    assert rbac.has_permission(["Compliance.Reviewer"], rbac.PERM_REVIEWS_RESOLVE)


def test_unknown_role_grants_nothing():
    assert rbac.permissions_for(["intern"]) == set()


# ------------------------------------------------------- endpoint enforcement
def test_onboarding_officer_can_kyc_but_not_inspect():
    headers = _token("officer", ["onboarding_officer"])
    kyc = client.post(
        "/api/agents/onboarding/kyc",
        json={"clientName": "Acme Corp", "jurisdiction": "GB"},
        headers=headers,
    )
    assert kyc.status_code == 200
    inspect = client.post(
        "/api/agents/inspect",
        json={"clientId": "C123", "txData": {"amount": 100}},
        headers=headers,
    )
    assert inspect.status_code == 403


def test_executive_is_view_only():
    headers = _token("cro", ["executive"])
    assert client.get("/api/dashboard/summary", headers=headers).status_code == 200
    assert client.get("/api/audit/logs", headers=headers).status_code == 200
    assert client.post("/api/copilot/query", json={"query": "aml policy"}, headers=headers).status_code == 403
    assert client.post("/api/eval/run", headers=headers).status_code == 403


def test_compliance_analyst_cannot_run_client_profile_endpoint():
    headers = _token("ana", ["compliance_analyst"])
    assert client.get("/api/agents/profile/C123", headers=headers).status_code == 403
    assert client.post("/api/copilot/query", json={"query": "aml policy"}, headers=headers).status_code == 200


# ------------------------------------------------- supervisor gateway checks
def test_supervisor_denies_out_of_scope_intent_for_onboarding_officer():
    result = run_supervisor(
        "Run a compliance check and give me the client profile.",
        store,
        client_id="C456",
        roles=["onboarding_officer"],
        user_id="officer",
    )
    assert "compliance_inspect" in result.get("denied", [])
    assert "client_profile" in result["routed_to"]


def test_supervisor_rbac_decisions_are_audited_allowed_and_denied():
    before = len([e for e in audit_log.list() if e["event_type"] == "rbac_check"])
    run_supervisor(
        "Run a compliance check and give me the client profile.",
        store,
        client_id="C456",
        roles=["onboarding_officer"],
        user_id="officer-audit",
    )
    checks = [e for e in audit_log.list() if e["event_type"] == "rbac_check"]
    assert len(checks) > before
    mine = [e for e in checks if e["actor_id"] == "officer-audit"]
    results = {e["resource_id"]: e["result"] for e in mine}
    assert results.get("intent:compliance_inspect") == "denied"
    assert results.get("intent:client_profile") == "allowed"


def test_endpoint_denial_is_audited():
    before = len([e for e in audit_log.list() if e["event_type"] == "rbac_check"])
    headers = _token("cro-audit", ["executive"])
    client.post("/api/copilot/query", json={"query": "aml policy"}, headers=headers)
    checks = [e for e in audit_log.list() if e["event_type"] == "rbac_check"]
    assert len(checks) == before + 1
    assert checks[-1]["actor_id"] == "cro-audit"
    assert checks[-1]["result"] == "denied"
    assert checks[-1]["action"] == f"permission:{rbac.PERM_COPILOT_QUERY}"


# ------------------------------------------------------ agent-to-agent scope
def test_internal_agent_scoped_to_declared_tools():
    assert rbac.agent_may_call("AGENT_COPILOT_001", "policy_search")
    assert not rbac.agent_may_call("AGENT_COPILOT_001", "compliance_inspect")
    # Unknown/external identities cleared transport RBAC already.
    assert rbac.agent_may_call("external-host", "compliance_inspect")


def test_mcp_denies_and_audits_out_of_scope_agent_call():
    before = len([e for e in audit_log.list() if e["event_type"] == "rbac_check"])
    with pytest.raises(RuntimeError, match="not permitted"):
        MCPClient("AGENT_COPILOT_001", store=store).call_tool("compliance_inspect", {"client_id": "C123"})
    checks = [e for e in audit_log.list() if e["event_type"] == "rbac_check"]
    assert len(checks) == before + 1
    assert checks[-1]["actor_id"] == "AGENT_COPILOT_001"
    assert checks[-1]["result"] == "denied"
