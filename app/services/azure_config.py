import os
from typing import Optional


def get_azure_settings() -> dict:
    return {
        "azure_openai_endpoint": os.getenv("AZURE_OPENAI_ENDPOINT", ""),
        "azure_openai_deployment": os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini"),
        "azure_openai_api_version": os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
        "azure_search_endpoint": os.getenv("AZURE_SEARCH_ENDPOINT", ""),
        "azure_search_index": os.getenv("AZURE_SEARCH_INDEX", "policy-index"),
        "use_azure_services": os.getenv("USE_AZURE_SERVICES", "false").lower() == "true",
    }


def get_optional_azure_client() -> Optional[str]:
    settings = get_azure_settings()
    if settings["use_azure_services"] and settings["azure_openai_endpoint"]:
        return settings["azure_openai_endpoint"]
    return None


def get_azure_ad_settings() -> dict:
    return {
        "enable_azure_ad": os.getenv("ENABLE_AZURE_AD", "false").lower() == "true",
        "tenant_id": os.getenv("AZURE_AD_TENANT_ID", ""),
        "client_id": os.getenv("AZURE_AD_CLIENT_ID", ""),
        "audience": os.getenv("AZURE_AD_CLIENT_ID", os.getenv("AZURE_AD_AUDIENCE", "")),
    }
