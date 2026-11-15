import json
from datetime import UTC, datetime

import pytest
from biolab import db, ranking, retrieval_log


def _retrieval(conn, external_id, snapshot):
    return retrieval_log.write_retrieval(
        conn, query_text="KRAS randomized trial", external_id=external_id,
        agent_id="test", source="pubmed", source_metadata={},
        raw_response=json.dumps(snapshot), snapshot=snapshot,
    )


def test_score_exposes_components_and_reasons():
    result = ranking.score_snapshot(
        "KRAS randomized trial", {
            "title": "KRAS randomized controlled trial in pancreatic cancer",
            "abstract": "A randomized trial studied KRAS inhibition.",
            "publication_types": ["Randomized Controlled Trial"],
            "journal": {"pub_date": "2025"},
        }, external_id="123", as_of=datetime(2026, 11, 14, tzinfo=UTC),
    )
    assert set(result["score_components"]) == set(ranking.SCORE_WEIGHTS)
    assert result["score_total"] > 0.7
    assert "title_match" in result["reason_codes"]
    assert "study_design_randomized" in result["reason_codes"]


def test_missing_metadata_is_explicit():
    result = ranking.score_snapshot(
        "BRCA1", {"title": "BRCA1"}, external_id="1",
        as_of=datetime(2026, 11, 14, tzinfo=UTC),
    )
    assert "missing_abstract" in result["missing_facets"]
    assert "publication_date_unavailable" in result["reason_codes"]
    assert "entity_metadata_unavailable" in result["reason_codes"]


def test_source_entity_fields_are_explainable():
    result = ranking.score_snapshot(
        "KRAS", {"title": "GTPase", "gene_names": ["KRAS", "KRAS2"]},
        external_id="P01116", as_of=datetime(2026, 11, 14, tzinfo=UTC),
    )
    assert result["score_components"]["entity_fit"] == 1.0
    assert "entity_match" in result["reason_codes"]
    assert "kras" in result["matched_terms"]


def test_rank_retrievals_persists_top_five_and_is_deterministic(tmp_path):
    conn = db.connect(str(tmp_path / "ranking.db"))
    records = [
        _retrieval(conn, "low", {"title": "Unrelated paper", "abstract": "biology"}),
        _retrieval(conn, "high", {"title": "KRAS pancreatic cancer randomized trial", "abstract": "KRAS"}),
        _retrieval(conn, "medium", {"title": "KRAS cancer review", "abstract": "KRAS"}),
    ]
    ids = [record.retrieval_id for record in records]
    first = ranking.rank_retrievals(conn, "KRAS randomized trial", ids, created_by="scientist", as_of="2026-11-14T00:00:00+00:00")
    second = ranking.rank_retrievals(conn, "KRAS randomized trial", list(reversed(ids)), created_by="scientist", as_of="2026-11-14T00:00:00+00:00")
    assert [item["external_id"] for item in first["candidates"]] == ["high", "medium", "low"]
    assert [item["external_id"] for item in first["candidates"]] == [item["external_id"] for item in second["candidates"]]
    stored = ranking.get_ranking(conn, first["ranking_run_id"])
    assert stored["candidates"][0]["rank"] == 1
    assert stored["candidates"][0]["review_state"] == "unreviewed"


def test_rank_rejects_unknown_retrieval(tmp_path):
    conn = db.connect(str(tmp_path / "ranking.db"))
    with pytest.raises(ValueError, match="retrievals not found"):
        ranking.rank_retrievals(conn, "question", ["missing"], created_by="scientist")
