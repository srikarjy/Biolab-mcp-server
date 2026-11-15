import pytest
from biolab import db, inference, retrieval_log


def _record(conn, external_id="PMID1"):
    return retrieval_log.write_retrieval(
        conn, "KRAS cancer", external_id, "scientist", "pubmed", {},
        "raw", {"title": "KRAS signal", "abstract": "KRAS is associated with pancreatic cancer."},
    )


def test_create_inference_preserves_citation_span_and_parser_chunks(tmp_path):
    conn = db.connect(str(tmp_path / "inference.db"))
    record = _record(conn)
    result = inference.create_inference(conn, "KRAS cancer", [record.retrieval_id], created_by="scientist", max_insights=1)
    assert result["insight_count"] == 1
    insight = result["insights"][0]
    assert insight["citations"][0]["retrieval_id"] == record.retrieval_id
    stored = inference.get_inference(conn, result["inference_run_id"])
    assert stored["claims"][0]["citations"][0]["excerpt"] == "KRAS is associated with pancreatic cancer."
    assert stored["parser_chunk_count"] == 2


def test_flush_only_removes_parser_chunks(tmp_path):
    conn = db.connect(str(tmp_path / "inference.db"))
    record = _record(conn)
    result = inference.create_inference(conn, "KRAS", [record.retrieval_id], created_by="scientist")
    flushed = inference.flush_parser_chunks(conn, result["inference_run_id"])
    assert flushed == {"inference_run_id": result["inference_run_id"], "flushed_parser_chunks": 2, "claims_preserved": True}
    stored = inference.get_inference(conn, result["inference_run_id"])
    assert stored["parser_chunk_count"] == 0
    assert stored["claims"][0]["citations"][0]["excerpt"] == "KRAS is associated with pancreatic cancer."


def test_inference_rejects_unknown_retrieval(tmp_path):
    conn = db.connect(str(tmp_path / "inference.db"))
    with pytest.raises(ValueError, match="retrievals not found"):
        inference.create_inference(conn, "KRAS", ["missing"], created_by="scientist")


def test_inference_can_use_request_context_evidence_when_storage_is_ephemeral(tmp_path):
    conn = db.connect(str(tmp_path / "fallback.db"))
    result = inference.create_inference(
        conn,
        "KRAS cancer",
        ["request-only"],
        created_by="web:local",
        evidence_records=[{
            "retrieval_id": "request-only", "source": "pubmed", "external_id": "1",
            "snapshot": {"title": "KRAS signal", "abstract": "KRAS is associated with cancer."},
        }],
    )
    assert result["durability"] == "request-context"
    assert result["insight_count"] == 2
    stored = inference.get_inference(conn, result["inference_run_id"])
    assert stored["claims"][0]["citations"][0]["excerpt"] == "KRAS is associated with cancer."
