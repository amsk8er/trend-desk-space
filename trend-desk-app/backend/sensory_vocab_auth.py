"""Fixed-key access gate for the public Sensory Vocabulary Lab."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from datetime import timedelta

from sqlmodel import Session

from backend.engine import engine
from backend.sensory_vocab_store import VocabAccessFailure, utcnow


COOKIE_NAME = "sensory_vocab_session"
SESSION_DAYS = 7
MAX_FAILURES = 5
FAILURE_WINDOW_MINUTES = 15
BLOCK_MINUTES = 30


def access_key_hash() -> str:
    return (os.getenv("SENSORY_VOCAB_ACCESS_KEY_SHA256") or "").strip().lower()


def access_configured() -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", access_key_hash()))


def hosted_runtime() -> bool:
    return bool((os.getenv("AI_BUILDER_TOKEN") or "").strip())


def signing_secret() -> bytes:
    provider_secret = (os.getenv("AI_BUILDER_TOKEN") or "").strip()
    material = f"sensory-vocab-session-v1\n{provider_secret}\n{access_key_hash()}"
    return hashlib.sha256(material.encode()).digest()


def access_key_matches(value: str) -> bool:
    expected = access_key_hash()
    submitted = hashlib.sha256(value.strip().encode()).hexdigest()
    return access_configured() and hmac.compare_digest(submitted, expected)


def create_session() -> str:
    payload = json.dumps(
        {
            "exp": int(time.time()) + SESSION_DAYS * 86400,
            "v": 1,
        },
        separators=(",", ":"),
    ).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    signature = hmac.new(signing_secret(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def verify_session(value: str | None) -> bool:
    if not access_configured() or not value or "." not in value:
        return False
    encoded, signature = value.rsplit(".", 1)
    expected = hmac.new(signing_secret(), encoded.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
        return payload.get("v") == 1 and int(payload["exp"]) > int(time.time())
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False


def visitor_hash(client_host: str | None) -> str:
    material = f"sensory-vocab-visitor-v1\n{client_host or 'unknown'}"
    return hmac.new(signing_secret(), material.encode(), hashlib.sha256).hexdigest()


def blocked_seconds(visitor: str) -> int:
    now = utcnow()
    with Session(engine) as session:
        row = session.get(VocabAccessFailure, visitor)
        if row is None or row.blocked_until is None or row.blocked_until <= now:
            return 0
        return max(1, int((row.blocked_until - now).total_seconds()))


def record_failure(visitor: str) -> int:
    now = utcnow()
    window = timedelta(minutes=FAILURE_WINDOW_MINUTES)
    with Session(engine) as session:
        row = session.get(VocabAccessFailure, visitor)
        if row is None:
            row = VocabAccessFailure(
                visitor_hash=visitor,
                failures=1,
                window_started_at=now,
                updated_at=now,
            )
        elif row.window_started_at + window <= now:
            row.failures = 1
            row.window_started_at = now
            row.blocked_until = None
            row.updated_at = now
        else:
            row.failures += 1
            row.updated_at = now
        if row.failures >= MAX_FAILURES:
            row.blocked_until = now + timedelta(minutes=BLOCK_MINUTES)
        session.add(row)
        session.commit()
        if row.blocked_until and row.blocked_until > now:
            return max(1, int((row.blocked_until - now).total_seconds()))
        return 0


def clear_failures(visitor: str) -> None:
    with Session(engine) as session:
        row = session.get(VocabAccessFailure, visitor)
        if row is not None:
            session.delete(row)
            session.commit()

