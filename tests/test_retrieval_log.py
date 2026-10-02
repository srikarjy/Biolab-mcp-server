"""Tests for the one module that writes to the retrieval log.

Uses a real temporary SQLite file per test (via pytest's tmp_path), not an in-memory
mock — sqlite3 is the real production engine here, so there's nothing to gain by
faking it.
"""

import json
import threading

from biolab import db, retrieval_log


def test_write_retrieval_persists_a_real_row(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    record = retrieval_log.write_retrieval(
        conn,
        query_text="BRCA1 pancreatic cancer",
        external_id="42431391",
        agent_id="aletheia:advocate",
        source="pubmed",
        source_metadata={"medline_status": "MEDLINE", "pub_status": "ppublish"},
        raw_response="<PubmedArticle/>",
        snapshot={"title": "Test", "abstract": "Test abstract"},
    )

    row = conn.execute(
        "SELECT query_text, external_id, agent_id, source, source_metadata, raw_response, snapshot "
        "FROM retrievals WHERE retrieval_id = ?",
        (record.retrieval_id,),
    ).fetchone()

    assert row[0] == "BRCA1 pancreatic cancer"
    assert row[1] == "42431391"
    assert row[2] == "aletheia:advocate"
    assert row[3] == "pubmed"
    assert json.loads(row[4]) == {"medline_status": "MEDLINE", "pub_status": "ppublish"}
    assert row[5] == "<PubmedArticle/>"
    assert json.loads(row[6]) == {"title": "Test", "abstract": "Test abstract"}


def test_write_retrieval_generates_a_unique_id_per_call(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    first = retrieval_log.write_retrieval(
        conn, "q", "1", "a", "pubmed", {}, "<x/>", {}
    )
    second = retrieval_log.write_retrieval(
        conn, "q", "2", "a", "pubmed", {}, "<x/>", {}
    )

    assert first.retrieval_id != second.retrieval_id
    count = conn.execute("SELECT count(*) FROM retrievals").fetchone()[0]
    assert count == 2


def test_write_retrieval_sets_a_retrieved_at_timestamp(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    record = retrieval_log.write_retrieval(
        conn, "q", "1", "a", "pubmed", {}, "<x/>", {}
    )
    assert record.retrieved_at


def test_get_retrieval_returns_record(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    written = retrieval_log.write_retrieval(
        conn,
        query_text="test query",
        external_id="12345",
        agent_id="test:agent",
        source="pubmed",
        source_metadata={"key": "value"},
        raw_response="<xml/>",
        snapshot={"title": "Test Paper"},
    )

    retrieved = retrieval_log.get_retrieval(conn, written.retrieval_id)
    assert retrieved is not None
    assert retrieved.retrieval_id == written.retrieval_id
    assert retrieved.query_text == "test query"
    assert retrieved.external_id == "12345"
    assert retrieved.agent_id == "test:agent"
    assert retrieved.source == "pubmed"
    assert json.loads(retrieved.source_metadata) == {"key": "value"}
    assert retrieved.raw_response == "<xml/>"
    assert json.loads(retrieved.snapshot) == {"title": "Test Paper"}


def test_get_retrieval_returns_none_for_missing(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    result = retrieval_log.get_retrieval(conn, "non-existent-id")
    assert result is None


def test_verify_chain_passes_for_untampered_rows(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    for i in range(5):
        retrieval_log.write_retrieval(
            conn, f"q{i}", str(i), "a", "pubmed", {}, f"<x id='{i}'/>", {}
        )

    ok, break_at = retrieval_log.verify_chain(conn)
    assert ok is True
    assert break_at is None


def test_verify_chain_links_each_row_to_the_previous_hash(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    first = retrieval_log.write_retrieval(conn, "q0", "0", "a", "pubmed", {}, "<x/>", {})
    second = retrieval_log.write_retrieval(conn, "q1", "1", "a", "pubmed", {}, "<y/>", {})

    assert first.prev_hash == ""
    assert second.prev_hash == first.response_hash
    assert second.response_hash != first.response_hash


def test_verify_chain_detects_a_tampered_row(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    for i in range(4):
        retrieval_log.write_retrieval(
            conn, f"q{i}", str(i), "a", "pubmed", {}, f"<x id='{i}'/>", {}
        )

    rows = conn.execute(
        "SELECT retrieval_id FROM retrievals ORDER BY rowid ASC"
    ).fetchall()
    tampered_id = rows[2][0]

    conn.execute(
        "UPDATE retrievals SET raw_response = ? WHERE retrieval_id = ?",
        ("<tampered/>", tampered_id),
    )
    conn.commit()

    ok, break_at = retrieval_log.verify_chain(conn)
    assert ok is False
    assert break_at == tampered_id


def test_concurrent_writes_through_the_background_writer_land_intact(tmp_path):
    """AD-9: a single background writer serializes concurrent callers.

    Fire writes from many threads at once and confirm every row lands with a
    unique retrieval_id and an unbroken hash chain — no interleaved/corrupted
    commits, no lost writes.
    """
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)
    retrieval_log.start_writer(db_path)
    try:
        n_threads = 20
        results: list[object] = [None] * n_threads
        errors: list[Exception] = []

        def do_write(i: int) -> None:
            try:
                results[i] = retrieval_log.write_retrieval(
                    conn, f"q{i}", str(i), "a", "pubmed", {}, f"<x id='{i}'/>", {}
                )
            except Exception as e:  # noqa: BLE001 - surfaced via `errors` below
                errors.append(e)

        threads = [threading.Thread(target=do_write, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert all(r is not None for r in results)

        count = conn.execute("SELECT count(*) FROM retrievals").fetchone()[0]
        assert count == n_threads

        retrieval_ids = {r.retrieval_id for r in results}
        assert len(retrieval_ids) == n_threads  # no collisions/lost writes

        ok, break_at = retrieval_log.verify_chain(conn)
        assert ok is True, f"chain broke at {break_at}"
    finally:
        retrieval_log.stop_writer()


def test_forced_db_error_mid_write_raises_loudly(tmp_path):
    """AD-8: a DB failure during write must propagate, never fail silently."""
    db_path = str(tmp_path / "test.db")
    conn = db.connect(db_path)

    class _FailsOnCommit:
        def __init__(self, real_conn):
            self._conn = real_conn

        def commit(self):
            raise RuntimeError("simulated DB failure")

        def __getattr__(self, name):
            return getattr(self._conn, name)

    failing_conn = _FailsOnCommit(conn)

    raised = False
    try:
        retrieval_log.write_retrieval(failing_conn, "q", "1", "a", "pubmed", {}, "<x/>", {})
    except RuntimeError:
        raised = True

    assert raised is True
    count = conn.execute("SELECT count(*) FROM retrievals").fetchone()[0]
    assert count == 0

def test_background_failure_propagates_and_writer_recovers(tmp_path):
    """A real SQLite trigger rejects an insert; the caller must receive the error."""
    import pytest

    path = str(tmp_path / "failure.db")
    conn = db.connect(path)
    conn.execute("""CREATE TRIGGER reject_bad BEFORE INSERT ON retrievals
                    WHEN NEW.external_id = 'bad'
                    BEGIN SELECT RAISE(ABORT, 'rejected test record'); END""")
    conn.commit()
    retrieval_log.start_writer(path)
    try:
        with pytest.raises(Exception, match="rejected test record"):
            retrieval_log.write_retrieval(conn, "q", "bad", "a", "pubmed", {}, "x", {})
        good = retrieval_log.write_retrieval(conn, "q", "good", "a", "pubmed", {}, "y", {})
        assert retrieval_log.get_retrieval(conn, good.retrieval_id) is not None
        assert conn.execute("SELECT count(*) FROM retrievals").fetchone()[0] == 1
        assert retrieval_log.verify_chain(conn) == (True, None)
    finally:
        retrieval_log.stop_writer()
        conn.close()


def test_writer_can_stop_repeatedly_and_restart(tmp_path):
    path = str(tmp_path / "restart.db")
    conn = db.connect(path)
    for _ in range(3):
        retrieval_log.stop_writer()
        retrieval_log.stop_writer()
        retrieval_log.start_writer(path)
        record = retrieval_log.write_retrieval(conn, "q", "same", "a", "pubmed", {}, "x", {})
        assert retrieval_log.get_retrieval(conn, record.retrieval_id) is not None
        retrieval_log.stop_writer()
    assert conn.execute("SELECT count(*) FROM retrievals").fetchone()[0] == 3
    assert retrieval_log.verify_chain(conn) == (True, None)
    conn.close()
