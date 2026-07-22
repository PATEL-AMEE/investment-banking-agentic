from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from difflib import get_close_matches
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

# High-risk jurisdictions per the compliance design docs (POLICY_KYC_001
# escalation criteria). Screening a client or owner from one of these
# jurisdictions raises the AML risk score even without a direct list match.
HIGH_RISK_JURISDICTIONS = {"IR", "SY", "KP"}

# Embedded local watchlist — always active, and the fallback when the OFAC
# SDN download is not present. Names lower-cased for matching.
_WATCHLIST = {
    "oleg volkov",
    "sanctioned holdings ltd",
    "red star trading",
}

# OFAC Specially Designated Nationals list, downloaded by
# ``scripts/update_sanctions.py``. Columns (no header row):
# ent_num, SDN_Name, SDN_Type, Program, Title, ...
SDN_PATH = Path("data") / "sanctions" / "sdn.csv"

_NORMALISE_RE = re.compile(r"[^a-z0-9\s]")
# Fuzzy-match threshold: high enough to avoid false accusations from a
# screening tool, low enough to catch transliteration/punctuation variants.
_FUZZY_CUTOFF = 0.92


def _normalise(name: str) -> str:
    return " ".join(_NORMALISE_RE.sub(" ", name.strip().lower()).split())


@lru_cache(maxsize=1)
def _sdn_names() -> frozenset[str]:
    """Normalised names from the local OFAC SDN snapshot (empty if absent)."""
    if not SDN_PATH.exists():
        return frozenset()
    names: set[str] = set()
    try:
        with SDN_PATH.open(newline="", encoding="utf-8", errors="ignore") as handle:
            for row in csv.reader(handle):
                if len(row) > 1 and row[1] and row[1] != "-0-":
                    names.add(_normalise(row[1]))
    except Exception:
        return frozenset()
    return frozenset(names)


@lru_cache(maxsize=4096)
def _match_name(name: str) -> str | None:
    """Return the matched list name ('OFAC-SDN' / 'LOCAL') or None.

    Exact match on the normalised name first (including token-sorted form,
    so "Volkov, Oleg" matches "Oleg Volkov"), then conservative fuzzy match
    against the SDN list to catch spelling/transliteration variants.
    """
    normalised = _normalise(name)
    if not normalised:
        return None
    token_sorted = " ".join(sorted(normalised.split()))

    if normalised in _WATCHLIST or token_sorted in {" ".join(sorted(w.split())) for w in _WATCHLIST}:
        return "LOCAL"

    sdn = _sdn_names()
    if not sdn:
        return None
    if normalised in sdn:
        return "OFAC-SDN"
    sdn_token_sorted = _sdn_token_sorted()
    if token_sorted in sdn_token_sorted:
        return "OFAC-SDN"
    if get_close_matches(normalised, sdn, n=1, cutoff=_FUZZY_CUTOFF):
        return "OFAC-SDN"
    return None


@lru_cache(maxsize=1)
def _sdn_token_sorted() -> frozenset[str]:
    return frozenset(" ".join(sorted(name.split())) for name in _sdn_names())


def sanctions_check(
    client_name: str,
    client_country: str,
    beneficial_owners: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Screen a client and its beneficial owners against sanctions lists.

    Deterministic (no LLM), matching the ``sanctions_check`` tool contract:
    returns whether any entity is sanctioned plus the individual matches.
    Screens against the embedded watchlist plus the OFAC SDN snapshot when
    ``data/sanctions/sdn.csv`` is present (see ``scripts/update_sanctions.py``).
    """
    beneficial_owners = beneficial_owners or []
    matches: List[Dict[str, Any]] = []

    client_list = _match_name(client_name)
    if client_list:
        matches.append({"entity": client_name, "type": "client", "list": "OFAC" if client_list == "LOCAL" else client_list})

    for owner in beneficial_owners:
        owner_name = str(owner.get("full_name") or owner.get("name") or "")
        if owner_name:
            owner_list = _match_name(owner_name)
            if owner_list:
                matches.append({"entity": owner_name, "type": "beneficial_owner", "list": "OFAC" if owner_list == "LOCAL" else owner_list})

    return {
        "is_sanctioned": bool(matches),
        "matches": matches,
        "high_risk_jurisdiction": (client_country or "").upper() in HIGH_RISK_JURISDICTIONS,
        "screened_against": "OFAC-SDN+local" if _sdn_names() else "local-watchlist",
        "check_timestamp": datetime.now(timezone.utc).isoformat(),
    }
