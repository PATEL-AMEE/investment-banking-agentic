"""MCP server: platform agents exposed as JSON-RPC 2.0 tools.

Implements the MCP wire shapes (``initialize``, ``tools/list``,
``tools/call``) without external dependencies, so any MCP-aware client —
another agent, an IDE, an orchestration host — can invoke the platform's
compliance, profiling, retrieval, and NLP capabilities with schema-validated
arguments. Served over HTTP at ``POST /api/mcp`` and consumed in-process by
:mod:`app.mcp.client` for agent-to-agent calls.

Every ``tools/call`` is written to the tamper-evident audit trail, so
inter-agent traffic is as auditable as human-triggered API calls.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "investment-banking-agentic-platform", "version": "1.0.0"}

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class MCPServer:
    """Registry of MCP tools plus a JSON-RPC 2.0 request handler."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._handlers: Dict[str, Callable[..., Any]] = {}
        self._register_platform_tools()

    # ---------------------------------------------------------------- registry
    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: Dict[str, Any],
        handler: Callable[..., Any],
    ) -> None:
        self._tools[name] = {"name": name, "description": description, "inputSchema": input_schema}
        self._handlers[name] = handler

    def tool_names(self) -> List[str]:
        return sorted(self._tools)

    def _register_platform_tools(self) -> None:
        self.register_tool(
            "compliance_inspect",
            "Run the compliance agent: rule evaluation + GraphRAG retrieval over a client transaction, returning a decision record with provenance.",
            {
                "type": "object",
                "properties": {
                    "client_id": {"type": "string"},
                    "tx_data": {"type": "object"},
                    "request_id": {"type": "string"},
                },
                "required": ["client_id"],
            },
            self._tool_compliance_inspect,
        )
        self.register_tool(
            "client_profile",
            "Run the client-profiling agent: 360-degree profile (risk, documents, evidence, reviews) for a client id.",
            {
                "type": "object",
                "properties": {"client_id": {"type": "string"}},
                "required": ["client_id"],
            },
            self._tool_client_profile,
        )
        self.register_tool(
            "policy_search",
            "GraphRAG retrieval over the policy/regulation/evidence corpus; returns scored citations with graph context.",
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                "required": ["query"],
            },
            self._tool_policy_search,
        )
        self.register_tool(
            "sanctions_check",
            "Screen a client (and optional beneficial owners) against the consolidated sanctions lists; returns match status and matched entries.",
            {
                "type": "object",
                "properties": {
                    "client_name": {"type": "string"},
                    "client_country": {"type": "string"},
                    "beneficial_owners": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["client_name"],
            },
            self._tool_sanctions_check,
        )
        self.register_tool(
            "nlp_analyze",
            "NLP pipeline over raw text: named-entity recognition, contract clause extraction, and regulatory document classification.",
            {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            self._tool_nlp_analyze,
        )
        self.register_tool(
            "copilot_query",
            "Grounded regulatory Q&A: DLP guardrails, GraphRAG citation retrieval, and citation-grounded answer generation.",
            {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            self._tool_copilot_query,
        )

    # ------------------------------------------------------------ tool bodies
    def _tool_compliance_inspect(self, client_id: str, tx_data: Optional[Dict[str, Any]] = None, request_id: str = "mcp-req") -> Dict[str, Any]:
        from app.services.workflow import run_inspection_workflow

        return run_inspection_workflow(
            client_id=client_id,
            tx_data=tx_data or {},
            request_id=request_id,
            user_id="mcp-client",
            session_id="mcp-session",
            workflow_step="mcp-tools-call",
            store=self.store,
            persist_review=False,
        )

    def _tool_client_profile(self, client_id: str) -> Dict[str, Any]:
        from app.agents.client_profiling import run_client_profiling

        return run_client_profiling(client_id, self.store)

    def _tool_policy_search(self, query: str, limit: int = 3) -> List[Dict[str, Any]]:
        from app.agents.base import default_registry

        return default_registry.call("policy_search", store=self.store, query=query, limit=limit)

    def _tool_sanctions_check(
        self,
        client_name: str,
        client_country: str = "GB",
        beneficial_owners: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        from app.services.sanctions import sanctions_check

        return sanctions_check(client_name, client_country, beneficial_owners)

    def _tool_nlp_analyze(self, text: str) -> Dict[str, Any]:
        from app.services.nlp_pipeline import analyze

        return analyze(text)

    def _tool_copilot_query(self, query: str) -> Dict[str, Any]:
        from app.agents.copilot import run_copilot

        return run_copilot(query, self.store)

    # ---------------------------------------------------------------- JSON-RPC
    def handle(self, request: Dict[str, Any], actor_id: str = "mcp-client") -> Optional[Dict[str, Any]]:
        """Dispatch one JSON-RPC 2.0 request; returns ``None`` for notifications."""
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}

        if not isinstance(method, str) or not method:
            return self._error(request_id, INVALID_PARAMS, "Missing method")
        if method.startswith("notifications/"):
            return None  # notifications get no response

        try:
            if method == "initialize":
                result: Any = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": SERVER_INFO,
                    "capabilities": {"tools": {"listChanged": False}},
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": [self._tools[name] for name in self.tool_names()]}
            elif method == "tools/call":
                return self._handle_tools_call(request_id, params, actor_id)
            else:
                return self._error(request_id, METHOD_NOT_FOUND, f"Unknown method: {method}")
        except Exception as exc:  # defensive: protocol errors, not tool errors
            return self._error(request_id, INTERNAL_ERROR, str(exc)[:300])
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _handle_tools_call(self, request_id: Any, params: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        from app.services.audit import audit_log
        from app.services.telemetry import span

        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        if name not in self._handlers:
            return self._error(request_id, INVALID_PARAMS, f"Unknown tool: {name}")
        missing = [
            field
            for field in self._tools[name]["inputSchema"].get("required", [])
            if field not in arguments
        ]
        if missing:
            return self._error(request_id, INVALID_PARAMS, f"Missing required arguments: {missing}")

        with span(f"mcp.tools/call.{name}", actor=actor_id):
            try:
                output = self._handlers[name](**arguments)
                is_error = False
            except ValueError as exc:
                output = {"error": str(exc)}
                is_error = True

        audit_log.record(
            event_type="mcp_tool_call",
            actor_id=actor_id,
            action=f"tools/call:{name}",
            result="error" if is_error else "success",
            metadata={"arguments": json.dumps(arguments, default=str)[:500]},
        )

        result = {
            "content": [{"type": "text", "text": json.dumps(output, default=str)}],
            "structuredContent": output if isinstance(output, dict) else {"items": output},
            "isError": is_error,
        }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


# Lazily constructed process-wide server bound to the API layer's store.
_server: Optional[MCPServer] = None


def get_mcp_server(store: Any = None) -> MCPServer:
    global _server
    if _server is None:
        if store is None:  # standalone use (tests, scripts) — in-memory store
            from pathlib import Path

            from app.services.graph_store import GraphStore
            from app.services.ingestion import seed_demo_data

            store = GraphStore(data_dir=Path("data"))
            seed_demo_data(store, data_dir=Path("data"))
        _server = MCPServer(store)
    return _server
