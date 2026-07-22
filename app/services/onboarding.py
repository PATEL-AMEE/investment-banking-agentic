"""KYC/AML onboarding entry point.

The workflow now runs as a LangGraph ``StateGraph`` agent
(:mod:`app.agents.onboarding`); this module re-exports the original
function as a stable shim for the API layer and tests.
"""
from __future__ import annotations

from app.agents.onboarding import run_onboarding_workflow

__all__ = ["run_onboarding_workflow"]
