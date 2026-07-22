"""Local JWT auth for development RBAC.

Issues and validates HS256-signed tokens carrying role claims, so role
enforcement runs the same code path in dev as with Azure AD in production
(``user_has_role`` reads the same ``roles`` claim shape AD emits).

Env:
- ``LOCAL_JWT_SECRET`` — signing secret (defaults to a dev-only value).
- ``REQUIRE_AUTH``     — "true" makes every endpoint demand a valid token;
                         default "false" keeps anonymous dev access working.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from jose import jwt

_ALGORITHM = "HS256"
_DEFAULT_DEV_SECRET = "dev-only-secret-change-me"
_ISSUER = "investment-banking-platform-local"


def _secret() -> str:
    return os.getenv("LOCAL_JWT_SECRET", _DEFAULT_DEV_SECRET)


def auth_required() -> bool:
    return os.getenv("REQUIRE_AUTH", "false").lower() == "true"


def issue_token(username: str, roles: List[str] | None = None, expires_minutes: int = 60) -> str:
    now = int(time.time())
    claims = {
        "sub": username,
        "name": username,
        "roles": roles or [],
        "iss": _ISSUER,
        "iat": now,
        "exp": now + expires_minutes * 60,
    }
    return jwt.encode(claims, _secret(), algorithm=_ALGORITHM)


def validate_local_token(token: str) -> Dict[str, Any]:
    """Decode and verify a local token; raises ``ValueError`` when invalid."""
    try:
        return jwt.decode(token, _secret(), algorithms=[_ALGORITHM], issuer=_ISSUER)
    except Exception as exc:
        raise ValueError(f"Invalid token: {exc}") from exc
