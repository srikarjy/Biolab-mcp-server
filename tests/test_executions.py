"""ExecutionRecord: chain integrity, failure-closed behaviour, replay and artifact verification."""

import json
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
