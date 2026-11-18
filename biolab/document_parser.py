"""Parser-run persistence and disposable parsed-document storage."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from biolab import hf_parser, rag, workspace

SECTION_NAMES = ("title", "abstract", "introduction", "methods", "results", "discussion", "conclusion", "table", "figure_caption", "references", "unknown")
REVIEW_STATES = {"pending", "accepted", "excluded"}


def classify_section(block: dict[str, Any]) -> str:
    block_type = str(block.get("block_type", "")).lower()
    text = str(block.get("text", "")).strip().lower()
    if "caption" in block_type or text.startswith(("figure ", "fig. ")):
        return "figure_caption"
    if "table" in block_type or text.startswith("table "):
        return "table"
    for section in ("abstract", "introduction", "methods", "results", "discussion", "conclusion", "references"):
        if text.rstrip(".:").strip() == section or text.startswith(section + "\n"):
            return section
    if not text:
        return "unknown"
    return "unknown"


def assign_sections(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Propagate explicit section headings to following page blocks."""
    current = "unknown"
    assigned: list[dict[str, Any]] = []
    for block in blocks:
        direct = classify_section(block)
        text = str(block.get("text", "")).strip().lower().rstrip(".:")
        block_type = str(block.get("block_type", "")).lower()
        is_heading = block_type in {"heading", "header", "section"} or text in SECTION_NAMES
        if direct != "unknown":
            current = direct
        elif is_heading:
            current = text if text in SECTION_NAMES else current
        enriched = dict(block)
        enriched["section_name"] = direct if direct != "unknown" else current
        assigned.append(enriched)
    return assigned


