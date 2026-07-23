"""In-process MCP client for agent-to-agent communication.

Agents call each other's capabilities through ``tools/call`` rather than
importing each other's internals — the same JSON-RPC envelope an external
MCP host would send, so the contract (schemas, audit, tracing) is identical
whether the caller is another agent in this process or a remote MCP client.
"""
from __future__ import annotations

import itertools
from typing import Any, Dict, Optional

from app.mcp.server import MCPServer, get_mcp_server

_request_ids = itertools.count(1)


class MCPClient:
    """Thin client speaking JSON-RPC 2.0 to the platform MCP server.

    With an explicit ``store`` the client binds a server to that store
    (deterministic for tests and per-request stores); otherwise it talks to
    the process-wide server shared with the HTTP ``/api/mcp`` endpoint.
    """

    def __init__(self, agent_id: str, store: Any = None) -> None:
        self.agent_id = agent_id
        self._server = MCPServer(store) if store is not None else get_mcp_server()

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """Invoke an MCP tool; returns the structured result payload.

        Raises ``RuntimeError`` on protocol errors and ``ValueError`` when the
        tool itself reports an error (``isError``) — a failed compliance tool
        must fail loudly, not silently return partial context.
        """
        response = self._server.handle(
            {
                "jsonrpc": "2.0",
                "id": next(_request_ids),
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments or {}},
            },
            actor_id=self.agent_id,
        )
        if response is None or "error" in response:
            error = (response or {}).get("error", {})
            raise RuntimeError(f"MCP error calling {name}: {error.get('message', 'no response')}")
        result = response["result"]
        structured = result.get("structuredContent", {})
        if result.get("isError"):
            raise ValueError(structured.get("error", f"tool {name} failed"))
        # List results are wrapped as {"items": [...]} on the wire.
        if isinstance(structured, dict) and set(structured) == {"items"}:
            return structured["items"]
        return structured

    def list_tools(self) -> list[Dict[str, Any]]:
        response = self._server.handle(
            {"jsonrpc": "2.0", "id": next(_request_ids), "method": "tools/list"},
            actor_id=self.agent_id,
        )
        return response["result"]["tools"]  # type: ignore[index]
