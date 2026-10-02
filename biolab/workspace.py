"""Scientist projects, evidence collections, and saved evidence watches."""

import json
import uuid
from datetime import UTC, datetime
from typing import Any

from biolab import evidence

RELEVANCE_VALUES = {"unreviewed", "relevant", "excluded", "conflicting", "low_quality"}
STANCE_VALUES = {"supports", "conflicts", "context", "insufficient"}
ASSESSMENT_VALUES = {"unreviewed", "human_reviewed", "agent_proposed"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def create_project(conn: Any, name: str, description: str = "") -> dict:
    if not name.strip():
        raise ValueError("project name must not be empty")
    project = {
        "project_id": str(uuid.uuid4()),
        "name": name.strip(),
        "description": description.strip(),
        "created_at": _now(),
    }
    conn.execute(
        "INSERT INTO projects (project_id, name, description, created_at) VALUES (?, ?, ?, ?)",
        tuple(project.values()),
    )
    conn.commit()
    return project


def list_projects(conn: Any) -> list[dict]:
    rows = conn.execute(
        "SELECT project_id, name, description, created_at FROM projects ORDER BY created_at DESC"
    ).fetchall()
    return [
        {"project_id": row[0], "name": row[1], "description": row[2], "created_at": row[3]}
        for row in rows
    ]


def create_collection(conn: Any, project_id: str, name: str, description: str = "") -> dict:
    if not name.strip():
        raise ValueError("collection name must not be empty")
    if conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
        raise ValueError("project not found")
    collection = {
        "collection_id": str(uuid.uuid4()),
        "project_id": project_id,
        "name": name.strip(),
        "description": description.strip(),
        "created_at": _now(),
    }
    conn.execute(
        """INSERT INTO evidence_collections
           (collection_id, project_id, name, description, created_at) VALUES (?, ?, ?, ?, ?)""",
        tuple(collection.values()),
    )
    conn.commit()
    return collection


def list_collections(conn: Any, project_id: str | None = None) -> list[dict]:
    where = "WHERE project_id = ?" if project_id else ""
    params = (project_id,) if project_id else ()
    rows = conn.execute(
        f"""SELECT collection_id, project_id, name, description, created_at
            FROM evidence_collections {where} ORDER BY created_at""",
        params,
    ).fetchall()
    return [
        {
            "collection_id": row[0], "project_id": row[1], "name": row[2],
            "description": row[3], "created_at": row[4],
        }
        for row in rows
    ]


def add_collection_item(
    conn: Any,
    collection_id: str,
    retrieval_id: str,
    note: str = "",
    relevance: str = "unreviewed",
) -> dict:
    if relevance not in RELEVANCE_VALUES:
        raise ValueError(f"invalid relevance; choose from: {', '.join(sorted(RELEVANCE_VALUES))}")
    if conn.execute(
        "SELECT 1 FROM evidence_collections WHERE collection_id = ?", (collection_id,)
    ).fetchone() is None:
        raise ValueError("collection not found")
    if conn.execute(
        "SELECT 1 FROM retrievals WHERE retrieval_id = ?", (retrieval_id,)
    ).fetchone() is None:
        raise ValueError("retrieval not found")
    added_at = _now()
    conn.execute(
        """INSERT INTO collection_items
           (collection_id, retrieval_id, note, relevance, added_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(collection_id, retrieval_id) DO UPDATE SET
             note = excluded.note, relevance = excluded.relevance""",
        (collection_id, retrieval_id, note, relevance, added_at),
    )
    conn.commit()
    return {
        "collection_id": collection_id,
        "retrieval_id": retrieval_id,
        "note": note,
        "relevance": relevance,
        "added_at": added_at,
    }


def get_collection(conn: Any, collection_id: str) -> dict | None:
    row = conn.execute(
        """SELECT collection_id, project_id, name, description, created_at
           FROM evidence_collections WHERE collection_id = ?""",
        (collection_id,),
    ).fetchone()
    if row is None:
        return None
    items = conn.execute(
        """SELECT i.retrieval_id, i.note, i.relevance, i.added_at,
                  r.source, r.external_id, r.snapshot
           FROM collection_items i JOIN retrievals r ON r.retrieval_id = i.retrieval_id
           WHERE i.collection_id = ? ORDER BY i.added_at""",
        (collection_id,),
    ).fetchall()
    return {
        "collection_id": row[0],
        "project_id": row[1],
        "name": row[2],
        "description": row[3],
        "created_at": row[4],
        "items": [
            {
                "retrieval_id": item[0],
                "note": item[1],
                "relevance": item[2],
                "added_at": item[3],
                "source": item[4],
                "external_id": item[5],
                "snapshot": json.loads(item[6]),
            }
            for item in items
        ],
    }


def create_watch(
    conn: Any,
    project_id: str,
    source: str,
    query_text: str,
    agent_id: str,
    max_results: int = 10,
) -> dict:
    if conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
        raise ValueError("project not found")
    if source not in evidence.SUPPORTED_SOURCES:
        raise ValueError("unsupported watch source")
    if not query_text.strip() or not agent_id.strip() or not 1 <= max_results <= evidence.MAX_RESULTS_CAP:
        raise ValueError("invalid watch query, agent_id, or max_results")
    watch = {
        "watch_id": str(uuid.uuid4()),
        "project_id": project_id,
        "source": source,
        "query_text": query_text,
        "agent_id": agent_id,
        "max_results": max_results,
        "active": True,
        "created_at": _now(),
        "last_checked_at": None,
    }
    conn.execute(
        """INSERT INTO evidence_watches
           (watch_id, project_id, source, query_text, agent_id, max_results, active,
            previous_ids, created_at, last_checked_at)
           VALUES (?, ?, ?, ?, ?, ?, 1, '[]', ?, NULL)""",
        (
            watch["watch_id"],
            project_id,
            source,
            query_text,
            agent_id,
            max_results,
            watch["created_at"],
        ),
    )
    conn.commit()
    return watch


def list_watches(conn: Any, project_id: str | None = None) -> list[dict]:
    where = "WHERE project_id = ?" if project_id else ""
    params = (project_id,) if project_id else ()
    rows = conn.execute(
        f"""SELECT watch_id, project_id, source, query_text, agent_id, max_results,
                   active, created_at, last_checked_at
            FROM evidence_watches {where} ORDER BY created_at""",
        params,
    ).fetchall()
    return [
        {
            "watch_id": row[0], "project_id": row[1], "source": row[2], "query": row[3],
            "agent_id": row[4], "max_results": row[5], "active": bool(row[6]),
            "created_at": row[7], "last_checked_at": row[8],
        }
        for row in rows
    ]


def run_watch(conn: Any, watch_id: str) -> dict:
    row = conn.execute(
        """SELECT source, query_text, agent_id, max_results, active, previous_ids
           FROM evidence_watches WHERE watch_id = ?""",
        (watch_id,),
    ).fetchone()
    if row is None:
        raise ValueError("watch not found")
    if not row[4]:
        raise ValueError("watch is inactive")

    records = evidence.search_source(conn, row[0], row[1], row[2], row[3])
    stored_state = json.loads(row[5])
    # Older databases stored only a list of identifiers. Treat those records as
    # hash-unknown so the first upgraded run does not report false changes.
    previous_hashes = (
        stored_state if isinstance(stored_state, dict) else dict.fromkeys(stored_state)
    )
    current_hashes = {
        record["external_id"]: record.get("content_hash", record["response_hash"])
        for record in records
    }
    previous_ids = set(previous_hashes)
    current_ids = set(current_hashes)
    added = current_ids - previous_ids
    removed = previous_ids - current_ids
    changed = {
        external_id for external_id in current_ids & previous_ids
        if previous_hashes[external_id] is not None
        and previous_hashes[external_id] != current_hashes[external_id]
    }
    detected_at = _now()
    events = []
    retrieval_by_external = {record["external_id"]: record["retrieval_id"] for record in records}
    for event_type, identifiers in (("added", added), ("removed", removed), ("changed", changed)):
        for external_id in sorted(identifiers):
            event = {
                "event_id": str(uuid.uuid4()),
                "watch_id": watch_id,
                "event_type": event_type,
                "external_id": external_id,
                "retrieval_id": retrieval_by_external.get(external_id),
                "detected_at": detected_at,
            }
            conn.execute(
                """INSERT INTO evidence_events
                   (event_id, watch_id, event_type, external_id, retrieval_id, payload, detected_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    event["event_id"], watch_id, event_type, external_id,
                    event["retrieval_id"], json.dumps(event), detected_at,
                ),
            )
            events.append(event)
    conn.execute(
        "UPDATE evidence_watches SET previous_ids = ?, last_checked_at = ? WHERE watch_id = ?",
        (json.dumps(current_hashes, sort_keys=True), detected_at, watch_id),
    )
    conn.commit()
    return {"watch_id": watch_id, "checked_at": detected_at, "records": records, "events": events}


def run_active_watches(conn: Any) -> dict:
    results = []
    errors = []
    for watch in list_watches(conn):
        if not watch["active"]:
            continue
        try:
            results.append(run_watch(conn, watch["watch_id"]))
        except Exception as exc:
            errors.append({"watch_id": watch["watch_id"], "error": str(exc)})
    return {"completed": results, "errors": errors}


def list_watch_events(conn: Any, watch_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT event_id, event_type, external_id, retrieval_id, detected_at
           FROM evidence_events WHERE watch_id = ? ORDER BY detected_at DESC""",
        (watch_id,),
    ).fetchall()
    return [
        {
            "event_id": row[0], "event_type": row[1], "external_id": row[2],
            "retrieval_id": row[3], "detected_at": row[4],
        }
        for row in rows
    ]


def create_claim(conn: Any, project_id: str, claim_text: str) -> dict:
    if conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
        raise ValueError("project not found")
    if not claim_text.strip():
        raise ValueError("claim must not be empty")
    claim = {
        "claim_id": str(uuid.uuid4()),
        "project_id": project_id,
        "claim_text": claim_text.strip(),
        "created_at": _now(),
    }
    conn.execute(
        "INSERT INTO claims (claim_id, project_id, claim_text, created_at) VALUES (?, ?, ?, ?)",
        tuple(claim.values()),
    )
    conn.commit()
    return claim


def link_claim_evidence(
    conn: Any,
    claim_id: str,
    retrieval_id: str,
    stance: str,
    assessment: str = "unreviewed",
    note: str = "",
) -> dict:
    if stance not in STANCE_VALUES:
        raise ValueError(f"invalid stance; choose from: {', '.join(sorted(STANCE_VALUES))}")
    if assessment not in ASSESSMENT_VALUES:
        raise ValueError(
            f"invalid assessment; choose from: {', '.join(sorted(ASSESSMENT_VALUES))}"
        )
    if conn.execute("SELECT 1 FROM claims WHERE claim_id = ?", (claim_id,)).fetchone() is None:
        raise ValueError("claim not found")
    if conn.execute(
        "SELECT 1 FROM retrievals WHERE retrieval_id = ?", (retrieval_id,)
    ).fetchone() is None:
        raise ValueError("retrieval not found")
    added_at = _now()
    conn.execute(
        """INSERT INTO claim_evidence
           (claim_id, retrieval_id, stance, assessment, note, added_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(claim_id, retrieval_id) DO UPDATE SET
             stance = excluded.stance, assessment = excluded.assessment, note = excluded.note""",
        (claim_id, retrieval_id, stance, assessment, note, added_at),
    )
    conn.commit()
    return {
        "claim_id": claim_id,
        "retrieval_id": retrieval_id,
        "stance": stance,
        "assessment": assessment,
        "note": note,
        "added_at": added_at,
    }


def trace_claim(conn: Any, claim_id: str) -> dict | None:
    row = conn.execute(
        "SELECT claim_id, project_id, claim_text, created_at FROM claims WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    if row is None:
        return None
    evidence_rows = conn.execute(
        """SELECT ce.retrieval_id, ce.stance, ce.assessment, ce.note, ce.added_at,
                  r.source, r.external_id, r.retrieved_at, r.snapshot, r.response_hash
           FROM claim_evidence ce JOIN retrievals r ON r.retrieval_id = ce.retrieval_id
           WHERE ce.claim_id = ? ORDER BY ce.added_at""",
        (claim_id,),
    ).fetchall()
    return {
        "claim_id": row[0],
        "project_id": row[1],
        "claim_text": row[2],
        "created_at": row[3],
        "evidence": [
            {
                "retrieval_id": item[0], "stance": item[1], "assessment": item[2],
                "note": item[3], "added_at": item[4], "source": item[5],
                "external_id": item[6], "retrieved_at": item[7],
                "snapshot": json.loads(item[8]), "response_hash": item[9],
            }
            for item in evidence_rows
        ],
    }


def get_project_report(conn: Any, project_id: str) -> dict | None:
    project_row = conn.execute(
        "SELECT project_id, name, description, created_at FROM projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if project_row is None:
        return None
    collection_ids = conn.execute(
        "SELECT collection_id FROM evidence_collections WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    claim_ids = conn.execute(
        "SELECT claim_id FROM claims WHERE project_id = ? ORDER BY created_at", (project_id,)
    ).fetchall()
    watches = conn.execute(
        """SELECT watch_id, source, query_text, active, created_at, last_checked_at
           FROM evidence_watches WHERE project_id = ? ORDER BY created_at""",
        (project_id,),
    ).fetchall()
    return {
        "project": {
            "project_id": project_row[0], "name": project_row[1],
            "description": project_row[2], "created_at": project_row[3],
        },
        "collections": [get_collection(conn, row[0]) for row in collection_ids],
        "claims": [trace_claim(conn, row[0]) for row in claim_ids],
        "watches": [
            {
                "watch_id": row[0], "source": row[1], "query": row[2],
                "active": bool(row[3]), "created_at": row[4], "last_checked_at": row[5],
                "events": list_watch_events(conn, row[0]),
            }
            for row in watches
        ],
    }


def render_project_markdown(report: dict) -> str:
    project = report["project"]
    lines = [
        f"# {project['name']}",
        "",
        project["description"] or "No project description.",
        "",
        f"Project ID: `{project['project_id']}`  ",
        f"Created: {project['created_at']}",
        "",
        "## Evidence collections",
        "",
    ]
    for collection in report["collections"]:
        lines.extend([f"### {collection['name']}", ""])
        if not collection["items"]:
            lines.extend(["No evidence items.", ""])
        for item in collection["items"]:
            title = item["snapshot"].get("title") or item["external_id"]
            lines.extend(
                [
                    f"- **{title}** ({item['source']}:{item['external_id']})",
                    f"  - Retrieval: `{item['retrieval_id']}`",
                    f"  - Review: {item['relevance']}",
                    f"  - Note: {item['note'] or 'None'}",
                ]
            )
        lines.append("")
    lines.extend(["## Claims", ""])
    for claim in report["claims"]:
        lines.extend([f"### {claim['claim_text']}", ""])
        if not claim["evidence"]:
            lines.extend(["No linked evidence.", ""])
        for item in claim["evidence"]:
            title = item["snapshot"].get("title") or item["external_id"]
            lines.extend(
                [
                    f"- **{item['stance']}** — {title}",
                    f"  - Assessment: {item['assessment']}",
                    f"  - Retrieval: `{item['retrieval_id']}`",
                    f"  - Source hash: `{item['response_hash']}`",
                ]
            )
        lines.append("")
    lines.extend(["## Evidence watches", ""])
    for watch in report["watches"]:
        lines.extend(
            [
                f"- `{watch['watch_id']}` — {watch['source']}: {watch['query']}",
                f"  - Last checked: {watch['last_checked_at'] or 'Never'}",
                f"  - Recorded events: {len(watch['events'])}",
            ]
        )
    lines.extend(
        [
            "",
            "---",
            "This report separates retrieved source records, agent-proposed relationships, and human review. It is not clinical advice.",
            "",
        ]
    )
    return "\n".join(lines)