def create_parser_run(conn: Any, content: bytes, filename: str, created_by: str, project_id: str | None = None, retrieval_id: str | None = None) -> dict[str, Any]:
    if project_id and not workspace.has_project_access(conn, project_id, created_by):
        raise ValueError("project access denied")
    parsed = hf_parser.parse_document(content, filename)
    document_id = str(uuid.uuid4())
    parsed["blocks"] = assign_sections(parsed["blocks"])
    run_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO parser_runs
           (parser_run_id, project_id, document_id, filename, parser_model, source_hash, retrieval_id, status, block_count, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, project_id, document_id, filename, parsed["parser_model"], parsed["source_hash"], retrieval_id, parsed["status"], parsed["block_count"], created_by, created_at),
    )
    for block in parsed["blocks"]:
        conn.execute(
            """INSERT INTO parsed_blocks
               (block_id, parser_run_id, page_number, block_index, block_type, section_name, text, bbox, metadata, content_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (block["block_id"], run_id, block["page_number"], block["block_index"], block["block_type"], block["section_name"], block["text"], json.dumps(block["bbox"]), json.dumps(block.get("metadata", {}), sort_keys=True), hashlib.sha256(block["text"].encode()).hexdigest(), created_at),
        )
    conn.commit()
    vector_status = {"status": "pending_review", "indexed_blocks": 0}
    return {"parser_run_id": run_id, "project_id": project_id, "document_id": document_id, "retrieval_id": retrieval_id, "filename": filename, "parser_model": parsed["parser_model"], "source_hash": parsed["source_hash"], "status": parsed["status"], "block_count": parsed["block_count"], "created_by": created_by, "created_at": created_at, "blocks": parsed["blocks"], "vector_status": vector_status}


def create_parser_run_from_retrieval(conn: Any, project_id: str, retrieval_id: str, created_by: str) -> dict[str, Any]:
    if not workspace.has_project_access(conn, project_id, created_by):
        raise ValueError("project access denied")
    record = conn.execute(
        "SELECT source, external_id, snapshot FROM retrievals WHERE retrieval_id = ?", (retrieval_id,)
    ).fetchone()
    if record is None:
        raise ValueError("retrieval not found")
    snapshot = json.loads(record[2])
    fields = [snapshot.get(name) for name in ("title", "abstract", "summary", "description", "name")]
    text = "\n\n".join(str(value).strip() for value in fields if value and str(value).strip())
    if not text:
        raise ValueError("retrieval has no parseable title, abstract, summary, description, or name")
    safe_external = re.sub(r"[^A-Za-z0-9._-]+", "-", record[1]).strip("-") or "paper"
    return create_parser_run(conn, text.encode("utf-8"), f"{record[0]}-{safe_external}.txt", created_by, project_id, retrieval_id)


def get_parser_run(conn: Any, parser_run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT parser_run_id, project_id, document_id, filename, parser_model, source_hash, retrieval_id, status, block_count, created_by, created_at FROM parser_runs WHERE parser_run_id = ?", (parser_run_id,)).fetchone()
    if row is None:
        return None
    blocks = conn.execute("SELECT block_id, page_number, block_index, block_type, section_name, text, bbox, metadata, content_hash, review_state, review_note FROM parsed_blocks WHERE parser_run_id = ? ORDER BY page_number, block_index", (parser_run_id,)).fetchall()
    return {"parser_run_id": row[0], "project_id": row[1], "document_id": row[2], "filename": row[3], "parser_model": row[4], "source_hash": row[5], "retrieval_id": row[6], "status": row[7], "block_count": row[8], "created_by": row[9], "created_at": row[10], "blocks": [{"block_id": item[0], "page_number": item[1], "block_index": item[2], "block_type": item[3], "section_name": item[4], "text": item[5], "bbox": json.loads(item[6]) if item[6] != "null" else None, "metadata": json.loads(item[7] or "{}"), "content_hash": item[8], "review_state": item[9], "review_note": item[10]} for item in blocks]}


def review_block(conn: Any, parser_run_id: str, block_id: str, review_state: str, reviewed_by: str, note: str = "") -> dict[str, Any]:
    if review_state not in REVIEW_STATES - {"pending"}:
        raise ValueError("review_state must be accepted or excluded")
    run = assert_run_access(conn, parser_run_id, reviewed_by)
    row = conn.execute("SELECT block_id, page_number, block_type, section_name, text, metadata, content_hash FROM parsed_blocks WHERE parser_run_id = ? AND block_id = ?", (parser_run_id, block_id)).fetchone()
    if row is None:
        raise ValueError("parsed block not found")
    vector_status = {"status": "not_indexed", "indexed_blocks": 0}
    if review_state == "accepted" and run["project_id"]:
        vector_status = rag.index_parser_blocks(run["project_id"], run["document_id"] or run["parser_run_id"], parser_run_id, [{"block_id": row[0], "page_number": row[1], "block_type": row[2], "section_name": row[3], "text": row[4], "metadata": json.loads(row[5] or "{}"), "content_hash": row[6]}])
        if vector_status.get("status") != "active":
            return {"parser_run_id": parser_run_id, "block_id": block_id, "review_state": "pending", "review_note": "", "vector_status": vector_status}
    elif review_state == "accepted":
        vector_status = {"status": "not_indexed", "reason": "project required for project-scoped RAG", "indexed_blocks": 0}
    else:
        rag.flush_parser_block(block_id)
    conn.execute("UPDATE parsed_blocks SET review_state = ?, review_note = ? WHERE block_id = ?", (review_state, note.strip(), block_id))
    conn.commit()
    return {"parser_run_id": parser_run_id, "block_id": block_id, "review_state": review_state, "review_note": note.strip(), "vector_status": vector_status}


def list_parser_runs(conn: Any, project_id: str, owner_id: str) -> list[dict[str, Any]]:
    if not workspace.has_project_access(conn, project_id, owner_id):
        raise ValueError("project access denied")
    rows = conn.execute(
        "SELECT parser_run_id, project_id, filename, parser_model, source_hash, retrieval_id, status, block_count, created_by, created_at FROM parser_runs WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return [{"parser_run_id": row[0], "project_id": row[1], "filename": row[2], "parser_model": row[3], "source_hash": row[4], "retrieval_id": row[5], "status": row[6], "block_count": row[7], "created_by": row[8], "created_at": row[9]} for row in rows]


def assert_run_access(conn: Any, parser_run_id: str, owner_id: str) -> dict[str, Any]:
    result = get_parser_run(conn, parser_run_id)
    if result is None or result["created_by"] != owner_id and not (result["project_id"] and workspace.has_project_access(conn, result["project_id"], owner_id)):
        raise ValueError("parser run not found")
    return result


def flush_parser_run(conn: Any, parser_run_id: str) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM parser_runs WHERE parser_run_id = ?", (parser_run_id,)).fetchone() is None:
        raise ValueError("parser run not found")
    count = conn.execute("SELECT count(*) FROM parsed_blocks WHERE parser_run_id = ?", (parser_run_id,)).fetchone()[0]
    vector_count = rag.flush_parser_context(parser_run_id)
    return {"parser_run_id": parser_run_id, "preserved_parsed_blocks": count, "flushed_vector_blocks": vector_count, "claims_preserved": True}


def rebuild_parser_index(conn: Any, parser_run_id: str, owner_id: str) -> dict[str, Any]:
    run = assert_run_access(conn, parser_run_id, owner_id)
    if not run["project_id"]:
        return {"status": "not_indexed", "reason": "project required for project-scoped RAG", "indexed_blocks": 0}
    rows = conn.execute(
        """SELECT block_id, page_number, block_type, section_name, text, metadata, content_hash
           FROM parsed_blocks WHERE parser_run_id = ? AND review_state = 'accepted'
           ORDER BY page_number, block_index""",
        (parser_run_id,),
    ).fetchall()
    status = rag.index_parser_blocks(
        run["project_id"], run["document_id"] or parser_run_id, parser_run_id,
        [{"block_id": row[0], "page_number": row[1], "block_type": row[2], "section_name": row[3], "text": row[4], "metadata": json.loads(row[5] or "{}"), "content_hash": row[6]} for row in rows],
    )
    status["accepted_blocks"] = len(rows)
    return status
