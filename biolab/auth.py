"""API key issuance/verification, and the per-request identity used for rate-limit fairness.

The public server stays open to unauthenticated callers (see pubmed_client.py's
two-tier limiter) — keys exist to give trusted callers their own isolated,
higher-throughput budget, not to gate access outright.
"""

import hashlib
import os
import secrets
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

# None means "no server-level identity concept" (CLI/direct API usage — the
# rate limiter's identity tier doesn't apply at all, see pubmed_client.py).
# The literal string "anonymous" means "an HTTP caller with no Authorization
# header at all" (set by server.py's middleware).
current_identity: ContextVar[str | None] = ContextVar("current_identity", default=None)

KEY_PREFIX = "blk_"


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def create_api_key(conn: Any, label: str, agent_id: str) -> str:
    """Create a new API key for `label`, bound to `agent_id`. Returns the raw key — shown once."""
    raw_key = generate_key()
    conn.execute(
        "INSERT INTO api_keys (key_hash, label, agent_id, created_at, revoked) VALUES (?, ?, ?, ?, 0)",
        (_hash_key(raw_key), label, agent_id, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return raw_key


def verify_api_key(conn: Any, raw_key: str) -> str | None:
    """Return the bound agent_id if `raw_key` is a valid, non-revoked key; None otherwise."""
    row = conn.execute(
        "SELECT agent_id, revoked FROM api_keys WHERE key_hash = ?",
        (_hash_key(raw_key),),
    ).fetchone()
    if row is None or row[1]:
        return None
    return row[0]


def revoke_api_key(conn: Any, label: str) -> int:
    """Revoke all keys for `label`. Returns how many keys were revoked."""
    cursor = conn.execute("UPDATE api_keys SET revoked = 1 WHERE label = ? AND revoked = 0", (label,))
    conn.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0


def list_api_keys(conn: Any) -> list[dict]:
    rows = conn.execute(
        "SELECT label, agent_id, created_at, revoked FROM api_keys ORDER BY created_at DESC"
    ).fetchall()
    return [
        {"label": r[0], "agent_id": r[1], "created_at": r[2], "revoked": bool(r[3])}
        for r in rows
    ]


def bind_agent_id(requested: str, request: Any = None) -> str:
    """The identity a write is attributed to.

    A verified API key always wins over whatever agent_id the caller typed, so an
    authenticated caller cannot write audit rows as someone else. Only anonymous or
    non-HTTP (CLI/direct) callers keep the self-declared value. `request` is the
    per-request Starlette object when available; its scope is preferred over the
    contextvar because MCP sessions can run handlers in a task whose contextvars were
    captured on an earlier request.
    """
    identity = None
    scope = getattr(request, "scope", None)
    if isinstance(scope, dict):
        identity = scope.get("biolab_identity")
    if identity is None:
        identity = current_identity.get()
    if identity is not None and identity != "anonymous":
        return str(identity)
    return requested


LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def check_exposure(host: str, env: Mapping[str, str] | None = None) -> None:
    """Refuse to listen beyond loopback without auth unless anonymous access is explicit.

    Anonymous callers can create projects, claims and watches and have retrievals
    recorded under a self-declared identity. That is acceptable for a deliberately
    public demo, never as an accident of a default.
    """
    env = os.environ if env is None else env
    if host in LOOPBACK_HOSTS:
        return
    if env.get("BIOLAB_REQUIRE_AUTH", "").lower() in {"1", "true", "yes"}:
        return
    if env.get("BIOLAB_ALLOW_ANONYMOUS", "").lower() in {"1", "true", "yes"}:
        return
    raise SystemExit(
        f"Refusing to listen on {host} without authentication. Set BIOLAB_REQUIRE_AUTH=true "
        "(recommended) or BIOLAB_ALLOW_ANONYMOUS=true for a deliberately public instance."
    )
