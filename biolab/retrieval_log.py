"""The only module that writes to the retrieval log. One writer, one place.

DB-write failures are not caught here — they propagate to the caller uncaught, hard-failing
the tool call rather than risking a paper returned without a retrieval_id.

Includes a write-queue for concurrent access safety (v2+).
"""

import hashlib
import hmac
import json
import queue
import threading
import uuid
from concurrent.futures import Future
from datetime import UTC, datetime
from typing import Any

from biolab import db as db_module
from biolab.models import RetrievalRecord

_write_queue: queue.Queue[tuple | None] = queue.Queue()
_writer_thread: threading.Thread | None = None
_writer_stop = threading.Event()
_writer_db_path: str | None = None


def _writer_loop(db_path: str) -> None:
    """Writer loop runs in its own thread with its own libSQL connection."""
    target, token = db_module.resolve_target(db_path)
    conn = db_module.open_connection(target, token)
    try:
        while True:
            try:
                item = _write_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            record, result = item
            try:
                _chain_and_insert(conn, record)
                conn.commit()
            except Exception as exc:
                conn.rollback()
                result.set_exception(exc)
            else:
                result.set_result(None)
    finally:
        conn.close()


def start_writer(db_path: str) -> None:
    """Start the background writer thread (call once at server startup)."""
    global _writer_thread, _writer_db_path
    target, _ = db_module.resolve_target(db_path)
    if _writer_thread is not None and _writer_thread.is_alive():
        if _writer_db_path == target:
            return  # already running for this db
        stop_writer()  # different db, restart
    _writer_stop.clear()
    _writer_db_path = target
    _writer_thread = threading.Thread(target=_writer_loop, args=(db_path,), daemon=True)
    _writer_thread.start()


def stop_writer() -> None:
    """Stop the background writer thread (call at shutdown)."""
    global _writer_thread, _writer_db_path
    if _writer_thread is None:
        return
    _writer_stop.set()
    _write_queue.put(None)
    if _writer_thread is not None:
        _writer_thread.join()
    _writer_thread = None
    _writer_db_path = None


HASH_VERSION = 2


