"""Offline tests for shared multi-source evidence orchestration."""

from dataclasses import dataclass

import pytest
from biolab import db, evidence


@dataclass
class FakeItem:
    external_id: str


def _retrieval_input(item: FakeItem) -> dict:
    return {
        "external_id": item.external_id,
        "source": "pubmed",
        "source_metadata": {"synthetic": True},
        "raw_response": f"raw:{item.external_id}",
        "snapshot": {"title": f"Evidence {item.external_id}"},
    }


def test_search_source_persists_before_returning(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "evidence.db"))
    monkeypatch.setattr(
        evidence.pubmed_client,
        "search_and_fetch",
        lambda query, limit: [FakeItem("1"), FakeItem("2")],
    )
    monkeypatch.setattr(evidence.pubmed_client, "paper_to_retrieval_input", _retrieval_input)

    results = evidence.search_source(conn, "pubmed", "query", "test:agent", 2)

    assert [result["external_id"] for result in results] == ["1", "2"]
    assert all(result["retrieval_id"] for result in results)
    assert conn.execute("SELECT count(*) FROM retrievals").fetchone()[0] == 2
    assert evidence.retrieval_log.verify_chain(conn) == (True, None)
    conn.close()


def test_search_evidence_reports_partial_failure(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "evidence.db"))

    def fake_search_source(conn, source, query, agent_id, max_results):
        if source == "europepmc":
            raise RuntimeError("source unavailable")
        return [{"source": source, "retrieval_id": source}]

    monkeypatch.setattr(evidence, "search_source", fake_search_source)
    result = evidence.search_evidence(
        conn,
        "query",
        "test:agent",
        ["pubmed", "europepmc", "pubmed"],
        1,
    )

    assert result["retrieval_count"] == 1
    assert list(result["sources"]) == ["pubmed"]
    assert result["errors"] == {"europepmc": "source unavailable"}
    conn.close()


@pytest.mark.parametrize(
    ("query", "agent_id", "max_results"),
    [("", "agent", 1), ("query", "", 1), ("query", "agent", 0), ("query", "agent", 51)],
)
def test_invalid_requests_fail_before_network(query, agent_id, max_results, tmp_path):
    conn = db.connect(str(tmp_path / "evidence.db"))
    with pytest.raises(ValueError):
        evidence.search_source(conn, "pubmed", query, agent_id, max_results)
    conn.close()


def test_unsupported_source_is_explicit(tmp_path):
    conn = db.connect(str(tmp_path / "evidence.db"))
    with pytest.raises(ValueError, match="unsupported source"):
        evidence.search_source(conn, "unknown", "query", "agent", 1)
    conn.close()
