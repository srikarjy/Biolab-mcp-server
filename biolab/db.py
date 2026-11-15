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
    prev_hash        TEXT NOT NULL DEFAULT '',
    hash_version     INTEGER NOT NULL DEFAULT 2
);

CREATE INDEX IF NOT EXISTS idx_retrievals_external_id ON retrievals(external_id);
CREATE INDEX IF NOT EXISTS idx_retrievals_agent_id ON retrievals(agent_id);
CREATE INDEX IF NOT EXISTS idx_retrievals_retrieved_at ON retrievals(retrieved_at);
CREATE INDEX IF NOT EXISTS idx_retrievals_source ON retrievals(source);

CREATE TABLE IF NOT EXISTS executions (
    execution_id    TEXT PRIMARY KEY,
    tool            TEXT NOT NULL,
    tool_version    TEXT NOT NULL,
    agent_id        TEXT NOT NULL,
    status          TEXT NOT NULL,           -- succeeded | failed
    inputs          TEXT NOT NULL,           -- canonical JSON
    input_hash      TEXT NOT NULL,
    container       TEXT NOT NULL DEFAULT '',  -- image digest or "host"
    device          TEXT NOT NULL DEFAULT '',  -- e.g. cpu, cuda:0
    retrieval_ids   TEXT NOT NULL DEFAULT '[]',  -- evidence this run was fed from
    outputs         TEXT NOT NULL DEFAULT '[]',  -- [{name, sha256, size}]
    error           TEXT NOT NULL DEFAULT '',
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    prev_hash       TEXT NOT NULL,
    record_hash     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_executions_tool ON executions(tool);
CREATE INDEX IF NOT EXISTS idx_executions_agent ON executions(agent_id);

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
    created_at   TEXT NOT NULL,
    owner_id     TEXT NOT NULL DEFAULT 'local',
    organization_id TEXT
);

CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS organization_members (
    organization_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, member_id),
    FOREIGN KEY (organization_id) REFERENCES organizations(organization_id)
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

CREATE TABLE IF NOT EXISTS ranking_runs (
    ranking_run_id  TEXT PRIMARY KEY,
    project_id      TEXT,
    question        TEXT NOT NULL,
    query_facets    TEXT NOT NULL,
    algorithm_version TEXT NOT NULL,
    requested_limit INTEGER NOT NULL,
    as_of           TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    completed_at    TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);

CREATE TABLE IF NOT EXISTS ranking_candidates (
    ranking_candidate_id TEXT PRIMARY KEY,
    ranking_run_id       TEXT NOT NULL,
    retrieval_id         TEXT NOT NULL,
    rank                 INTEGER NOT NULL,
    score_total          REAL NOT NULL,
    score_components     TEXT NOT NULL,
    reason_codes         TEXT NOT NULL,
    matched_terms        TEXT NOT NULL,
    missing_facets       TEXT NOT NULL,
    review_state         TEXT NOT NULL DEFAULT 'unreviewed',
    review_note          TEXT NOT NULL DEFAULT '',
    reviewed_by          TEXT,
    reviewed_at          TEXT,
    UNIQUE (ranking_run_id, retrieval_id),
    FOREIGN KEY (ranking_run_id) REFERENCES ranking_runs(ranking_run_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id)
);

CREATE INDEX IF NOT EXISTS idx_ranking_runs_project ON ranking_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_ranking_candidates_run ON ranking_candidates(ranking_run_id, rank);
CREATE INDEX IF NOT EXISTS idx_ranking_candidates_retrieval ON ranking_candidates(retrieval_id);

CREATE TABLE IF NOT EXISTS inference_runs (
    inference_run_id  TEXT PRIMARY KEY,
    project_id        TEXT,
    ranking_run_id    TEXT,
    question          TEXT NOT NULL,
    parser_version    TEXT NOT NULL,
    model_provider    TEXT NOT NULL,
    model_name        TEXT NOT NULL,
    status            TEXT NOT NULL,
    created_by        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    completed_at      TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id),
    FOREIGN KEY (ranking_run_id) REFERENCES ranking_runs(ranking_run_id)
);

CREATE TABLE IF NOT EXISTS inference_claims (
    claim_id          TEXT PRIMARY KEY,
    inference_run_id  TEXT NOT NULL,
    claim_text        TEXT NOT NULL,
    review_state      TEXT NOT NULL DEFAULT 'unreviewed',
    created_at        TEXT NOT NULL,
    FOREIGN KEY (inference_run_id) REFERENCES inference_runs(inference_run_id)
);

