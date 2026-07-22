"""Shared agent infrastructure: structured tool registry.

Agents never call services directly — they invoke named tools through the
registry so every invocation is uniform, recordable in agent state
(``tool_calls``), and swappable (e.g. the ``sanctions_check`` tool can be
re-pointed at a World-Check API without touching agent graphs).
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List


class ToolRegistry:
    """Named tools an agent may invoke during a workflow run."""

    def __init__(self) -> None:
        self._tools: Dict[str, Callable[..., Any]] = {}

    def register(self, name: str, fn: Callable[..., Any]) -> None:
        self._tools[name] = fn

    def names(self) -> List[str]:
        return sorted(self._tools)

    def call(self, name: str, **kwargs: Any) -> Any:
        """Invoke a registered tool by name.

        Raises ``KeyError`` for unknown tools — a misconfigured agent should
        fail loudly, not silently skip a compliance control.
        """
        return self._tools[name](**kwargs)


def _graph_retriever(store: Any, jurisdiction: str) -> List[Dict[str, Any]]:
    """Applicable regulations for a jurisdiction; empty list on unsupported backends."""
    getter = getattr(store, "get_regulations_by_jurisdiction", None)
    if getter is None:
        return []
    try:
        return getter(jurisdiction)
    except Exception:
        return []


def _policy_search(store: Any, query: str, limit: int = 3) -> List[Dict[str, Any]]:
    """Keyword retrieval over Policy/Regulation nodes (GraphRAG-lite).

    Replaced by embedding retrieval in Phase 2; the tool contract stays stable.
    """
    fallback = [{"source_id": "POL-AML-01", "excerpt": "Enhanced customer due diligence is required for high-risk profiles."}]
    nodes = getattr(store, "nodes", None)
    if not nodes:
        return fallback
    terms = [term for term in query.lower().split() if len(term) > 3]
    citations: List[Dict[str, Any]] = []
    for node in nodes.values():
        source_id = node.get("policy_id") or node.get("regulation_id")
        if not source_id:
            continue
        text = ((node.get("summary") or "") + " " + (node.get("title") or "")).lower()
        if terms and any(term in text for term in terms):
            citations.append({"source_id": source_id, "excerpt": node.get("summary") or node.get("title") or ""})
        if len(citations) >= limit:
            break
    return citations or fallback


def build_default_registry() -> ToolRegistry:
    """Registry with the platform's standard tools wired to local services."""
    from app.services.sanctions import sanctions_check

    registry = ToolRegistry()
    registry.register("graph_retriever", _graph_retriever)
    registry.register("sanctions_check", sanctions_check)
    registry.register("policy_search", _policy_search)
    return registry


# Process-wide default registry shared by the agent graphs.
default_registry = build_default_registry()
