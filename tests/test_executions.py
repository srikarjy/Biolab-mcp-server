"""ExecutionRecord: chain integrity, failure-closed behaviour, replay and artifact verification."""

import json
import shutil
import sqlite3
import threading

import pytest
from biolab import db, executions
from biolab.artifacts import ArtifactStore
from biolab.executions import ToolRegistry, ToolSpec


def _reverse(inputs: dict) -> dict[str, bytes]:
    return {"out.txt": inputs["text"][::-1].encode()}


@pytest.fixture
def env(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    store = ArtifactStore(tmp_path / "artifacts")
    registry = ToolRegistry()
    registry.register(ToolSpec("reverse", "1.0", _reverse))
    return conn, store, registry


def test_successful_execution_is_recorded_with_hashed_outputs(env):
    conn, store, registry = env
    rec = executions.execute(
        conn, store, registry, "reverse", {"text": "abc"}, "agent:a", retrieval_ids=["r2", "r1"]
    )
    assert rec["status"] == "succeeded"
    assert rec["retrieval_ids"] == ["r1", "r2"]
    assert store.get(rec["outputs"][0]["sha256"]) == b"cba"
    assert executions.get_execution(conn, rec["execution_id"]) == rec
    assert executions.verify_executions(conn, store) == (True, None)


def test_failed_tool_is_recorded_then_reraised(env):
    conn, store, registry = env
    registry.register(ToolSpec("boom", "1", lambda i: (_ for _ in ()).throw(RuntimeError("no gpu"))))
    with pytest.raises(RuntimeError, match="no gpu"):
        executions.execute(conn, store, registry, "boom", {}, "agent:a")
    (rec,) = executions.list_executions(conn, tool="boom")
    assert rec["status"] == "failed" and "no gpu" in rec["error"] and rec["outputs"] == []
    assert executions.verify_executions(conn, store) == (True, None)


def test_unsealable_record_means_no_result_is_returned(env):
    conn, store, registry = env
    conn.execute("DROP TABLE executions")
    conn.commit()
    with pytest.raises(sqlite3.OperationalError):
        executions.execute(conn, store, registry, "reverse", {"text": "x"}, "agent:a")


def test_artifact_write_failure_means_no_record_and_no_result(env, monkeypatch):
    conn, store, registry = env

    def fail(_data):
        raise OSError("disk full")

    monkeypatch.setattr(store, "put", fail)
    with pytest.raises(OSError):
        executions.execute(conn, store, registry, "reverse", {"text": "x"}, "agent:a")
    # the attempt IS recorded as failed (auditable), but with no outputs and an error
    (rec,) = executions.list_executions(conn)
    assert rec["status"] == "failed" and "disk full" in rec["error"]


@pytest.mark.parametrize(
    "column,value",
    [("agent_id", "evil"), ("tool_version", "9"), ("status", "failed"), ("device", "cuda:7"),
     ("outputs", "[]"), ("retrieval_ids", '["x"]'), ("container", "other"), ("error", "x")],
)
def test_every_audited_column_is_tamper_evident(env, column, value):
    conn, store, registry = env
    recs = [
        executions.execute(conn, store, registry, "reverse", {"text": str(i)}, "agent:a")
        for i in range(3)
    ]
    conn.execute(
        f"UPDATE executions SET {column} = ? WHERE execution_id = ?",
        (value, recs[1]["execution_id"]),
    )
    conn.commit()
    assert executions.verify_executions(conn) == (False, recs[1]["execution_id"])


def test_swapped_inputs_are_detected_via_input_hash(env):
    conn, store, registry = env
    rec = executions.execute(conn, store, registry, "reverse", {"text": "abc"}, "agent:a")
    conn.execute(
        "UPDATE executions SET inputs = ? WHERE execution_id = ?",
        (json.dumps({"text": "forged"}), rec["execution_id"]),
    )
    conn.commit()
    assert executions.verify_executions(conn) == (False, rec["execution_id"])


def test_corrupted_or_deleted_artifact_fails_verification(env):
    conn, store, registry = env
    rec = executions.execute(conn, store, registry, "reverse", {"text": "abc"}, "agent:a")
    digest = rec["outputs"][0]["sha256"]
    store._path(digest).write_bytes(b"tampered")
    assert executions.verify_executions(conn, store) == (False, rec["execution_id"])
    assert executions.verify_executions(conn) == (True, None)  # chain alone can't see it
    store._path(digest).unlink()
    assert executions.verify_executions(conn, store) == (False, rec["execution_id"])


def test_replay_reproduces_a_deterministic_tool(env):
    conn, store, registry = env
    rec = executions.execute(conn, store, registry, "reverse", {"text": "abc"}, "agent:a")
    result = executions.replay(conn, store, registry, rec["execution_id"])
    assert result["reproduced"] is True and result["diff"]["matched"] == ["out.txt"]


def test_replay_reports_drift_and_version_change(env):
    conn, store, _ = env
    first = ToolRegistry()
    first.register(ToolSpec("t", "1.0", lambda i: {"o": b"v1"}))
    rec = executions.execute(conn, store, first, "t", {}, "agent:a")

    second = ToolRegistry()
    second.register(ToolSpec("t", "2.0", lambda i: {"o": b"v2"}))
    result = executions.replay(conn, store, second, rec["execution_id"])
    assert result["reproduced"] is False
    assert result["diff"]["drifted"] == ["o"]
    assert result["tool_version_matches"] is False


def test_concurrent_executions_keep_a_single_valid_chain(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    store = ArtifactStore(tmp_path / "a")
    registry = ToolRegistry()
    registry.register(ToolSpec("reverse", "1.0", _reverse))
    errors: list[Exception] = []

    def work(n: int) -> None:
        try:
            for i in range(5):
                executions.execute(conn, store, registry, "reverse", {"text": f"{n}-{i}"}, "a")
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    assert len(executions.list_executions(conn, limit=500)) == 40
    assert executions.verify_executions(conn, store) == (True, None)


def test_artifact_store_is_content_addressed_and_rejects_bad_ids(tmp_path):
    store = ArtifactStore(tmp_path)
    assert store.put(b"same") == store.put(b"same")
    with pytest.raises(ValueError):
        store.get("../../etc/passwd")


def test_sequence_stats_tool_end_to_end_and_replays(env, tmp_path):
    from biolab import tools

    conn, store, _ = env
    registry = tools.default_registry()
    rec = executions.execute(
        conn, store, registry, "sequence_stats", {"sequence": "ATGCGC"}, "agent:a"
    )
    report = json.loads(store.get(rec["outputs"][0]["sha256"]))
    assert report == {"composition": {"A": 1, "C": 2, "G": 2, "T": 1}, "gc_fraction": 0.666667,
                      "length": 6, "type": "nucleotide"}
    assert executions.replay(conn, store, registry, rec["execution_id"])["reproduced"] is True

    with pytest.raises(ValueError):
        executions.execute(conn, store, registry, "sequence_stats", {"sequence": "ZZ!!"}, "a")


def test_20_concurrent_executions_with_20_injected_faults_keep_one_valid_chain(tmp_path):
    conn = db.connect(str(tmp_path / "t.db"))
    store = ArtifactStore(tmp_path / "a")
    registry = ToolRegistry()

    def flaky(inputs: dict) -> dict[str, bytes]:
        if inputs["n"] % 2:
            raise RuntimeError("injected fault")
        return {"o": str(inputs["n"]).encode()}

    registry.register(ToolSpec("flaky", "1", flaky))
    raised: list[int] = []
    lock = threading.Lock()

    def work(n: int) -> None:
        try:
            executions.execute(conn, store, registry, "flaky", {"n": n}, "a")
        except RuntimeError:
            with lock:
                raised.append(n)

    threads = [threading.Thread(target=work, args=(n,)) for n in range(40)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    rows = executions.list_executions(conn, limit=500)
    assert len(raised) == 20 and len(rows) == 40  # every fault surfaced AND was recorded
    assert sum(r["status"] == "failed" for r in rows) == 20
    assert executions.verify_executions(conn, store) == (True, None)


KEY = b"anchor-test-key"


def _seed(env, n=3):
    conn, store, registry = env
    for i in range(n):
        executions.execute(conn, store, registry, "reverse", {"text": f"abc{i}"}, "agent")
    return conn


def test_execution_anchor_detects_truncation(env):
    conn = _seed(env)
    anchor = executions.make_anchor(conn, KEY)
    assert executions.verify_anchor(conn, anchor, KEY)[0]
    conn.execute("DELETE FROM executions WHERE rowid = (SELECT max(rowid) FROM executions)")
    conn.commit()
    assert executions.verify_executions(conn)[0]  # chain alone cannot see tail truncation
    ok, reason = executions.verify_anchor(conn, anchor, KEY)
    assert not ok and "truncated" in reason


def test_execution_anchor_detects_rewrite_and_forgery(env):
    conn = _seed(env)
    anchor = executions.make_anchor(conn, KEY)
    # Attacker rewrites record 2 and recomputes every hash after it.
    rows = conn.execute(f"SELECT {', '.join(executions._COLUMNS)} FROM executions ORDER BY rowid").fetchall()
    prev = rows[0][executions._COLUMNS.index("record_hash")]
    for values in rows[1:]:
        row = dict(zip(executions._COLUMNS, values, strict=True))
        row["agent_id"] = "mallory"
        row["prev_hash"] = prev
        row["record_hash"] = executions._record_hash(executions.HASH_VERSION, row)
        conn.execute(
            "UPDATE executions SET agent_id=?, prev_hash=?, record_hash=? WHERE execution_id=?",
            (row["agent_id"], row["prev_hash"], row["record_hash"], row["execution_id"]),
        )
        prev = row["record_hash"]
    conn.commit()
    assert executions.verify_executions(conn)[0]
    ok, reason = executions.verify_anchor(conn, anchor, KEY)
    assert not ok and "rewritten" in reason
    forged = dict(anchor, head_hash="0" * 64)
    assert "signature" in executions.verify_anchor(conn, forged, KEY)[1]
    assert not executions.verify_anchor(conn, anchor, b"wrong-key")[0]


def test_retrieval_anchor_is_not_accepted_as_execution_anchor(env):
    from biolab import retrieval_log

    conn = _seed(env)
    assert not executions.verify_anchor(conn, retrieval_log.make_anchor(conn, KEY), KEY)[0]


@pytest.mark.skipif(shutil.which("mmseqs") is None, reason="mmseqs not installed")
def test_mmseqs2_search_runs_records_and_replays(tmp_path):
    from biolab import tools

    conn = db.connect(str(tmp_path / "m.db"))
    store = ArtifactStore(tmp_path / "artifacts")
    registry = tools.default_registry()
    protein = "MKTAYIAKQRQISFVKSHFSRQLEERLGLIEVQAPILSRVGDGTQDNLSGAEKAVQVKVKALPDAQFEVV"
    inputs = {
        "query_fasta": f">q1\n{protein}\n",
        "target_fasta": f">t1\n{protein}\n>t2\n{protein[::-1]}\n",
    }
    record = executions.execute(conn, store, registry, "mmseqs2_search", inputs, "agent")
    assert record["status"] == "succeeded"
    hits = store.get(record["outputs"][0]["sha256"]).decode()
    assert "q1\tt1" in hits
    assert executions.verify_executions(conn, store)[0]
    result = executions.replay(conn, store, registry, record["execution_id"])
    assert result["reproduced"], result
