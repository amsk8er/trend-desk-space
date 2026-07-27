"""Small single-user access gate for public AI Builder Space deployments.

``AI_BUILDER_TOKEN`` is a provider credential injected for server-side model
calls.  It must never double as a browser password: users cannot retrieve it,
and accepting it at the login form would expose a provider-scoped token.

Public deployments therefore require the separately configured
``TREND_DESK_ACCESS_KEY``.  A missing key in a cloud container fails closed;
local development remains open unless an explicit key is configured.
"""

import base64
import hashlib
import hmac
import json
import os
import time

COOKIE_NAME = "trend_desk_session"
SESSION_DAYS = 30


def access_secret() -> str:
    return (os.getenv("TREND_DESK_ACCESS_KEY") or "").strip()


def auth_required() -> bool:
    # AI Builder injects this only in hosted containers.  If the dedicated
    # login key is omitted there, keep all business APIs closed rather than
    # accidentally publishing private trading data.
    return bool(access_secret() or (os.getenv("AI_BUILDER_TOKEN") or "").strip())


def access_key_matches(value: str) -> bool:
    secret = access_secret()
    return bool(secret) and hmac.compare_digest(value.strip(), secret)


def create_session() -> str:
    payload = json.dumps(
        {"exp": int(time.time()) + SESSION_DAYS * 86400},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(
        access_secret().encode(), encoded.encode(), hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def verify_session(value: str | None) -> bool:
    if not auth_required():
        return True
    if not value or "." not in value:
        return False
    encoded, signature = value.rsplit(".", 1)
    expected = hmac.new(
        access_secret().encode(), encoded.encode(), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return int(payload["exp"]) > int(time.time())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
