"""Specialised LangGraph agents for the Investment Banking platform.

Five agents, each a compiled LangGraph ``StateGraph``:

- :mod:`app.agents.compliance` — client/transaction compliance inspection
- :mod:`app.agents.onboarding` — KYC/AML onboarding with sanctions + PEP screening
- :mod:`app.agents.document_analysis` — document classification, enrichment, ingestion
- :mod:`app.agents.client_profiling` — 360° client profile from the knowledge graph
- :mod:`app.agents.copilot` — policy Q&A with retrieved citations

Shared structured tool-calling lives in :mod:`app.agents.base`.
"""
