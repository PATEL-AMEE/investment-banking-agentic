from __future__ import annotations

from typing import Any, Dict

from app.services.azure_config import get_azure_settings


class AzureOpenAIAdapter:
    def __init__(self) -> None:
        self.settings = get_azure_settings()

    def generate_reasoning(self, prompt: str) -> Dict[str, Any]:
        if not self.settings["use_azure_services"]:
            return {
                "summary": "Azure services are disabled in this free-tier prototype. Returning a deterministic summary.",
                "mode": "mock",
            }

        return {
            "summary": f"Azure OpenAI path is configured for prompt: {prompt[:80]}",
            "mode": "azure",
            "deployment": self.settings["azure_openai_deployment"],
        }
