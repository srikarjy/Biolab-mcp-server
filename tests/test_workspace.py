"""Offline tests for scientist projects, collections, and evidence watches."""

from biolab import db, retrieval_log, workspace


def test_project_collection_and_reviewed_item(tmp_path):
    conn = db.connect(str(tmp_path / "workspace.db"))
    project = workspace.create_project(conn, "KRAS validation", "Target evidence")
    collection = workspace.create_collection(conn, project["project_id"], "Core evidence")
    record = retrieval_log.write_retrieval(
        conn, "KRAS", "P01116", "test", "uniprot", {}, "raw", {"title": "KRAS"}
    )

    workspace.add_collection_item(
        conn, collection["collection_id"], record.retrieval_id, "Reviewed", "relevant"
    )
    fetched = workspace.get_collection(conn, collection["collection_id"])

    assert workspace.list_projects(conn)[0]["name"] == "KRAS validation"
    assert workspace.list_collections(conn, project["project_id"])[0]["collection_id"] == (
        collection["collection_id"]
    )
    assert fetched is not None
    assert fetched["items"][0]["external_id"] == "P01116"
    assert fetched["items"][0]["relevance"] == "relevant"
    conn.close()


def test_watch_records_added_and_removed_events(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "workspace.db"))
    project = workspace.create_project(conn, "Watch project")
    watch = workspace.create_watch(
        conn, project["project_id"], "pubmed", "KRAS", "watch:test", 5
    )
    result_sets = iter(
        [
            [
                {"external_id": "1", "retrieval_id": "r1", "response_hash": "h1"},
                {"external_id": "2", "retrieval_id": "r2", "response_hash": "h2"},
            ],
            [
                {"external_id": "2", "retrieval_id": "r3", "response_hash": "h2-revised"},
                {"external_id": "3", "retrieval_id": "r4", "response_hash": "h3"},
            ],
        ]
    )
    monkeypatch.setattr(workspace.evidence, "search_source", lambda *_args: next(result_sets))

    first = workspace.run_watch(conn, watch["watch_id"])
    second = workspace.run_watch(conn, watch["watch_id"])
    events = workspace.list_watch_events(conn, watch["watch_id"])

    assert {(event["event_type"], event["external_id"]) for event in first["events"]} == {
        ("added", "1"), ("added", "2")
    }
    assert {(event["event_type"], event["external_id"]) for event in second["events"]} == {
        ("added", "3"), ("removed", "1"), ("changed", "2")
    }
    assert len(events) == 5
    conn.close()


def test_run_active_watches_collects_results_and_errors(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "run-all.db"))
    project = workspace.create_project(conn, "Scheduled monitoring")
    first = workspace.create_watch(conn, project["project_id"], "pubmed", "KRAS", "test")
    second = workspace.create_watch(conn, project["project_id"], "uniprot", "TP53", "test")

    def fake_run(_conn, watch_id):
        if watch_id == second["watch_id"]:
            raise RuntimeError("source unavailable")
        return {"watch_id": watch_id, "events": []}

    monkeypatch.setattr(workspace, "run_watch", fake_run)
    result = workspace.run_active_watches(conn)

    assert result["completed"] == [{"watch_id": first["watch_id"], "events": []}]
    assert result["errors"] == [
        {"watch_id": second["watch_id"], "error": "source unavailable"}
    ]
    conn.close()


def test_watch_does_not_report_unchanged_content(monkeypatch, tmp_path):
    conn = db.connect(str(tmp_path / "unchanged.db"))
    project = workspace.create_project(conn, "Stable evidence")
    watch = workspace.create_watch(conn, project["project_id"], "pubmed", "KRAS", "test")
    calls = iter([
        [{"external_id": "1", "retrieval_id": "r1", "response_hash": "chain-1", "content_hash": "same"}],
        [{"external_id": "1", "retrieval_id": "r2", "response_hash": "chain-2", "content_hash": "same"}],
    ])
    monkeypatch.setattr(workspace.evidence, "search_source", lambda *_args: next(calls))

    workspace.run_watch(conn, watch["watch_id"])
    second = workspace.run_watch(conn, watch["watch_id"])

    assert second["events"] == []
    conn.close()


def test_claim_trace_keeps_stance_and_review_status_explicit(tmp_path):
    conn = db.connect(str(tmp_path / "claims.db"))
    project = workspace.create_project(conn, "Claim review")
    claim = workspace.create_claim(conn, project["project_id"], "KRAS is a validated target")
    supporting = retrieval_log.write_retrieval(
        conn, "KRAS", "1", "test", "pubmed", {}, "support", {"title": "Support"}
    )
    conflicting = retrieval_log.write_retrieval(
        conn, "KRAS", "2", "test", "pubmed", {}, "conflict", {"title": "Conflict"}
    )
    workspace.link_claim_evidence(
        conn, claim["claim_id"], supporting.retrieval_id, "supports", "human_reviewed"
    )
    workspace.link_claim_evidence(
        conn, claim["claim_id"], conflicting.retrieval_id, "conflicts", "agent_proposed"
    )

    trace = workspace.trace_claim(conn, claim["claim_id"])

    assert trace is not None
    assert {item["stance"] for item in trace["evidence"]} == {"supports", "conflicts"}
    assert {item["assessment"] for item in trace["evidence"]} == {
        "human_reviewed", "agent_proposed"
    }
    conn.close()


def test_project_report_contains_traceable_evidence(tmp_path):
    conn = db.connect(str(tmp_path / "report.db"))
    project = workspace.create_project(conn, "Evidence report", "A project report")
    collection = workspace.create_collection(conn, project["project_id"], "Key papers")
    record = retrieval_log.write_retrieval(
        conn, "query", "PMID1", "test", "pubmed", {}, "raw", {"title": "Paper title"}
    )
    workspace.add_collection_item(conn, collection["collection_id"], record.retrieval_id)

    report = workspace.get_project_report(conn, project["project_id"])
    markdown = workspace.render_project_markdown(report)

    assert report is not None
    assert report["collections"][0]["items"][0]["retrieval_id"] == record.retrieval_id
    assert "Paper title" in markdown
    assert record.retrieval_id in markdown
    assert "not clinical advice" in markdown
    conn.close()
