"""Client-portal identity store — a SEPARATE identity system for external users.

Bank clients are not Barclays employees and have no corporate directory
account, so they authenticate against their own provider, never the staff
SSO. In production this is **Azure AD B2C** (which owns sign-up, email
verification, password reset, and MFA for external users). This module is the
dev stand-in for exactly that boundary: a self-contained email/password store
for the client portal, with no overlap with staff identity.

Passwords are stored only as salted PBKDF2-HMAC-SHA256 hashes. The register
step happens at intake (the application form doubles as sign-up, as a B2C
sign-up policy would); returning clients sign in at ``/client/login``.

Demo scope: in-memory, keyed by lowercased email. A real deployment delegates
all of this to B2C and keeps no client passwords of its own.
"""
from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

_PBKDF2_ROUNDS = 120_000


@dataclass
class ClientAccount:
    email: str
    password_hash: str  # "<salt_hex>$<derived_hex>"
    client_id: str
    company_name: str
    # Email ownership. Azure AD B2C verifies this out of band in prod; in dev
    # we mark it verified at sign-up so the flow stays usable without a mailer.
    verified: bool = True
    application_ids: list[str] = field(default_factory=list)


# email (lowercased) -> account
_accounts: Dict[str, ClientAccount] = {}


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"{salt.hex()}${derived.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, _ = stored.split("$", 1)
    except ValueError:
        return False
    candidate = _hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, stored)


def register(
    *,
    email: str,
    password: str,
    client_id: str,
    company_name: str,
    application_id: str,
) -> ClientAccount:
    """Create or update the client's portal credentials at intake.

    A repeat application from the same email keeps the existing password and
    just links the new application to the account.
    """
    key = email.strip().lower()
    existing = _accounts.get(key)
    if existing is not None:
        if application_id not in existing.application_ids:
            existing.application_ids.append(application_id)
        existing.client_id = client_id
        existing.company_name = company_name
        return existing
    account = ClientAccount(
        email=key,
        password_hash=_hash_password(password),
        client_id=client_id,
        company_name=company_name,
        application_ids=[application_id],
    )
    _accounts[key] = account
    return account


def authenticate(email: str, password: str) -> Optional[ClientAccount]:
    """Return the account when email + password match and are verified."""
    account = _accounts.get(email.strip().lower())
    if account is None or not account.verified:
        return None
    if not _verify_password(password, account.password_hash):
        return None
    return account


def get_account(email: str) -> Optional[ClientAccount]:
    return _accounts.get(email.strip().lower())
