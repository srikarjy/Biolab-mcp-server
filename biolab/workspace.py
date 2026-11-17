"""Scientist projects, evidence collections, and saved evidence watches."""

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from biolab import audit, evidence

RELEVANCE_VALUES = {"unreviewed", "relevant", "excluded", "conflicting", "low_quality"}
STANCE_VALUES = {"supports", "conflicts", "context", "insufficient"}
ASSESSMENT_VALUES = {"unreviewed", "human_reviewed", "agent_proposed"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _organization_id(owner_id: str) -> str:
    return "org_" + hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:24]


def create_project(conn: Any, name: str, description: str = "", owner_id: str = "local") -> dict:
    if not name.strip():
        raise ValueError("project name must not be empty")
    project = {
        "project_id": str(uuid.uuid4()),
        "name": name.strip(),
        "description": description.strip(),
        "created_at": _now(),
        "owner_id": owner_id,
        "organization_id": _organization_id(owner_id),
    }
    organization_id = project["organization_id"]
    conn.execute(
        "INSERT OR IGNORE INTO organizations (organization_id, name, created_by, created_at) VALUES (?, ?, ?, ?)",
        (organization_id, f"{owner_id} workspace", owner_id, project["created_at"]),
    )
    conn.execute(
        "INSERT OR IGNORE INTO organization_members (organization_id, member_id, role, created_at) VALUES (?, ?, ?, ?)",
        (organization_id, owner_id, "owner", project["created_at"]),
    )
    conn.execute(
        "INSERT INTO projects (project_id, name, description, created_at, owner_id, organization_id) VALUES (?, ?, ?, ?, ?, ?)",
        tuple(project.values()),
    )
    audit.record(conn, project["project_id"], "project_created", "project", project["project_id"], owner_id, {"name": project["name"]})
    conn.commit()
    return project


def list_projects(conn: Any, owner_id: str | None = None) -> list[dict]:
    where = "WHERE owner_id = ?" if owner_id else ""
    params = (owner_id,) if owner_id else ()
    rows = conn.execute(
        f"SELECT project_id, name, description, created_at, owner_id, organization_id FROM projects {where} ORDER BY created_at DESC",
        params,
    ).fetchall()
    return [
        {"project_id": row[0], "name": row[1], "description": row[2], "created_at": row[3], "owner_id": row[4], "organization_id": row[5]}
        for row in rows
    ]


def has_project_access(conn: Any, project_id: str, owner_id: str) -> bool:
    row = conn.execute(
        "SELECT owner_id, organization_id FROM projects WHERE project_id = ?", (project_id,)
    ).fetchone()
    if row is None:
        return False
    if row[0] == owner_id:
        return True
    return conn.execute(
        "SELECT 1 FROM organization_members WHERE organization_id = ? AND member_id = ?",
        (row[1], owner_id),
    ).fetchone() is not None


def create_collection(conn: Any, project_id: str, name: str, description: str = "", owner_id: str | None = None) -> dict:
    if not name.strip():
        raise ValueError("collection name must not be empty")
    if conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
        raise ValueError("project not found")
    if owner_id is not None and not has_project_access(conn, project_id, owner_id):
        raise ValueError("project access denied")
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


