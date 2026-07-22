from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

# High-risk jurisdictions per the compliance design docs (POLICY_KYC_001
# escalation criteria). Screening a client or owner from one of these
# jurisdictions raises the AML risk score even without a direct list match.
HIGH_RISK_JURISDICTIONS = {"IR", "SY", "KP"}

# Minimal embedded watchlist standing in for OFAC / EU / HMT consolidated
# lists. Names are lower-cased for case-insensitive matching. In production this
# would be replaced by the ``worldcheck`` external API described in the tool
# registry.
_WATCHLIST = {
    "oleg volkov",
    "sanctioned holdings ltd",
    "red star trading",
}


def _name_matches(name: str) -> bool:
    return name.strip().lower() in _WATCHLIST


def sanctions_check(
    client_name: str,
    client_country: str,
    beneficial_owners: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Screen a client and its beneficial owners against sanctions lists.

    Deterministic (no LLM), matching the ``sanctions_check`` tool contract:
    returns whether any entity is sanctioned plus the individual matches.
    """
    beneficial_owners = beneficial_owners or []
    matches: List[Dict[str, Any]] = []

    if _name_matches(client_name):
        matches.append({"entity": client_name, "type": "client", "list": "OFAC"})

    for owner in beneficial_owners:
        owner_name = str(owner.get("full_name") or owner.get("name") or "")
        if owner_name and _name_matches(owner_name):
            matches.append({"entity": owner_name, "type": "beneficial_owner", "list": "OFAC"})

    return {
        "is_sanctioned": bool(matches),
        "matches": matches,
        "high_risk_jurisdiction": (client_country or "").upper() in HIGH_RISK_JURISDICTIONS,
        "check_timestamp": datetime.now(timezone.utc).isoformat(),
    }
