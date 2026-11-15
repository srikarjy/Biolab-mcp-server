"""Parser-run persistence and disposable parsed-document storage."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from biolab import hf_parser


def create_parser_run(conn: Any, content: bytes, filename: str, created_by: str) -> dict[str, Any]:
    parsed = hf_parser.parse_document(content, filename)
    run_id = str(uuid.uuid4())
    created_at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO parser_runs
           (parser_run_id, filename, parser_model, source_hash, status, block_count, created_by, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (run_id, filename, parsed["parser_model"], parsed["source_hash"], parsed["status"], parsed["block_count"], created_by, created_at),
    )
    for block in parsed["blocks"]:
        conn.execute(
            """INSERT INTO parsed_blocks
               (block_id, parser_run_id, page_number, block_index, block_type, text, bbox, content_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (block["block_id"], run_id, block["page_number"], block["block_index"], block["block_type"], block["text"], json.dumps(block["bbox"]), hashlib.sha256(block["text"].encode()).hexdigest(), created_at),
        )
    conn.commit()
    return {"parser_run_id": run_id, "filename": filename, "parser_model": parsed["parser_model"], "source_hash": parsed["source_hash"], "status": parsed["status"], "block_count": parsed["block_count"], "created_by": created_by, "created_at": created_at, "blocks": parsed["blocks"]}


def get_parser_run(conn: Any, parser_run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT parser_run_id, filename, parser_model, source_hash, status, block_count, created_by, created_at FROM parser_runs WHERE parser_run_id = ?", (parser_run_id,)).fetchone()
    if row is None:
        return None
    blocks = conn.execute("SELECT block_id, page_number, block_index, block_type, text, bbox, content_hash FROM parsed_blocks WHERE parser_run_id = ? ORDER BY page_number, block_index", (parser_run_id,)).fetchall()
    return {"parser_run_id": row[0], "filename": row[1], "parser_model": row[2], "source_hash": row[3], "status": row[4], "block_count": row[5], "created_by": row[6], "created_at": row[7], "blocks": [{"block_id": item[0], "page_number": item[1], "block_index": item[2], "block_type": item[3], "text": item[4], "bbox": json.loads(item[5]) if item[5] != "null" else None, "content_hash": item[6]} for item in blocks]}


def flush_parser_run(conn: Any, parser_run_id: str) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM parser_runs WHERE parser_run_id = ?", (parser_run_id,)).fetchone() is None:
        raise ValueError("parser run not found")
    count = conn.execute("SELECT count(*) FROM parsed_blocks WHERE parser_run_id = ?", (parser_run_id,)).fetchone()[0]
    conn.execute("DELETE FROM parsed_blocks WHERE parser_run_id = ?", (parser_run_id,))
    conn.commit()
    return {"parser_run_id": parser_run_id, "flushed_parsed_blocks": count, "claims_preserved": True}