def save_reviewed_candidates(
    conn: Any, project_id: str, ranking_run_id: str, candidate_ids: list[str],
    owner_id: str | None = None, override_note: str = "",
) -> dict:
    """Atomically save accepted ranking candidates into a project collection."""
    if owner_id is not None and not has_project_access(conn, project_id, owner_id):
        raise ValueError("project access denied")
    if not candidate_ids:
        raise ValueError("candidate_ids must not be empty")
    rows = conn.execute(
        """SELECT ranking_candidate_id, retrieval_id, review_state, review_note, rank
           FROM ranking_candidates WHERE ranking_run_id = ? AND ranking_candidate_id IN ({})""".format(
            ",".join("?" for _ in candidate_ids)
        ),
        (ranking_run_id, *candidate_ids),
    ).fetchall()
    if len(rows) != len(set(candidate_ids)):
        raise ValueError("one or more ranking candidates were not found in this run")
    rejected = [row[0] for row in rows if row[2] != "accepted" and not override_note.strip()]
    if rejected:
        raise ValueError("only accepted candidates can be saved without override_note")
    collection_row = conn.execute(
        "SELECT collection_id FROM evidence_collections WHERE project_id = ? AND name = ?",
        (project_id, "Reviewed ranking evidence"),
    ).fetchone()
    if collection_row is None:
        collection = create_collection(conn, project_id, "Reviewed ranking evidence", "Saved from a reviewed ranking run", owner_id)
        collection_id = collection["collection_id"]
    else:
        collection_id = collection_row[0]
    saved: list[str] = []
    try:
        for _candidate_id, retrieval_id, _review_state, review_note, rank in rows:
            note = override_note.strip() or review_note or f"Accepted from rank {rank}"
            conn.execute(
                """INSERT INTO collection_items (collection_id, retrieval_id, note, relevance, added_at)
                   VALUES (?, ?, ?, 'relevant', ?)
                   ON CONFLICT(collection_id, retrieval_id) DO UPDATE SET note=excluded.note, relevance='relevant'""",
                (collection_id, retrieval_id, note, _now()),
            )
            saved.append(retrieval_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"project_id": project_id, "ranking_run_id": ranking_run_id, "collection_id": collection_id, "saved_retrieval_ids": saved, "saved_count": len(saved)}


def list_collections(conn: Any, project_id: str | None = None, owner_id: str | None = None) -> list[dict]:
    where = "WHERE ec.project_id = ?" if project_id else ""
    params: tuple[str, ...] = (project_id,) if project_id else ()
    if owner_id:
        where += " AND " if where else "WHERE "
        where += "p.owner_id = ?"
        params += (owner_id,)
    rows = conn.execute(
        f"""SELECT ec.collection_id, ec.project_id, ec.name, ec.description, ec.created_at
            FROM evidence_collections ec JOIN projects p ON p.project_id = ec.project_id
            {where} ORDER BY ec.created_at""",
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
    owner_id: str | None = None,
) -> dict:
    if relevance not in RELEVANCE_VALUES:
        raise ValueError(f"invalid relevance; choose from: {', '.join(sorted(RELEVANCE_VALUES))}")
    collection_row = conn.execute(
        "SELECT project_id FROM evidence_collections WHERE collection_id = ?", (collection_id,)
    ).fetchone()
    if collection_row is None:
        raise ValueError("collection not found")
    if owner_id is not None and not has_project_access(conn, collection_row[0], owner_id):
        raise ValueError("project access denied")
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


def get_collection(conn: Any, collection_id: str, owner_id: str | None = None) -> dict | None:
    row = conn.execute(
        """SELECT collection_id, project_id, name, description, created_at
           FROM evidence_collections WHERE collection_id = ?""",
        (collection_id,),
    ).fetchone()
    if row is None:
        return None
    if owner_id is not None and not has_project_access(conn, row[1], owner_id):
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


def save_retrieval_to_project(conn: Any, project_id: str, retrieval_id: str, owner_id: str) -> dict:
    if not has_project_access(conn, project_id, owner_id):
        raise ValueError("project access denied")
    if conn.execute("SELECT 1 FROM retrievals WHERE retrieval_id = ?", (retrieval_id,)).fetchone() is None:
        raise ValueError("retrieval not found")
    row = conn.execute(
        "SELECT collection_id FROM evidence_collections WHERE project_id = ? AND name = ?",
        (project_id, "Selected papers"),
    ).fetchone()
    collection = get_collection(conn, row[0], owner_id) if row else create_collection(conn, project_id, "Selected papers", "MCP-retrieved papers selected for parsing", owner_id)
    if collection is None:
        raise ValueError("selected papers collection not found")
    result = add_collection_item(conn, collection["collection_id"], retrieval_id, "Selected for project parsing", "unreviewed", owner_id)
    audit.record(conn, project_id, "paper_selected", "retrieval", retrieval_id, owner_id, {"source": result.get("source", "")})
    conn.commit()
    return result


