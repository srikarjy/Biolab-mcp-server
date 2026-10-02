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