CREATE TABLE IF NOT EXISTS claim_citations (
    citation_id       TEXT PRIMARY KEY,
    claim_id          TEXT NOT NULL,
    retrieval_id      TEXT NOT NULL,
    field_name        TEXT NOT NULL,
    span_start        INTEGER NOT NULL,
    span_end          INTEGER NOT NULL,
    excerpt_hash      TEXT NOT NULL,
    locator           TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT '',
    external_id      TEXT NOT NULL DEFAULT '',
    excerpt_text     TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (claim_id) REFERENCES inference_claims(claim_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id)
);

CREATE TABLE IF NOT EXISTS parser_chunks (
    chunk_id          TEXT PRIMARY KEY,
    inference_run_id  TEXT NOT NULL,
    retrieval_id      TEXT NOT NULL,
    field_name        TEXT NOT NULL,
    chunk_index       INTEGER NOT NULL,
    chunk_text        TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    FOREIGN KEY (inference_run_id) REFERENCES inference_runs(inference_run_id),
    FOREIGN KEY (retrieval_id) REFERENCES retrievals(retrieval_id)
);

CREATE INDEX IF NOT EXISTS idx_inference_runs_project ON inference_runs(project_id);
CREATE INDEX IF NOT EXISTS idx_inference_claims_run ON inference_claims(inference_run_id);
CREATE INDEX IF NOT EXISTS idx_claim_citations_claim ON claim_citations(claim_id);
CREATE INDEX IF NOT EXISTS idx_claim_citations_retrieval ON claim_citations(retrieval_id);
CREATE INDEX IF NOT EXISTS idx_parser_chunks_run ON parser_chunks(inference_run_id);

CREATE TABLE IF NOT EXISTS exports (
    export_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, format TEXT NOT NULL,
    status TEXT NOT NULL, checksum TEXT NOT NULL, size_bytes INTEGER NOT NULL,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE INDEX IF NOT EXISTS idx_exports_project ON exports(project_id, created_at);

CREATE TABLE IF NOT EXISTS parser_runs (
    parser_run_id TEXT PRIMARY KEY, project_id TEXT, filename TEXT NOT NULL, parser_model TEXT NOT NULL,
    source_hash TEXT NOT NULL, status TEXT NOT NULL, block_count INTEGER NOT NULL,
    created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(project_id)
);
CREATE TABLE IF NOT EXISTS parsed_blocks (
    block_id TEXT PRIMARY KEY, parser_run_id TEXT NOT NULL, page_number INTEGER NOT NULL,
    block_index INTEGER NOT NULL, block_type TEXT NOT NULL, text TEXT NOT NULL,
    bbox TEXT, content_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    FOREIGN KEY (parser_run_id) REFERENCES parser_runs(parser_run_id)
);
CREATE INDEX IF NOT EXISTS idx_parsed_blocks_run ON parsed_blocks(parser_run_id, page_number, block_index);
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


def _upgrade_retrievals(conn: Any) -> None:
    """Bring a pre-0.5 retrievals table up to date.

    Rows written before hash_version existed were hashed over raw_response only, so
    they are stamped version 1 and verified under that legacy rule; new rows get 2.
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(retrievals)").fetchall()}
    if "prev_hash" not in columns:
        conn.execute("ALTER TABLE retrievals ADD COLUMN prev_hash TEXT NOT NULL DEFAULT ''")
    if "hash_version" not in columns:
        conn.execute("ALTER TABLE retrievals ADD COLUMN hash_version INTEGER NOT NULL DEFAULT 1")


def _upgrade_projects(conn: Any) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)").fetchall()}
    if "owner_id" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'local'")
    if "organization_id" not in columns:
        conn.execute("ALTER TABLE projects ADD COLUMN organization_id TEXT")


def _upgrade_citations(conn: Any) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(claim_citations)").fetchall()}
    for name in ("source", "external_id", "excerpt_text"):
        if name not in columns:
            conn.execute(f"ALTER TABLE claim_citations ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


def _upgrade_parser_runs(conn: Any) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(parser_runs)").fetchall()}
    if "project_id" not in columns:
        conn.execute("ALTER TABLE parser_runs ADD COLUMN project_id TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_parser_runs_project ON parser_runs(project_id, created_at)")


def connect(db_path: str) -> Any:
    global _current_target
    target, token = resolve_target(db_path)

    conn = open_connection(target, token)
    conn.executescript(SCHEMA_V2)
    _upgrade_retrievals(conn)
    _upgrade_projects(conn)
    _upgrade_citations(conn)
    _upgrade_parser_runs(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_org ON projects(organization_id)")
    conn.commit()
    _current_target = target
    return conn
