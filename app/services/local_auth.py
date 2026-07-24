"""Local JWT auth for development RBAC.

Issues and validates HS256-signed tokens carrying role claims, so role
enforcement runs the same code path in dev as with Azure AD in production
(``user_has_role`` reads the same ``roles`` claim shape AD emits).

Two identity domains, never one shared login (see the two-apps-two-logins
architecture): the **internal staff app** authenticates staff via corporate
SSO (Azure AD in prod, staff tokens here), and the **client portal**
authenticates external clients via a separate provider (Azure AD B2C in prod,
client tokens here). Each app stamps its own issuer + audience so a token
minted for one app can never be honoured by the other:

- staff tokens:  iss=``_ISSUER_STAFF``  aud=``AUD_STAFF``
- client tokens: iss=``_ISSUER_CLIENT`` aud=``AUD_CLIENT``

``issue_token`` remains the low-level primitive (legacy issuer, no audience)
used by tests and internal tooling; real app flows use ``issue_staff_token``
and ``issue_client_token``.

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

# One issuer per app — the two logins are separate identity domains.
_ISSUER_STAFF = "investment-banking-platform-staff"
_ISSUER_CLIENT = "investment-banking-platform-client"
# Legacy issuer kept so bare ``issue_token`` calls (tests, tooling) still
# validate. New code should prefer the per-app helpers below.
_LEGACY_ISSUER = "investment-banking-platform-local"
_ACCEPTED_ISSUERS = (_ISSUER_STAFF, _ISSUER_CLIENT, _LEGACY_ISSUER)

# One audience per app. Endpoints gate on these so a client-portal token can
# never reach the internal staff surface (defence-in-depth on top of RBAC).
AUD_STAFF = "ib-staff-app"
AUD_CLIENT = "ib-client-portal"


def _secret() -> str:
    return os.getenv("LOCAL_JWT_SECRET", _DEFAULT_DEV_SECRET)


def auth_required() -> bool:
    return os.getenv("REQUIRE_AUTH", "false").lower() == "true"


def issue_token(
    username: str,
    roles: List[str] | None = None,
    expires_minutes: int = 60,
    extra_claims: Dict[str, Any] | None = None,
    *,
    issuer: str = _LEGACY_ISSUER,
    audience: str | None = None,
) -> str:
    """Issue a signed token.

    ``extra_claims`` carries resource-scoping claims such as ``client_id`` for
    client-portal tokens (own-record access only). ``issuer``/``audience``
    stamp the app identity domain; callers normally use ``issue_staff_token``
    or ``issue_client_token`` rather than setting these directly.
    """
    now = int(time.time())
    claims = {
        "sub": username,
        "name": username,
        "roles": roles or [],
        "iss": issuer,
        "iat": now,
        "exp": now + expires_minutes * 60,
    }
    if audience is not None:
        claims["aud"] = audience
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, _secret(), algorithm=_ALGORITHM)


def issue_staff_token(
    username: str,
    roles: List[str] | None = None,
    expires_minutes: int = 60,
    extra_claims: Dict[str, Any] | None = None,
) -> str:
    """Internal staff app token (Azure AD SSO replaces this in prod)."""
    return issue_token(
        username,
        roles,
        expires_minutes,
        extra_claims,
        issuer=_ISSUER_STAFF,
        audience=AUD_STAFF,
    )


def issue_client_token(
    username: str,
    roles: List[str] | None = None,
    expires_minutes: int = 60,
    extra_claims: Dict[str, Any] | None = None,
) -> str:
    """Client portal token (Azure AD B2C replaces this in prod).

    Scoped to the client's own records via a ``client_id`` claim in
    ``extra_claims`` and gated to the client-facing endpoint surface only.
    """
    return issue_token(
        username,
        roles or ["client"],
        expires_minutes,
        extra_claims,
        issuer=_ISSUER_CLIENT,
        audience=AUD_CLIENT,
    )


def validate_local_token(token: str) -> Dict[str, Any]:
    """Decode and verify a local token; raises ``ValueError`` when invalid.

    Accepts any of the known app issuers and returns the full claim set
    (including ``iss``/``aud``) so the API layer can enforce the app
    boundary. Audience is not checked here — a client token and a staff token
    are both structurally valid; which endpoints each may reach is decided by
    the caller from the ``aud`` claim.
    """
    try:
        return jwt.decode(
            token,
            _secret(),
            algorithms=[_ALGORITHM],
            issuer=_ACCEPTED_ISSUERS,
            options={"verify_aud": False},
        )
    except Exception as exc:
        raise ValueError(f"Invalid token: {exc}") from exc
