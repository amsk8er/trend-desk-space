"""Small single-user access gate for public AI Builder Space deployments.

AI Builder injects ``AI_BUILDER_TOKEN`` into the container.  We reuse that
server-side secret to verify a one-time login and issue a signed HttpOnly
cookie.  The token is never embedded in the frontend or persisted by it.
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
    return (
        os.getenv("TREND_DESK_ACCESS_KEY")
        or os.getenv("AI_BUILDER_TOKEN")
        or ""
    ).strip()


def auth_required() -> bool:
    return bool(access_secret())


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
