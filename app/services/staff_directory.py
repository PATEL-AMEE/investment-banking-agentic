"""Staff directory — the dev stand-in for corporate Microsoft Entra ID.

Real staff sign in with their Microsoft work account and the token comes back
carrying their identity + role claims *from the directory* — the browser never
chooses its own role. This module is the development equivalent: a known list
of staff emails, each mapped to the role Entra would assign. Sign-in looks the
account up here and issues a token with the directory's role, so a caller can
never grant themselves a role by asking for it.

Demo scope: static map. Production deletes this and trusts Entra ID claims.
Override in dev with ``STAFF_DIRECTORY`` = ``email:role,email:role`` if needed.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, TypedDict


class StaffMember(TypedDict):
    name: str
    email: str
    role: str


# email (lowercased) -> staff member. Roles are the canonical platform roles.
_DIRECTORY: Dict[str, StaffMember] = {
    "aisha.khan@bank-demo.example": {"name": "Aisha Khan", "email": "aisha.khan@bank-demo.example", "role": "compliance_analyst"},
    "tom.reed@bank-demo.example": {"name": "Tom Reed", "email": "tom.reed@bank-demo.example", "role": "onboarding_officer"},
    "priya.shah@bank-demo.example": {"name": "Priya Shah", "email": "priya.shah@bank-demo.example", "role": "risk_manager"},
    "dev.cole@bank-demo.example": {"name": "Dev Cole", "email": "dev.cole@bank-demo.example", "role": "executive"},
}


def _load_overrides() -> None:
    """Optional ``STAFF_DIRECTORY=email:role,...`` for local experimentation."""
    raw = os.getenv("STAFF_DIRECTORY", "")
    for pair in (p for p in raw.split(",") if ":" in p):
        email, role = pair.split(":", 1)
        email = email.strip().lower()
        _DIRECTORY[email] = {"name": email.split("@")[0], "email": email, "role": role.strip()}


_load_overrides()


def lookup(email: str) -> Optional[StaffMember]:
    """Return the staff member for an email, or ``None`` if not in the directory."""
    return _DIRECTORY.get((email or "").strip().lower())