def list_project_retrievals(conn: Any, project_id: str, owner_id: str) -> list[dict]:
    if not has_project_access(conn, project_id, owner_id):
        raise ValueError("project access denied")
    rows = conn.execute(
        """SELECT DISTINCT r.retrieval_id, r.source, r.external_id, r.retrieved_at, r.snapshot,
                  ci.relevance, pr.parser_run_id
           FROM collection_items ci JOIN evidence_collections ec ON ec.collection_id = ci.collection_id
           JOIN retrievals r ON r.retrieval_id = ci.retrieval_id
           LEFT JOIN parser_runs pr ON pr.retrieval_id = r.retrieval_id AND pr.project_id = ?
           WHERE ec.project_id = ? ORDER BY r.retrieved_at DESC""",
        (project_id, project_id),
    ).fetchall()
    return [{
        "retrieval_id": row[0], "source": row[1], "external_id": row[2], "retrieved_at": row[3],
        "snapshot": json.loads(row[4]), "relevance": row[5], "parser_run_id": row[6],
    } for row in rows]


def save_project_note(conn: Any, project_id: str, question: str, note_text: str, citations: list[dict], created_by: str) -> dict:
    if not has_project_access(conn, project_id, created_by):
        raise ValueError("project access denied")
    if not note_text.strip():
        raise ValueError("note_text must not be empty")
    note = {"note_id": str(uuid.uuid4()), "project_id": project_id, "question": question.strip(), "note_text": note_text.strip(), "citations": citations, "created_by": created_by, "created_at": _now()}
    conn.execute("INSERT INTO project_notes (note_id, project_id, question, note_text, citations, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (note["note_id"], project_id, note["question"], note["note_text"], json.dumps(citations), created_by, note["created_at"]))
    audit.record(conn, project_id, "note_saved", "note", note["note_id"], created_by, {"question": note["question"], "citation_count": len(citations)})
    conn.commit()
    return note


def list_project_notes(conn: Any, project_id: str, owner_id: str) -> list[dict]:
    if not has_project_access(conn, project_id, owner_id):
        raise ValueError("project access denied")
    rows = conn.execute("SELECT note_id, question, note_text, citations, created_by, created_at FROM project_notes WHERE project_id = ? ORDER BY created_at DESC", (project_id,)).fetchall()
    return [{"note_id": row[0], "question": row[1], "note_text": row[2], "citations": json.loads(row[3]), "created_by": row[4], "created_at": row[5]} for row in rows]


def create_watch(
    conn: Any,
    project_id: str,
    source: str,
    query_text: str,
    agent_id: str,
    max_results: int = 10,
    owner_id: str | None = None,
) -> dict:
    if conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
        raise ValueError("project not found")
    if owner_id is not None and not has_project_access(conn, project_id, owner_id):
        raise ValueError("project access denied")
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


def list_watches(conn: Any, project_id: str | None = None, owner_id: str | None = None) -> list[dict]:
    where = "WHERE ew.project_id = ?" if project_id else ""
    params: tuple[str, ...] = (project_id,) if project_id else ()
    if owner_id:
        where += " AND " if where else "WHERE "
        where += "p.owner_id = ?"
        params += (owner_id,)
    rows = conn.execute(
        f"""SELECT ew.watch_id, ew.project_id, ew.source, ew.query_text, ew.agent_id, ew.max_results,
                       ew.active, ew.created_at, ew.last_checked_at
            FROM evidence_watches ew JOIN projects p ON p.project_id = ew.project_id {where} ORDER BY ew.created_at""",
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


def run_watch(conn: Any, watch_id: str, owner_id: str | None = None) -> dict:
    row = conn.execute(
        """SELECT project_id, source, query_text, agent_id, max_results, active, previous_ids
           FROM evidence_watches WHERE watch_id = ?""",
        (watch_id,),
    ).fetchone()
    if row is None:
        raise ValueError("watch not found")
    if owner_id is not None and not has_project_access(conn, row[0], owner_id):
        raise ValueError("watch not found")
    if not row[5]:
        raise ValueError("watch is inactive")

    records = evidence.search_source(conn, row[1], row[2], row[3], row[4])
    stored_state = json.loads(row[6])
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


def run_active_watches(conn: Any, owner_id: str | None = None) -> dict:
    results = []
    errors = []
    for watch in list_watches(conn, owner_id=owner_id):
        if not watch["active"]:
            continue
        try:
            results.append(run_watch(conn, watch["watch_id"], owner_id) if owner_id is not None else run_watch(conn, watch["watch_id"]))
        except Exception as exc:
            errors.append({"watch_id": watch["watch_id"], "error": str(exc)})
    return {"completed": results, "errors": errors}


def list_watch_events(conn: Any, watch_id: str, owner_id: str | None = None) -> list[dict]:
    project_row = conn.execute(
        "SELECT project_id FROM evidence_watches WHERE watch_id = ?", (watch_id,)
    ).fetchone()
    if project_row is None or (owner_id is not None and not has_project_access(conn, project_row[0], owner_id)):
        raise ValueError("watch not found")
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


def create_claim(conn: Any, project_id: str, claim_text: str, owner_id: str | None = None) -> dict:
    if conn.execute("SELECT 1 FROM projects WHERE project_id = ?", (project_id,)).fetchone() is None:
        raise ValueError("project not found")
    if owner_id is not None and not has_project_access(conn, project_id, owner_id):
        raise ValueError("project access denied")
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
    owner_id: str | None = None,
) -> dict:
    if stance not in STANCE_VALUES:
        raise ValueError(f"invalid stance; choose from: {', '.join(sorted(STANCE_VALUES))}")
    if assessment not in ASSESSMENT_VALUES:
        raise ValueError(
            f"invalid assessment; choose from: {', '.join(sorted(ASSESSMENT_VALUES))}"
        )
    claim_row = conn.execute("SELECT project_id FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
    if claim_row is None:
        raise ValueError("claim not found")
    if owner_id is not None and not has_project_access(conn, claim_row[0], owner_id):
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


def trace_claim(conn: Any, claim_id: str, owner_id: str | None = None) -> dict | None:
    row = conn.execute(
        "SELECT claim_id, project_id, claim_text, created_at FROM claims WHERE claim_id = ?",
        (claim_id,),
    ).fetchone()
    if row is None:
        return None
    if owner_id is not None and not has_project_access(conn, row[1], owner_id):
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


def get_project_report(conn: Any, project_id: str, owner_id: str | None = None) -> dict | None:
    if owner_id is not None and not has_project_access(conn, project_id, owner_id):
        return None
    project_row = conn.execute(
        "SELECT project_id, name, description, created_at, owner_id, organization_id FROM projects WHERE project_id = ?",
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
    parser_runs = conn.execute(
        """SELECT parser_run_id, filename, parser_model, source_hash, retrieval_id, status,
                  block_count, created_by, created_at
           FROM parser_runs WHERE project_id = ? ORDER BY created_at DESC""",
        (project_id,),
    ).fetchall()
    return {
        "project": {
            "project_id": project_row[0], "name": project_row[1],
            "description": project_row[2], "created_at": project_row[3],
            "owner_id": project_row[4], "organization_id": project_row[5],
        },
        "collections": [get_collection(conn, row[0]) for row in collection_ids],
        "papers": list_project_retrievals(conn, project_id, project_row[4]),
        "notes": list_project_notes(conn, project_id, project_row[4]),
        "claims": [trace_claim(conn, row[0]) for row in claim_ids],
        "parser_runs": [
            {
                "parser_run_id": row[0], "filename": row[1], "parser_model": row[2],
                "source_hash": row[3], "retrieval_id": row[4], "status": row[5], "block_count": row[6],
                "created_by": row[7], "created_at": row[8],
            }
            for row in parser_runs
        ],
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
    lines.extend(["## Parsed documents", ""])
    for parsed in report.get("parser_runs", []):
        lines.extend([
            f"- **{parsed['filename']}** — `{parsed['parser_run_id']}`",
            f"  - Status: {parsed['status']}; blocks: {parsed['block_count']}",
            f"  - Source hash: `{parsed['source_hash']}`",
        ])
    if not report.get("parser_runs"):
        lines.append("No parsed documents.")
    lines.append("")
    lines.extend(
        [
            "",
            "---",
            "This report separates retrieved source records, agent-proposed relationships, and human review. It is not clinical advice.",
            "",
        ]
    )
    return "\n".join(lines)
