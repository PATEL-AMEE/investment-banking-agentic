from __future__ import annotations

import time
from typing import Any, Dict

import requests
from jose import jwk, jwt
from jose.utils import base64url_decode

from app.services.azure_config import get_azure_ad_settings


_JWKS_CACHE: Dict[str, Any] = {}


def _get_openid_config(tenant: str) -> Dict[str, Any]:
    url = f"https://login.microsoftonline.com/{tenant}/v2.0/.well-known/openid-configuration"
    r = requests.get(url, timeout=5)
    r.raise_for_status()
    return r.json()


def _get_jwks(openid_config: Dict[str, Any]) -> Dict[str, Any]:
    jwks_uri = openid_config.get("jwks_uri")
    r = requests.get(jwks_uri, timeout=5)
    r.raise_for_status()
    return r.json()


def _get_cached_jwks(tenant: str) -> Dict[str, Any]:
    now = time.time()
    cache = _JWKS_CACHE.get(tenant)
    if cache and now - cache.get("fetched_at", 0) < 3600:
        return cache["jwks"]
    openid = _get_openid_config(tenant)
    jwks = _get_jwks(openid)
    _JWKS_CACHE[tenant] = {"jwks": jwks, "fetched_at": now}
    return jwks


def validate_jwt(token: str) -> Dict[str, Any]:
    settings = get_azure_ad_settings()
    if not settings["enable_azure_ad"]:
        return {"sub": "local-user", "name": "local-user"}

    tenant = settings.get("tenant_id")
    audience = settings.get("audience")
    if not tenant or not audience:
        raise ValueError("Azure AD not configured correctly")

    jwks = _get_cached_jwks(tenant)
    headers = jwt.get_unverified_header(token)
    kid = headers.get("kid")
    key = None
    for jwk_dict in jwks.get("keys", []):
        if jwk_dict.get("kid") == kid:
            key = jwk_dict
            break
    if not key:
        raise ValueError("Unable to find matching JWK")

    public_key = jwk.construct(key)
    message, encoded_sig = token.rsplit('.', 1)
    decoded_sig = base64url_decode(encoded_sig.encode('utf-8'))
    if not public_key.verify(message.encode('utf-8'), decoded_sig):
        raise ValueError("Signature verification failed")

    claims = jwt.get_unverified_claims(token)
    if claims.get('aud') != audience and audience not in claims.get('aud', []):
        raise ValueError('Invalid audience')

    # Note: additional checks for issuer and exp can be added
    return claims


def get_current_user_from_header(auth_header: str | None) -> Dict[str, Any]:
    if not auth_header:
        raise ValueError("Missing Authorization header")
    parts = auth_header.split()
    if parts[0].lower() != 'bearer' or len(parts) != 2:
        raise ValueError("Invalid Authorization header format")
    token = parts[1]
    return validate_jwt(token)


def user_has_role(claims: Dict[str, Any], role: str) -> bool:
    """Check common JWT claim places for roles or groups.

    Supports: 'roles' claim (app roles), 'groups' claim, and 'scp' scope strings.
    """
    if not claims:
        return False
    # app roles
    roles = claims.get('roles') or claims.get('role') or []
    if isinstance(roles, str):
        roles = [roles]
    if role in roles:
        return True
    # groups
    groups = claims.get('groups') or []
    if isinstance(groups, str):
        groups = [groups]
    if role in groups:
        return True
    # scopes
    scp = claims.get('scp') or claims.get('scope') or ''
    if isinstance(scp, str) and role in scp.split():
        return True
    return False
