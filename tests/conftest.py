"""Test configuration: keep the suite offline and deterministic."""
from __future__ import annotations

import os

# Force the mock LLM provider so tests never make network calls (a real
# key may be configured in .env for the running app).
os.environ["LLM_MODE"] = "mock"

# Pin the deterministic engines: the running app may enable spaCy NER and
# Presidio DLP via .env, but the suite's assertions target the always-available
# rule-based/regex paths so results don't depend on ML model downloads.
os.environ["NLP_ENGINE"] = "rule-based"
os.environ["DLP_ENGINE"] = "regex"
