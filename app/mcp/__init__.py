"""Model Context Protocol (MCP) layer — inter-agent communication.

The platform's agents expose their capabilities as MCP tools over JSON-RPC
2.0 (``app.mcp.server``), and call each other through an in-process MCP
client (``app.mcp.client``) so every cross-agent invocation is a structured,
schema-validated, audited ``tools/call`` — never an ad-hoc Python import.
"""
