"""Login, tokens and scope. The appsec half of the security story.

Deliberately small: an HMAC-signed token rather than a JWT library, and an
in-process rate limiter rather than Redis. Both are honest for a single
instance, and neither adds a dependency that has to be explained.

The part that matters is not the token format - it is that `scope` travels
from the database, through the ContextVar, into every retrieval filter and
every scoped tool, so that "actor A cannot read actor B's rows" is enforced in
one place instead of remembered at each call site.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections import defaultdict, deque

from fastapi import Header, HTTPException

from agentcore.contracts import Actor

TOKEN_TTL_SECONDS = 8 * 3600
RATE_LIMIT_PER_MINUTE = 30

_requests: dict[str, deque[float]] = defaultdict(deque)

# In-memory user table. Replaced by db/01_schema.sql's app_user when Postgres
# is reachable; this keeps the API usable (and testable) without it.
DEMO_USERS: dict[str, dict[str, str]] = {
    "alice@example.com": {"id": "u-alice", "password": "demo1234", "role": "engineer", "scope": "payments"},
    "bob@example.com": {"id": "u-bob", "password": "demo1234", "role": "engineer", "scope": "platform"},
    "admin@example.com": {"id": "u-admin", "password": "demo1234", "role": "admin", "scope": "public"},
}


def _secret() -> bytes:
    secret = os.getenv("APP_SECRET", "")
    if not secret:
        raise RuntimeError("APP_SECRET is not set. Run scripts/preflight.sh.")
    return secret.encode()


def issue_token(actor: Actor) -> str:
    """payload.signature, where payload is id|role|scope|expiry."""
    payload = f"{actor.id}|{actor.role}|{actor.scope}|{int(time.time()) + TOKEN_TTL_SECONDS}"
    signature = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    return f"{urlsafe_b64encode(payload.encode()).decode()}.{urlsafe_b64encode(signature).decode()}"


def verify_token(token: str) -> Actor:
    try:
        encoded_payload, encoded_signature = token.split(".", 1)
        payload = urlsafe_b64decode(encoded_payload).decode()
        signature = urlsafe_b64decode(encoded_signature)
    except Exception as error:  # noqa: BLE001
        raise HTTPException(401, "Malformed token") from error

    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).digest()
    # compare_digest, not ==: a timing-safe comparison costs nothing here.
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "Invalid token")

    actor_id, role, scope, expiry = payload.split("|")
    if int(expiry) < time.time():
        raise HTTPException(401, "Token expired")
    return Actor(id=actor_id, role=role, scope=scope)


def authenticate(email: str, password: str) -> Actor:
    record = DEMO_USERS.get(email.strip().lower())
    # Same message either way: distinguishing them tells an attacker which
    # addresses are real.
    if not record or not hmac.compare_digest(record["password"], password):
        raise HTTPException(401, "Invalid credentials")
    return Actor(id=record["id"], email=email, role=record["role"], scope=record["scope"])


def rate_limit(actor_id: str) -> None:
    now = time.time()
    seen = _requests[actor_id]
    while seen and now - seen[0] > 60:
        seen.popleft()
    if len(seen) >= RATE_LIMIT_PER_MINUTE:
        raise HTTPException(429, f"Rate limit: {RATE_LIMIT_PER_MINUTE} requests per minute")
    seen.append(now)


async def current_actor(authorization: str = Header(default="")) -> Actor:
    """FastAPI dependency. Every route that touches data depends on this."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token. POST /login first.")
    actor = verify_token(authorization[7:].strip())
    rate_limit(actor.id)
    return actor
