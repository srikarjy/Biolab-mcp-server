"""Offline acceptance tests for the portfolio demo and audit commands."""

import json

from biolab import db, retrieval_log
from biolab.cli import app
from biolab.demo import run_demo
from typer.testing import CliRunner

runner = CliRunner()


def test_demo_traces_evidence_and_detects_corruption():
    result = run_demo()
    assert result["records_written"] == 2
    assert result["evidence_reference"]["retrieval_id"] == result["retrieved_record"]["retrieval_id"]
    assert result["chain_valid_before_edit"] is True
    assert result["tampering_detected"] is True


def test_portfolio_demo_command():
    result = runner.invoke(app, ["portfolio-demo"])
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    assert "Synthetic" in result.output


def test_verify_and_export_preserve_auditable_payloads(tmp_path):
    path = str(tmp_path / "audit.db")
    conn = db.connect(path)
    record = retrieval_log.write_retrieval(conn, "q", "1", "a", "pubmed", {}, "<x/>", {})
    result = runner.invoke(app, ["verify", "--db", path])
    assert result.exit_code == 0, result.output
    output = tmp_path / "evidence.jsonl"
    result = runner.invoke(app, ["export", str(output), "--db", path])
    assert result.exit_code == 0, result.output
    exported = json.loads(output.read_text())
    assert exported["raw_response"] == record.raw_response
    assert exported["prev_hash"] == record.prev_hash
    conn.execute("UPDATE retrievals SET raw_response = 'changed'")
    conn.commit()
    result = runner.invoke(app, ["verify", "--db", path])
    assert result.exit_code == 1
    assert record.retrieval_id in result.output
    conn.close()


def test_empty_queries_are_rejected_without_a_traceback(tmp_path):
    db_path = str(tmp_path / "t.db")
    for args in (["search", " "], ["search-europepmc", ""], ["search-clinicaltrials", ""],
                 ["search-biorxiv", ""]):
        result = runner.invoke(app, [*args, "--db", db_path])
        assert result.exit_code == 1 and "must not be empty" in result.output, args


def test_evidence_with_only_invalid_sources_exits_nonzero(tmp_path):
    result = runner.invoke(app, ["evidence", "q", "--sources", "bogus", "--db", str(tmp_path / "t.db")])
    assert result.exit_code == 1


def test_keys_create_rejects_blank_label_and_revoke_unknown_fails(tmp_path):
    db_path = str(tmp_path / "t.db")
    assert runner.invoke(app, ["keys", "create", " ", "--db", db_path]).exit_code == 1
    assert runner.invoke(app, ["keys", "revoke", "ghost", "--db", db_path]).exit_code == 1
    assert runner.invoke(app, ["keys", "create", "alice", "--db", db_path]).exit_code == 0
    assert runner.invoke(app, ["keys", "revoke", "alice", "--db", db_path]).exit_code == 0


def test_web_refuses_open_public_bind_unless_explicit(tmp_path, monkeypatch):
    monkeypatch.delenv("BIOLAB_ALLOW_ANONYMOUS", raising=False)
    monkeypatch.delenv("BIOLAB_REQUIRE_AUTH", raising=False)
    result = runner.invoke(app, ["web", "--host", "0.0.0.0", "--db", str(tmp_path / "t.db")])
    assert result.exit_code == 1 and "Refusing to listen" in result.output


def test_execution_commands_end_to_end_including_tamper(tmp_path, monkeypatch):
    db_path, art = str(tmp_path / "t.db"), str(tmp_path / "art")
    monkeypatch.setenv("BIOLAB_ARTIFACT_DIR", art)
    ran = runner.invoke(app, ["run-tool", "sequence_stats", "-i", '{"sequence":"ATGC"}',
                              "--db", db_path])
    assert ran.exit_code == 0, ran.output
    record = json.loads(ran.output)
    sha = record["outputs"][0]["sha256"]
    for args in (["executions", "list"], ["executions", "show", record["execution_id"]],
                 ["executions", "verify"], ["executions", "replay", record["execution_id"]]):
        result = runner.invoke(app, [*args, "--db", db_path])
        assert result.exit_code == 0, (args, result.output)
    assert runner.invoke(app, ["executions", "artifact", sha]).exit_code == 0
    assert runner.invoke(app, ["executions", "artifact", "../x"]).exit_code == 1

    (tmp_path / "art" / sha[:2] / sha).write_bytes(b"{}")
    assert runner.invoke(app, ["executions", "verify", "--db", db_path]).exit_code == 1
    assert runner.invoke(app, ["executions", "artifact", sha]).exit_code == 1
    assert runner.invoke(app, ["run-tool", "sequence_stats", "-i", "[1]", "--db", db_path]).exit_code == 1
    assert runner.invoke(app, ["run-tool", "sequence_stats", "-i", "bad", "--db", db_path]).exit_code == 1


def test_execution_anchor_cli_detects_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOLAB_ARTIFACT_DIR", str(tmp_path / "art"))
    monkeypatch.setenv("BIOLAB_ANCHOR_KEY", "k")
    db_path = str(tmp_path / "t.db")
    for seq in ("ATGC", "GGCC"):
        r = runner.invoke(app, ["run-tool", "sequence_stats", "-i", f'{{"sequence":"{seq}"}}', "--db", db_path])
        assert r.exit_code == 0, r.output
    anchor_file = str(tmp_path / "exec.anchor")
    assert runner.invoke(app, ["anchor", anchor_file, "--chain", "executions", "--db", db_path]).exit_code == 0
    assert runner.invoke(app, ["verify-anchor", anchor_file, "--db", db_path]).exit_code == 0
    assert runner.invoke(app, ["anchor", anchor_file, "--chain", "bogus", "--db", db_path]).exit_code == 2
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM executions WHERE rowid = (SELECT max(rowid) FROM executions)")
    conn.commit()
    conn.close()
    assert runner.invoke(app, ["executions", "verify", "--db", db_path]).exit_code == 0
    result = runner.invoke(app, ["verify-anchor", anchor_file, "--db", db_path])
    assert result.exit_code == 1 and "truncated" in result.output