def compute_hash(
    version: int,
    prev_hash: str,
    retrieval_id: str,
    source: str,
    external_id: str,
    query_text: str,
    retrieved_at: str,
    agent_id: str,
    source_metadata: str,
    raw_response: str,
    snapshot: str,
) -> str:
    """The chained hash of one retrieval row.

    Version 1 (legacy rows) covered only raw_response, so query, caller identity and the
    normalized snapshot could be rewritten without breaking verification. Version 2
    hashes every audited column as a JSON array, which is unambiguous (no field can
    bleed into its neighbour) and stable across Python versions.
    """
    if version == 1:
        material = prev_hash + raw_response + retrieval_id + retrieved_at
    elif version == 2:
        material = json.dumps(
            [
                "biolab-retrieval-v2", prev_hash, retrieval_id, source, external_id,
                query_text, retrieved_at, agent_id, source_metadata, raw_response, snapshot,
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )
    else:
        raise ValueError(f"unknown hash_version {version}")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def get_last_hash(conn: Any) -> str:
    """The response_hash of the most recently inserted row, or "" if the table is empty.

    This is the genesis value a fresh chain starts from.
    """
    row = conn.execute("SELECT response_hash FROM retrievals ORDER BY rowid DESC LIMIT 1").fetchone()
    return row[0] if row else ""


def _chain_and_insert(conn: Any, record: RetrievalRecord) -> None:
    """Compute this row's chained hash and insert it.

    Must run on whichever path is currently serializing writes (the background
    writer thread, or — when no writer is running — the caller's own thread via
    _write_sync) so that reading the last hash and inserting the next row is
    never interleaved with another writer doing the same (AD-9).
    """
    record.prev_hash = get_last_hash(conn)
    record.hash_version = HASH_VERSION
    record.response_hash = compute_hash(
        record.hash_version, record.prev_hash, record.retrieval_id, record.source,
        record.external_id, record.query_text, record.retrieved_at, record.agent_id,
        record.source_metadata, record.raw_response, record.snapshot,
    )

    conn.execute(
        """
        INSERT INTO retrievals
            (retrieval_id, source, external_id, query_text, retrieved_at,
             agent_id, source_metadata, raw_response, snapshot, response_hash, prev_hash,
             hash_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.retrieval_id,
            record.source,
            record.external_id,
            record.query_text,
            record.retrieved_at,
            record.agent_id,
            record.source_metadata,
            record.raw_response,
            record.snapshot,
            record.response_hash,
            record.prev_hash,
            record.hash_version,
        ),
    )


def _write_sync(
    conn: Any,
    record: RetrievalRecord,
) -> None:
    """Synchronous write fallback (used in tests or when writer not running)."""
    try:
        _chain_and_insert(conn, record)
        conn.commit()
    except Exception:
        # Rollback on any error to avoid leaving partial transactions
        conn.rollback()
        raise


def verify_chain(conn: Any) -> tuple[bool, str | None]:
    """Walk the retrieval log in insertion order and verify the hash chain.

    Returns (True, None) if every row is internally consistent. Returns
    (False, retrieval_id) for the first row where the chain breaks — either its
    stored prev_hash doesn't match the previous row's response_hash, or its
    response_hash doesn't match what recomputing from its own content plus
    prev_hash produces (i.e. the row itself, or a row before it, was tampered
    with or deleted).
    """
    rows = conn.execute(
        """
        SELECT retrieval_id, source, external_id, query_text, retrieved_at, agent_id,
               source_metadata, raw_response, snapshot, prev_hash, response_hash, hash_version
        FROM retrievals ORDER BY rowid ASC
        """
    ).fetchall()

    expected_prev = ""
    for row in rows:
        (retrieval_id, source, external_id, query_text, retrieved_at, agent_id,
         source_metadata, raw_response, snapshot, prev_hash, response_hash, version) = row
        if prev_hash != expected_prev:
            return False, retrieval_id
        try:
            recomputed = compute_hash(
                version, prev_hash, retrieval_id, source, external_id, query_text,
                retrieved_at, agent_id, source_metadata, raw_response, snapshot,
            )
        except ValueError:
            return False, retrieval_id
        if recomputed != response_hash:
            return False, retrieval_id
        expected_prev = response_hash

    return True, None


def make_anchor(conn: Any, key: bytes) -> dict:
    """A signed checkpoint of the chain head, to be stored OUTSIDE the database.

    verify_chain alone cannot see rows deleted from the tail or a wholesale rewrite
    that recomputes every hash. A checkpoint held elsewhere (a file, a ticket, a
    git commit) pins "the log had at least N rows and row N hashed to H".
    """
    row = conn.execute("SELECT count(*) FROM retrievals").fetchone()
    count = row[0]
    anchor = {
        "version": 1,
        "count": count,
        "head_hash": get_last_hash(conn),
        "created_at": datetime.now(UTC).isoformat(),
    }
    anchor["hmac"] = _anchor_mac(anchor, key)
    return anchor


def _anchor_mac(anchor: dict, key: bytes) -> str:
    body = json.dumps(
        [anchor["version"], anchor["count"], anchor["head_hash"], anchor["created_at"]],
        separators=(",", ":"),
    )
    return hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_anchor(conn: Any, anchor: dict, key: bytes) -> tuple[bool, str]:
    """Check the live log against a checkpoint. Returns (ok, human-readable reason)."""
    try:
        expected = _anchor_mac(anchor, key)
    except (KeyError, TypeError):
        return False, "malformed anchor"
    if not hmac.compare_digest(expected, str(anchor.get("hmac", ""))):
        return False, "anchor signature invalid (wrong key or edited anchor)"
    count = anchor["count"]
    if count == 0:
        return True, "anchor predates any records"
    rows = conn.execute(
        "SELECT response_hash FROM retrievals ORDER BY rowid ASC LIMIT 1 OFFSET ?", (count - 1,)
    ).fetchall()
    if not rows:
        total = conn.execute("SELECT count(*) FROM retrievals").fetchone()[0]
        return False, f"log truncated: anchor covers {count} records, log has {total}"
    if rows[0][0] != anchor["head_hash"]:
        return False, f"record {count} no longer matches the anchored hash (log rewritten)"
    return True, f"log extends the anchored head at record {count}"


def write_retrieval(
    conn: Any,
    query_text: str,
    external_id: str,
    agent_id: str,
    source: str,
    source_metadata: dict,
    raw_response: str,
    snapshot: dict,
) -> RetrievalRecord:
    """Write a retrieval record. Uses background writer for thread safety.

    response_hash/prev_hash are computed inside the serialized write path
    (_chain_and_insert), not here, so chaining stays correct under concurrent
    callers — see _chain_and_insert's docstring.
    """
    record = RetrievalRecord(
        retrieval_id=str(uuid.uuid4()),
        source=source,
        external_id=external_id,
        query_text=query_text,
        retrieved_at=datetime.now(UTC).isoformat(),
        agent_id=agent_id,
        source_metadata=json.dumps(source_metadata),
        raw_response=raw_response,
        snapshot=json.dumps(snapshot),
        response_hash="",
        prev_hash="",
    )

    # Check if writer is running for the database `conn` was connected to
    global _writer_thread, _writer_db_path
    target = db_module.current_target()

    if _writer_thread is not None and _writer_thread.is_alive() and _writer_db_path == target:
        # Use background writer
        result: Future[None] = Future()
        _write_queue.put((record, result))
        result.result()
    else:
        # Fallback to synchronous write (tests, or writer not started)
        _write_sync(conn, record)

    return record


def get_retrieval(conn: Any, retrieval_id: str) -> RetrievalRecord | None:
    """Retrieve a single retrieval record by ID."""
    row = conn.execute(
        """
        SELECT retrieval_id, source, external_id, query_text, retrieved_at,
               agent_id, source_metadata, raw_response, snapshot, response_hash, prev_hash,
               hash_version
        FROM retrievals WHERE retrieval_id = ?
        """,
        (retrieval_id,),
    ).fetchone()

    if row is None:
        return None

    return RetrievalRecord(
        retrieval_id=row[0],
        source=row[1],
        external_id=row[2],
        query_text=row[3],
        retrieved_at=row[4],
        agent_id=row[5],
        source_metadata=row[6],
        raw_response=row[7],
        snapshot=row[8],
        response_hash=row[9],
        prev_hash=row[10],
        hash_version=row[11],
    )
