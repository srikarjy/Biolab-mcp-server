"""Project-scoped, hash-chained activity history."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(event: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def record(conn: Any, project_id: str, event_type: str, entity_type: str, entity_id: str, actor_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    previous = conn.execute("SELECT event_hash FROM audit_events WHERE project_id = ? ORDER BY rowid DESC LIMIT 1", (project_id,)).fetchone()
    event = {
        "event_id": str(uuid.uuid4()), "project_id": project_id, "event_type": event_type,
        "entity_type": entity_type, "entity_id": entity_id, "actor_id": actor_id,
        "payload": payload or {}, "created_at": _now(), "prev_hash": previous[0] if previous else "",
    }
    event["event_hash"] = _hash(event)
    conn.execute(
        """INSERT INTO audit_events
           (event_id, project_id, event_type, entity_type, entity_id, actor_id, payload, created_at, prev_hash, event_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (event["event_id"], project_id, event_type, entity_type, entity_id, actor_id,
         json.dumps(event["payload"], sort_keys=True), event["created_at"], event["prev_hash"], event["event_hash"]),
    )
    return event


def list_events(conn: Any, project_id: str, limit: int = 200) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT event_id, project_id, event_type, entity_type, entity_id, actor_id, payload, created_at, prev_hash, event_hash
           FROM audit_events WHERE project_id = ? ORDER BY rowid DESC LIMIT ?""", (project_id, limit)
    ).fetchall()
    return [{"event_id": row[0], "project_id": row[1], "event_type": row[2], "entity_type": row[3], "entity_id": row[4], "actor_id": row[5], "payload": json.loads(row[6]), "created_at": row[7], "prev_hash": row[8], "event_hash": row[9]} for row in rows]


def verify_project(conn: Any, project_id: str) -> tuple[bool, str | None]:
    rows = conn.execute(
        """SELECT event_id, project_id, event_type, entity_type, entity_id, actor_id, payload, created_at, prev_hash, event_hash
           FROM audit_events WHERE project_id = ? ORDER BY rowid""", (project_id,)
    ).fetchall()
    previous = ""
    for row in rows:
        if row[8] != previous:
            return False, row[0]
        event = {"event_id": row[0], "project_id": row[1], "event_type": row[2], "entity_type": row[3], "entity_id": row[4], "actor_id": row[5], "payload": json.loads(row[6]), "created_at": row[7], "prev_hash": row[8]}
        if _hash(event) != row[9]:
            return False, row[0]
        previous = row[9]
    return True, None
