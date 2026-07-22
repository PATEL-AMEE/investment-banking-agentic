"""Compliance inspection entry point.

The workflow now runs as a LangGraph ``StateGraph`` agent
(:mod:`app.agents.compliance`); this module keeps the original function
signature as a stable shim for the API layer and tests.
"""
from __future__ import annotations

from typing import Any, Dict

from app.agents.compliance import run_compliance_agent
from app.services.graph_store import GraphStore


def run_inspection_workflow(
    client_id: str,
    tx_data: Dict[str, Any],
    request_id: str,
    user_id: str,
    session_id: str,
    workflow_step: str,
    store: GraphStore,
    persist_review: bool = True,
) -> Dict[str, Any]:
    return run_compliance_agent(
        client_id=client_id,
        tx_data=tx_data,
        request_id=request_id,
        user_id=user_id,
        session_id=session_id,
        workflow_step=workflow_step,
        store=store,
        persist_review=persist_review,
    )
