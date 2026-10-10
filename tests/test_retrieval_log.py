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


import hashlib  # noqa: E402
import sqlite3  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.parametrize(
    "column,value",
    [
        ("source", "europepmc"),
        ("external_id", "999"),
        ("query_text", "a different question"),
        ("agent_id", "someone:else"),
        ("source_metadata", '{"forged": true}'),
        ("snapshot", '{"title": "forged"}'),
        ("retrieved_at", "1999-01-01T00:00:00+00:00"),
    ],
)
def test_verify_chain_detects_tampering_with_every_audited_column(tmp_path, column, value):
    conn = db.connect(str(tmp_path / "test.db"))
    records = [
        retrieval_log.write_retrieval(conn, f"q{i}", str(i), "a", "pubmed", {}, "<x/>", {})
        for i in range(3)
    ]
    target = records[1].retrieval_id

    conn.execute(f"UPDATE retrievals SET {column} = ? WHERE retrieval_id = ?", (value, target))
    conn.commit()

    assert retrieval_log.verify_chain(conn) == (False, target)


def test_legacy_v1_rows_still_verify_and_new_rows_chain_onto_them(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    raw = sqlite3.connect(db_path)
    raw.execute(
        """CREATE TABLE retrievals (
            retrieval_id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
            query_text TEXT NOT NULL, retrieved_at TEXT NOT NULL, agent_id TEXT NOT NULL,
            source_metadata TEXT NOT NULL, raw_response TEXT NOT NULL, snapshot TEXT NOT NULL,
            response_hash TEXT NOT NULL, prev_hash TEXT NOT NULL DEFAULT '')"""
    )
    legacy_hash = hashlib.sha256(("" + "<old/>" + "r-old" + "2026-01-01T00:00:00+00:00").encode()).hexdigest()
    raw.execute(
        "INSERT INTO retrievals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("r-old", "pubmed", "1", "q", "2026-01-01T00:00:00+00:00", "a", "{}", "<old/>", "{}",
         legacy_hash, ""),
    )
    raw.commit()
    raw.close()

    conn = db.connect(db_path)
    assert retrieval_log.verify_chain(conn) == (True, None)

    new = retrieval_log.write_retrieval(conn, "q2", "2", "a", "pubmed", {}, "<new/>", {})
    assert new.prev_hash == legacy_hash
    assert retrieval_log.verify_chain(conn) == (True, None)

    conn.execute("UPDATE retrievals SET query_text = 'forged' WHERE retrieval_id = ?", (new.retrieval_id,))
    conn.commit()
    assert retrieval_log.verify_chain(conn) == (False, new.retrieval_id)


def test_anchor_detects_tail_truncation_and_full_rewrite(tmp_path):
    key = b"k"
    conn = db.connect(str(tmp_path / "test.db"))
    for i in range(5):
        retrieval_log.write_retrieval(conn, f"q{i}", str(i), "a", "pubmed", {}, f"<{i}/>", {})
    anchor = retrieval_log.make_anchor(conn, key)
    assert retrieval_log.verify_anchor(conn, anchor, key)[0] is True

    # growth is fine
    retrieval_log.write_retrieval(conn, "q5", "5", "a", "pubmed", {}, "<5/>", {})
    assert retrieval_log.verify_anchor(conn, anchor, key)[0] is True

    # the chain itself stays valid after deleting the newest rows, only the anchor notices
    conn.execute("DELETE FROM retrievals WHERE rowid > 3")
    conn.commit()
    assert retrieval_log.verify_chain(conn) == (True, None)
    ok, reason = retrieval_log.verify_anchor(conn, anchor, key)
    assert ok is False and "truncated" in reason

    # a forged anchor or wrong key is rejected
    assert retrieval_log.verify_anchor(conn, anchor, b"other")[0] is False
    assert retrieval_log.verify_anchor(conn, {**anchor, "count": 1}, key)[0] is False


