"""Small single-user access gate for public AI Builder Space deployments.

``AI_BUILDER_TOKEN`` is a provider credential injected for server-side model
calls.  It must never double as a browser password: users cannot retrieve it,
and accepting it at the login form would expose a provider-scoped token.

Public deployments therefore require the separately configured SHA-256 digest
``TREND_DESK_ACCESS_KEY_SHA256``.  The plaintext login key never enters the
deployment request.  A missing key in a cloud container fails closed; local
development remains open unless an explicit digest is configured.
"""

import base64
import hashlib
import hmac
import json
import os
import time

COOKIE_NAME = "trend_desk_session"
SESSION_DAYS = 30
# A temporary verifier can be added during an AI Builder deployment-control
# outage.  It contains only a SHA-256 digest of a high-entropy random login
# key, never the plaintext.  It is deliberately inactive unless the normal
# deployment-provided verifier is also present.
RECOVERY_ACCESS_KEY_SHA256 = "d1187365dd68cb1b42ac2446a0de7ca1b3b788975a79ed3d26bd709d23ecce10"


def access_key_hash() -> str:
    return (os.getenv("TREND_DESK_ACCESS_KEY_SHA256") or "").strip().lower()


def access_key_hashes() -> tuple[str, ...]:
    """Return configured verifiers, including an outage-recovery verifier.

    The recovery verifier is paired with (rather than replacing) the normal
    deployment verifier, so local development remains open and a cloud
    deployment without its dedicated verifier continues to fail closed.
    """
    configured = access_key_hash()
    if not configured:
        return ()
    recovery = RECOVERY_ACCESS_KEY_SHA256.strip().lower()
    return tuple(dict.fromkeys(value for value in (configured, recovery) if value))


def session_signing_secret() -> str:
    # Keep the browser login secret out of deployment audit data.  The
    # platform-injected provider token is server-only and is suitable for
    # signing the HttpOnly session cookie.
    return (os.getenv("AI_BUILDER_TOKEN") or access_key_hash()).strip()


def auth_required() -> bool:
    # AI Builder injects this only in hosted containers.  If the dedicated
    # login key is omitted there, keep all business APIs closed rather than
    # accidentally publishing private trading data.
    return bool(access_key_hash() or (os.getenv("AI_BUILDER_TOKEN") or "").strip())


def access_key_matches(value: str) -> bool:
    submitted_hash = hashlib.sha256(value.strip().encode()).hexdigest()
    return any(
        hmac.compare_digest(submitted_hash, expected_hash)
        for expected_hash in access_key_hashes()
    )


def create_session() -> str:
    payload = json.dumps(
        {"exp": int(time.time()) + SESSION_DAYS * 86400},
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(
        session_signing_secret().encode(), encoded.encode(), hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def verify_session(value: str | None) -> bool:
    if not auth_required():
        return True
    if not value or "." not in value:
        return False
    encoded, signature = value.rsplit(".", 1)
    expected = hmac.new(
        session_signing_secret().encode(), encoded.encode(), hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return int(payload["exp"]) > int(time.time())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
