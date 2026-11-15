"""Parser-run persistence and disposable parsed-document storage."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from biolab import hf_parser, rag, workspace

SECTION_NAMES = ("title", "abstract", "introduction", "methods", "results", "discussion", "conclusion", "table", "figure_caption", "references", "unknown")


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


def create_parser_run(conn: Any, content: bytes, filename: str, created_by: str, project_id: str | None = None) -> dict[str, Any]:
    if project_id and not workspace.has_project_access(conn, project_id, created_by):
        raise ValueError("project access denied")
    parsed = hf_parser.parse_document(content, filename)
    document_id = str(uuid.uuid4())
    for block in parsed["blocks"]:
        block["section_name"] = classify_section(block)
    run_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO parser_runs
           (parser_run_id, project_id, document_id, filename, parser_model, source_hash, status, block_count, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, project_id, document_id, filename, parsed["parser_model"], parsed["source_hash"], parsed["status"], parsed["block_count"], created_by, created_at),
    )
    for block in parsed["blocks"]:
        conn.execute(
            """INSERT INTO parsed_blocks
               (block_id, parser_run_id, page_number, block_index, block_type, section_name, text, bbox, content_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (block["block_id"], run_id, block["page_number"], block["block_index"], block["block_type"], block["section_name"], block["text"], json.dumps(block["bbox"]), hashlib.sha256(block["text"].encode()).hexdigest(), created_at),
        )
    conn.commit()
    vector_status = rag.index_parser_blocks(project_id or "unassigned", document_id, run_id, parsed["blocks"])
    return {"parser_run_id": run_id, "project_id": project_id, "document_id": document_id, "filename": filename, "parser_model": parsed["parser_model"], "source_hash": parsed["source_hash"], "status": parsed["status"], "block_count": parsed["block_count"], "created_by": created_by, "created_at": created_at, "blocks": parsed["blocks"], "vector_status": vector_status}


def get_parser_run(conn: Any, parser_run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT parser_run_id, project_id, document_id, filename, parser_model, source_hash, status, block_count, created_by, created_at FROM parser_runs WHERE parser_run_id = ?", (parser_run_id,)).fetchone()
    if row is None:
        return None
    blocks = conn.execute("SELECT block_id, page_number, block_index, block_type, section_name, text, bbox, content_hash FROM parsed_blocks WHERE parser_run_id = ? ORDER BY page_number, block_index", (parser_run_id,)).fetchall()
    return {"parser_run_id": row[0], "project_id": row[1], "document_id": row[2], "filename": row[3], "parser_model": row[4], "source_hash": row[5], "status": row[6], "block_count": row[7], "created_by": row[8], "created_at": row[9], "blocks": [{"block_id": item[0], "page_number": item[1], "block_index": item[2], "block_type": item[3], "section_name": item[4], "text": item[5], "bbox": json.loads(item[6]) if item[6] != "null" else None, "content_hash": item[7]} for item in blocks]}


def list_parser_runs(conn: Any, project_id: str, owner_id: str) -> list[dict[str, Any]]:
    if not workspace.has_project_access(conn, project_id, owner_id):
        raise ValueError("project access denied")
    rows = conn.execute(
        "SELECT parser_run_id, project_id, filename, parser_model, source_hash, status, block_count, created_by, created_at FROM parser_runs WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    ).fetchall()
    return [{"parser_run_id": row[0], "project_id": row[1], "filename": row[2], "parser_model": row[3], "source_hash": row[4], "status": row[5], "block_count": row[6], "created_by": row[7], "created_at": row[8]} for row in rows]


def assert_run_access(conn: Any, parser_run_id: str, owner_id: str) -> dict[str, Any]:
    result = get_parser_run(conn, parser_run_id)
    if result is None or result["created_by"] != owner_id and not (result["project_id"] and workspace.has_project_access(conn, result["project_id"], owner_id)):
        raise ValueError("parser run not found")
    return result


def flush_parser_run(conn: Any, parser_run_id: str) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM parser_runs WHERE parser_run_id = ?", (parser_run_id,)).fetchone() is None:
        raise ValueError("parser run not found")
    count = conn.execute("SELECT count(*) FROM parsed_blocks WHERE parser_run_id = ?", (parser_run_id,)).fetchone()[0]
    conn.execute("DELETE FROM parsed_blocks WHERE parser_run_id = ?", (parser_run_id,))
    conn.commit()
    vector_count = rag.flush_parser_context(parser_run_id)
    return {"parser_run_id": parser_run_id, "flushed_parsed_blocks": count, "flushed_vector_blocks": vector_count, "claims_preserved": True}
