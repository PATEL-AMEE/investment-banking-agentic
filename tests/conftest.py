"""Test configuration: keep the suite offline and deterministic."""
from __future__ import annotations

import os

# Force the mock LLM provider so tests never make network calls (a real
# key may be configured in .env for the running app).
os.environ["LLM_MODE"] = "mock"