def test_anchor_detects_a_rewritten_log_with_recomputed_hashes(tmp_path):
    key = b"k"
    conn = db.connect(str(tmp_path / "test.db"))
    for i in range(3):
        retrieval_log.write_retrieval(conn, f"q{i}", str(i), "a", "pubmed", {}, f"<{i}/>", {})
    anchor = retrieval_log.make_anchor(conn, key)

    conn.execute("DELETE FROM retrievals")
    conn.commit()
    for i in range(3):  # attacker replays different content through the legitimate writer
        retrieval_log.write_retrieval(conn, f"forged{i}", str(i), "a", "pubmed", {}, f"<f{i}/>", {})
    assert retrieval_log.verify_chain(conn) == (True, None)
    assert retrieval_log.verify_anchor(conn, anchor, key)[0] is False


def test_20_concurrent_writers_with_20_injected_faults_lose_nothing(tmp_path):
    """40 threads race through the background writer; every other one hits a DB-level reject.

    Exactly the 20 good writes must land and return, exactly the 20 faulty ones must raise,
    and the chain must stay valid with no orphan or missing rows.
    """
    path = str(tmp_path / "faults.db")
    conn = db.connect(path)
    conn.execute("""CREATE TRIGGER reject_bad BEFORE INSERT ON retrievals
                    WHEN NEW.external_id LIKE 'bad-%'
                    BEGIN SELECT RAISE(ABORT, 'injected fault'); END""")
    conn.commit()
    retrieval_log.start_writer(path)
    good: dict[int, object] = {}
    failures: list[int] = []
    unexpected: list[Exception] = []
    lock = threading.Lock()

    def work(i: int) -> None:
        ext = f"bad-{i}" if i % 2 else f"ok-{i}"
        try:
            record = retrieval_log.write_retrieval(conn, "q", ext, "a", "pubmed", {}, f"<{i}/>", {})
            with lock:
                good[i] = record
        except Exception as exc:  # noqa: BLE001
            with lock:
                (failures if "injected fault" in str(exc) else unexpected).append(i)

    try:
        threads = [threading.Thread(target=work, args=(i,)) for i in range(40)]
        [t.start() for t in threads]
        [t.join() for t in threads]
    finally:
        retrieval_log.stop_writer()

    assert unexpected == []
    assert len(good) == 20 and len(failures) == 20
    stored = {r[0] for r in conn.execute("SELECT retrieval_id FROM retrievals").fetchall()}
    assert stored == {r.retrieval_id for r in good.values()}  # no lost, no orphaned rows
    assert retrieval_log.verify_chain(conn) == (True, None)
    conn.close()


def test_hash_vectors_match_go_port():
    """Pinned against go-biolab/internal/retrieval/chain_test.go; both must change together."""
    args = {
        "prev_hash": "abc", "retrieval_id": "id-1", "source": "pubmed", "external_id": "123",
        "query_text": 'BRCA1 "mut" <ß> \u00e9\u4e2d 😀 \\ \n\t\x01\x7f',
        "retrieved_at": "2026-01-01T00:00:00+00:00", "agent_id": "a&b",
        "source_metadata": '{"k": "v"}', "raw_response": "<xml>é</xml>", "snapshot": '{"t":"😀"}',
    }
    assert retrieval_log.compute_hash(2, **args) == (
        "90125ef4e394665706600b57ca70dd63192111b5da64520a44db33f1be027a24"
    )
    assert retrieval_log.compute_hash(1, **args) == (
        "fc3f558ddde4376baba8f9774c9e2ec8b8b2f24ba39d7fcb0e7067b6f725d7cc"
    )
    anchor = {"version": 1, "count": 3, "head_hash": "hé", "created_at": "2026-01-01T00:00:00.000000+00:00"}
    assert retrieval_log._anchor_mac(anchor, b"k") == (
        "2ff6a1ddb1b3954331f77c63cfdf37bd666c01d8bc20c40ac2e79c617821db0c"
    )
