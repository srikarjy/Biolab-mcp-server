"""Persisted, bounded jobs for work that should outlive one HTTP request."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from biolab import audit, document_parser, workspace

SUPPORTED_KINDS = {"parser_index_rebuild"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _decode(row: Any) -> dict[str, Any]:
    return {
        "job_id": row[0], "project_id": row[1], "kind": row[2], "status": row[3],
        "payload": json.loads(row[4] or "{}"), "result": json.loads(row[5] or "{}"),
        "error": row[6], "created_by": row[7], "created_at": row[8],
        "started_at": row[9], "finished_at": row[10], "attempts": row[11],
        "max_attempts": row[12], "idempotency_key": row[13],
    }


def _row(conn: Any, job_id: str) -> Any:
    return conn.execute(
        """SELECT job_id, project_id, kind, status, payload, result, error, created_by,
                  created_at, started_at, finished_at, attempts, max_attempts, idempotency_key
           FROM background_jobs WHERE job_id = ?""", (job_id,)
    ).fetchone()


def _assert_access(conn: Any, job: dict[str, Any], owner_id: str) -> None:
    if not workspace.has_project_access(conn, job["project_id"], owner_id):
        raise ValueError("job not found")


def enqueue(
    conn: Any, *, project_id: str, kind: str, payload: dict[str, Any],
    created_by: str, idempotency_key: str | None = None, max_attempts: int = 3,
) -> dict[str, Any]:
    if kind not in SUPPORTED_KINDS:
        raise ValueError("unsupported job kind")
    if not workspace.has_project_access(conn, project_id, created_by):
        raise ValueError("project not found")
    if not 1 <= max_attempts <= 5:
        raise ValueError("max_attempts must be between 1 and 5")
    if idempotency_key:
        existing = conn.execute(
            "SELECT job_id FROM background_jobs WHERE created_by = ? AND idempotency_key = ?",
            (created_by, idempotency_key),
        ).fetchone()
        if existing:
            return get(conn, existing[0], created_by)  # type: ignore[return-value]
    job_id = str(uuid.uuid4())
    created_at = _now()
    conn.execute(
        """INSERT INTO background_jobs
           (job_id, project_id, kind, status, payload, created_by, created_at, max_attempts, idempotency_key)
           VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, ?)""",
        (job_id, project_id, kind, json.dumps(payload, sort_keys=True), created_by, created_at, max_attempts, idempotency_key),
    )
    audit.record(conn, project_id, "job_queued", "background_job", job_id, created_by, {"kind": kind})
    conn.commit()
    return get(conn, job_id, created_by)  # type: ignore[return-value]


def get(conn: Any, job_id: str, owner_id: str) -> dict[str, Any] | None:
    row = _row(conn, job_id)
    if row is None:
        return None
    job = _decode(row)
    _assert_access(conn, job, owner_id)
    return job


def run_once(conn: Any, job_id: str, owner_id: str) -> dict[str, Any]:
    job = get(conn, job_id, owner_id)
    if job is None:
        raise ValueError("job not found")
    if job["status"] == "succeeded":
        return job
    if job["status"] == "running":
        raise ValueError("job is already running")
    if job["status"] == "failed" and job["attempts"] >= job["max_attempts"]:
        return job
    started_at = _now()
    attempts = job["attempts"] + 1
    conn.execute(
        "UPDATE background_jobs SET status = 'running', attempts = ?, started_at = ?, error = '' WHERE job_id = ?",
        (attempts, started_at, job_id),
    )
    audit.record(conn, job["project_id"], "job_started", "background_job", job_id, owner_id, {"kind": job["kind"], "attempt": attempts})
    conn.commit()
    try:
        if job["kind"] == "parser_index_rebuild":
            result = document_parser.rebuild_parser_index(conn, str(job["payload"]["parser_run_id"]), owner_id)
        else:  # guarded by enqueue, retained as a defensive boundary
            raise ValueError("unsupported job kind")  # noqa: TRY301 - defensive dispatch guard
    except (ValueError, TypeError, RuntimeError) as exc:
        error = str(exc)[:500]
        terminal = attempts >= job["max_attempts"]
        status = "failed" if terminal else "queued"
        conn.execute(
            "UPDATE background_jobs SET status = ?, error = ?, finished_at = ? WHERE job_id = ?",
            (status, error, _now() if terminal else None, job_id),
        )
        audit.record(conn, job["project_id"], "job_failed", "background_job", job_id, owner_id, {"kind": job["kind"], "attempt": attempts, "terminal": terminal, "error": error})
        conn.commit()
        return get(conn, job_id, owner_id)  # type: ignore[return-value]
    conn.execute(
        "UPDATE background_jobs SET status = 'succeeded', result = ?, finished_at = ?, error = '' WHERE job_id = ?",
        (json.dumps(result, sort_keys=True), _now(), job_id),
    )
    audit.record(conn, job["project_id"], "job_succeeded", "background_job", job_id, owner_id, {"kind": job["kind"], "indexed_blocks": result.get("indexed_blocks", 0)})
    conn.commit()
    return get(conn, job_id, owner_id)  # type: ignore[return-value]
