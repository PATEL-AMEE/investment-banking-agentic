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
        """Invoke a registered tool by name (traced as ``tool.<name>``).

        Raises ``KeyError`` for unknown tools — a misconfigured agent should
        fail loudly, not silently skip a compliance control.
        """
        from app.services.telemetry import span

        with span(f"tool.{name}"):
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
    """GraphRAG retrieval over the policy/regulation/evidence corpus.

    Vector similarity search enriched with knowledge-graph relationships;
    see :mod:`app.services.retrieval`.
    """
    from app.services.retrieval import get_retriever

    return get_retriever(store).retrieve(query, k=limit)


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
