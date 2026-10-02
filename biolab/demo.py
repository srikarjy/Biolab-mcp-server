"""Isolated offline demonstration using explicitly synthetic evidence."""

import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import UTC, datetime

from biolab import db, retrieval_log
from biolab.models import RetrievalRecord


def run_demo() -> dict:
    """Exercise real storage without touching configured local or remote databases."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(db.SCHEMA_V2)
    try:
        # Use the synchronous storage path: the server's global writer may be active.
        records = []
        for index in range(2):
            payload = {"title": f"Synthetic evidence example {index + 1}", "demo": True}
            record = RetrievalRecord(
                retrieval_id=str(uuid.uuid4()), source="demo",
                external_id=f"SYNTHETIC-{index + 1}", query_text="example evidence query",
                retrieved_at=datetime.now(UTC).isoformat(), agent_id="portfolio:demo",
                source_metadata=json.dumps({"synthetic": True}),
                raw_response=json.dumps(payload), snapshot=json.dumps(payload),
                response_hash="", prev_hash="",
            )
            retrieval_log._write_sync(conn, record)
            records.append(record)
        fetched = retrieval_log.get_retrieval(conn, records[0].retrieval_id)
        if fetched is None:
            raise RuntimeError("Retrieval was not persisted")
        valid, _ = retrieval_log.verify_chain(conn)
        conn.execute("UPDATE retrievals SET raw_response = ? WHERE retrieval_id = ?",
                     ('{"title":"altered"}', fetched.retrieval_id))
        conn.commit()
        valid_after, broken_id = retrieval_log.verify_chain(conn)
        if not valid or valid_after or broken_id != fetched.retrieval_id:
            raise RuntimeError("Integrity demonstration failed")
        return {
            "mode": "offline / synthetic / isolated in-memory database",
            "records_written": len(records),
            "evidence_reference": {"retrieval_id": fetched.retrieval_id},
            "retrieved_record": asdict(fetched),
            "chain_valid_before_edit": valid,
            "tampering_detected": not valid_after,
            "first_broken_retrieval_id": broken_id,
        }
    finally:
        conn.close()
