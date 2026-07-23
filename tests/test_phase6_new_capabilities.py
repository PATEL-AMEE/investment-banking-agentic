"""Phase 6 tests: MCP protocol, NLP pipeline, signed audit, Vertex adapter,
RAGAS-style evaluation harness, and RBAC on LLM endpoints."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.api import app, store
from app.eval.harness import (
    answer_relevancy,
    context_precision,
    context_recall,
    faithfulness,
    run_evaluation,
)
from app.mcp.client import MCPClient
from app.mcp.server import MCPServer
from app.services.audit import AuditLog
from app.services.llm_adapter import LLMAdapter
from app.services.nlp_pipeline import analyze, classify_document, extract_clauses, extract_entities

client = TestClient(app)


# ------------------------------------------------------------------------ MCP
def test_mcp_initialize_and_tools_list():
    server = MCPServer(store)
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert init["result"]["protocolVersion"]
    listed = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = [tool["name"] for tool in listed["result"]["tools"]]
    assert {"compliance_inspect", "policy_search", "sanctions_check", "nlp_analyze", "copilot_query"} <= set(names)


def test_mcp_tools_call_policy_search_returns_citations():
    result = MCPClient("TEST_AGENT", store=store).call_tool("policy_search", {"query": "enhanced due diligence"})
    assert isinstance(result, list) and result
    assert any("source_id" in citation for citation in result)


def test_mcp_unknown_tool_and_missing_args_are_json_rpc_errors():
    server = MCPServer(store)
    unknown = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "nope"}})
    assert unknown["error"]["code"] == -32602
    missing = server.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "policy_search", "arguments": {}}})
    assert missing["error"]["code"] == -32602
    bad_method = server.handle({"jsonrpc": "2.0", "id": 5, "method": "does/not/exist"})
    assert bad_method["error"]["code"] == -32601


def test_mcp_http_endpoint_round_trip():
    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "sanctions_check", "arguments": {"client_name": "Acme Corp"}}},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == 1 and "result" in body
    assert body["result"]["isError"] is False


def test_mcp_tools_call_lands_on_audit_trail():
    from app.services.audit import audit_log

    before = len([e for e in audit_log.list() if e["event_type"] == "mcp_tool_call"])
    MCPClient("TEST_AGENT", store=store).call_tool("sanctions_check", {"client_name": "Globex Ltd"})
    after = len([e for e in audit_log.list() if e["event_type"] == "mcp_tool_call"])
    assert after == before + 1


# ------------------------------------------------------------------------ NLP
_CONTRACT = (
    "This Agreement between Meridian Holdings Ltd and Northwind Bank is governed by the laws of England. "
    "Each party shall not disclose confidential information. "
    "Either party may terminate this Agreement with a 30 day notice period. "
    "Payment of £2,500,000 is payable within 30 days of the settlement date. "
    "The parties shall comply with all applicable sanctions and embargo regimes per POL-SAN-01."
)


def test_ner_extracts_org_money_and_reg_refs():
    labels = {entity["label"] for entity in extract_entities(_CONTRACT)}
    assert {"ORG", "MONEY", "REG_REF"} <= labels


def test_clause_extraction_finds_contract_clauses():
    clause_types = {clause["clause_type"] for clause in extract_clauses(_CONTRACT)}
    assert {"governing_law", "confidentiality", "termination", "sanctions_compliance"} <= clause_types


def test_document_classification_identifies_aml_policy():
    result = classify_document("This anti-money laundering policy defines suspicious activity reporting and customer due diligence obligations.")
    assert result["category"] == "aml_policy"
    assert result["confidence"] > 0


def test_classification_falls_back_to_general():
    result = classify_document("Lunch menu for the staff canteen.")
    assert result["category"] == "general_correspondence"


def test_nlp_endpoint_returns_full_analysis():
    response = client.post("/api/nlp/analyze", json={"text": _CONTRACT})
    assert response.status_code == 200
    body = response.json()
    assert body["engine"] == "rule-based"
    assert body["entities"] and body["clauses"] and body["classification"]["category"]


def test_document_upload_carries_nlp_enrichment(tmp_path: Path):
    doc = tmp_path / "aml_policy_note.txt"
    doc.write_text("AML policy: enhanced due diligence and suspicious activity reporting for high-risk clients.", encoding="utf-8")
    with doc.open("rb") as handle:
        response = client.post("/api/documents/upload", files={"file": (doc.name, handle, "text/plain")})
    assert response.status_code == 200
    nlp = response.json()["nlp"]
    assert nlp["classification"]["category"] == "aml_policy"


# ------------------------------------------------------------- signed audit
def test_audit_entries_are_signed_and_verified(tmp_path: Path):
    log = AuditLog(path=tmp_path / "audit.jsonl")
    event = log.record("test_event", "ACTOR", "signed_action")
    assert len(event["signature"]) == 64
    verdict = log.verify_chain()
    assert verdict["valid"] is True and verdict["signed"] == 1


def test_forged_signature_is_detected(tmp_path: Path):
    log = AuditLog(path=tmp_path / "audit.jsonl")
    log.record("test_event", "ACTOR", "signed_action")
    log._events[0]["signature"] = "0" * 64  # forge in memory
    assert log.verify_chain()["valid"] is False


def test_legacy_unsigned_entries_still_verify(tmp_path: Path):
    log = AuditLog(path=tmp_path / "audit.jsonl")
    log.record("test_event", "ACTOR", "act")
    del log._events[0]["signature"]  # pre-signing era event
    # Hash chain still holds (signature is excluded from the entry hash).
    verdict = log.verify_chain()
    assert verdict["valid"] is True and verdict["signed"] == 0


# ---------------------------------------------------------------- Vertex AI
def test_vertex_provider_resolution(monkeypatch):
    monkeypatch.delenv("LLM_MODE", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_ENDPOINT", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("VERTEX_PROJECT", "bank-compliance-llm")
    monkeypatch.setenv("VERTEX_ACCESS_TOKEN", "test-token")
    adapter = LLMAdapter()
    assert adapter.provider == "vertex"
    url, headers, payload = adapter._vertex_request(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], 0.2, 100
    )
    assert "publishers/google/models" in url
    assert headers["Authorization"] == "Bearer test-token"
    assert payload["systemInstruction"]["parts"][0]["text"] == "sys"


def test_vertex_tuned_adapter_endpoint(monkeypatch):
    monkeypatch.delenv("LLM_MODE", raising=False)
    monkeypatch.setenv("VERTEX_PROJECT", "bank-compliance-llm")
    monkeypatch.setenv("VERTEX_ACCESS_TOKEN", "test-token")
    monkeypatch.setenv("VERTEX_TUNED_ENDPOINT", "1234567890")
    adapter = LLMAdapter()
    url, _, _ = adapter._vertex_request([{"role": "user", "content": "hi"}], 0.2, 100)
    assert url.endswith("/endpoints/1234567890:generateContent")
    assert adapter._model_name("vertex") == "tuned-endpoint:1234567890"


# ------------------------------------------------------------------- eval
def test_metric_faithfulness_and_hallucination():
    contexts = ["Enhanced due diligence is required for high-risk clients."]
    assert faithfulness("Enhanced due diligence is required for high-risk clients.", contexts) == 1.0
    assert faithfulness("The moon is made of cheese entirely.", contexts) == 0.0


def test_metric_context_precision_recall():
    assert context_precision(["POL-AML-01", "POL-XYZ"], ["POL-AML-01"]) == 0.5
    assert context_recall(["POL-AML-01"], ["POL-AML-01", "POL-AML-02"]) == 0.5


def test_metric_answer_relevancy_bounds():
    score = answer_relevancy("What is required for high-risk clients?", "high-risk clients require enhanced review")
    assert 0.0 < score <= 1.0


def test_run_evaluation_produces_aggregate_report():
    dataset = [
        {"case_id": "T-1", "question": "What is required for high-risk clients under enhanced due diligence?", "expected_sources": ["POL-AML-01"]},
    ]
    report = run_evaluation(dataset, store)
    assert report["case_count"] == 1
    assert set(report["aggregate"]) == {"faithfulness", "hallucination_rate", "answer_relevancy", "context_precision", "context_recall"}


def test_eval_endpoint_runs_golden_dataset():
    response = client.post("/api/eval/run")
    assert response.status_code == 200
    body = response.json()
    assert body["case_count"] >= 6
    assert "faithfulness" in body["aggregate"]


# ---------------------------------------------------------- RBAC on LLM APIs
def test_llm_endpoint_rejects_token_without_analyst_role():
    token = client.post("/api/auth/token", json={"username": "intern", "roles": []}).json()["access_token"]
    response = client.post("/api/copilot/query", json={"query": "aml policy"}, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403


def test_llm_endpoint_accepts_analyst_token():
    token = client.post("/api/auth/token", json={"username": "ami", "roles": ["analyst"]}).json()["access_token"]
    response = client.post("/api/copilot/query", json={"query": "aml policy"}, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200


def test_mcp_endpoint_enforces_rbac():
    token = client.post("/api/auth/token", json={"username": "intern", "roles": []}).json()["access_token"]
    response = client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
