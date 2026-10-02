"""SQLite/libSQL connection and schema setup.

Local use relies on Python's standard-library SQLite. The optional ``turso``
extra supplies libSQL only when a remote TURSO_DATABASE_URL is configured.
"""

import os
import sqlite3
from typing import Any

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS retrievals (
    retrieval_id     TEXT PRIMARY KEY,
    source           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    query_text       TEXT NOT NULL,
    retrieved_at     TEXT NOT NULL,
    agent_id         TEXT NOT NULL,
    source_metadata  TEXT NOT NULL,
    raw_response     TEXT NOT NULL,
    snapshot         TEXT NOT NULL,
    response_hash    TEXT NOT NULL,
    prev_hash        TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_retrievals_external_id ON retrievals(external_id);
CREATE INDEX IF NOT EXISTS idx_retrievals_agent_id ON retrievals(agent_id);
CREATE INDEX IF NOT EXISTS idx_retrievals_retrieved_at ON retrievals(retrieved_at);
CREATE INDEX IF NOT EXISTS idx_retrievals_source ON retrievals(source);

CREATE TABLE IF NOT EXISTS api_keys (
    key_hash    TEXT PRIMARY KEY,  -- SHA-256 of the raw key; the raw key is never stored
    label       TEXT NOT NULL,     -- human-readable owner, e.g. "alice"
    agent_id    TEXT NOT NULL,     -- identity this key's traffic is attributed to
    created_at  TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS projects (
    project_id   TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_collections (
    collection_id TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS collection_items (
    collection_id TEXT NOT NULL,
    retrieval_id  TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    relevance     TEXT NOT NULL DEFAULT 'unreviewed',
    added_at      TEXT NOT NULL,
    PRIMARY KEY (collection_id, retrieval_id),
    FOREIGN KEY (collection_id) REFERENCES evidence_collections(collection_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id)
);

CREATE TABLE IF NOT EXISTS evidence_watches (
    watch_id          TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    source            TEXT NOT NULL,
    query_text        TEXT NOT NULL,
    agent_id          TEXT NOT NULL,
    max_results       INTEGER NOT NULL,
    active            INTEGER NOT NULL DEFAULT 1,
    previous_ids      TEXT NOT NULL DEFAULT '[]',
    created_at        TEXT NOT NULL,
    last_checked_at   TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS evidence_events (
    event_id       TEXT PRIMARY KEY,
    watch_id       TEXT NOT NULL,
    event_type     TEXT NOT NULL,
    external_id    TEXT NOT NULL,
    retrieval_id   TEXT,
    payload        TEXT NOT NULL,
    detected_at    TEXT NOT NULL,
    FOREIGN KEY (watch_id) REFERENCES evidence_watches(watch_id)
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id      TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    claim_text    TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id       TEXT NOT NULL,
    retrieval_id   TEXT NOT NULL,
    stance         TEXT NOT NULL,
    assessment     TEXT NOT NULL DEFAULT 'unreviewed',
    note           TEXT NOT NULL DEFAULT '',
    added_at       TEXT NOT NULL,
    PRIMARY KEY (claim_id, retrieval_id),
    FOREIGN KEY (claim_id) REFERENCES claims(claim_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id)
);

CREATE INDEX IF NOT EXISTS idx_collections_project ON evidence_collections(project_id);
CREATE INDEX IF NOT EXISTS idx_collection_items_retrieval ON collection_items(retrieval_id);
CREATE INDEX IF NOT EXISTS idx_watches_project ON evidence_watches(project_id);
CREATE INDEX IF NOT EXISTS idx_events_watch ON evidence_events(watch_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_claims_project ON claims(project_id);
CREATE INDEX IF NOT EXISTS idx_claim_evidence_retrieval ON claim_evidence(retrieval_id);
"""

# Tracks the target (Turso URL or local path) most recently connected to, so
# retrieval_log's writer-matching logic doesn't have to introspect the
# connection object (libSQL's remote-connection PRAGMA behavior isn't
# guaranteed to mirror sqlite3's).
_current_target: str | None = None


def resolve_target(db_path: str) -> tuple[str, str | None]:
    """Return (target, auth_token). target is the Turso URL if configured, else db_path."""
    url = os.environ.get("TURSO_DATABASE_URL")
    if url:
        return url, os.environ.get("TURSO_AUTH_TOKEN")
    return db_path, None


def current_target() -> str | None:
    """The target most recently passed to connect()."""
    return _current_target


def open_connection(target: str, token: str | None = None) -> Any:
    """Open a local SQLite connection or an optional remote libSQL connection."""
    if token or target.startswith(("libsql://", "https://")):
        try:
            import libsql
        except ImportError as exc:
            raise RuntimeError(
                "Remote Turso storage requires: pip install 'biolab-mcp[turso]'"
            ) from exc
        return libsql.connect(target, auth_token=token) if token else libsql.connect(target)
    return sqlite3.connect(target, check_same_thread=False)


def connect(db_path: str) -> Any:
    global _current_target
    target, token = resolve_target(db_path)

    conn = open_connection(target, token)
    conn.executescript(SCHEMA_V2)
    conn.commit()
    _current_target = target
    return conn
