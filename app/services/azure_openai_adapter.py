"""Back-compat shim: the adapter is now provider-agnostic.

See :mod:`app.services.llm_adapter` — supports Azure OpenAI, any
OpenAI-compatible endpoint (e.g. GitHub Models), and a mock fallback.
"""
from __future__ import annotations

from app.services.llm_adapter import LLMAdapter as AzureOpenAIAdapter

__all__ = ["AzureOpenAIAdapter"]
