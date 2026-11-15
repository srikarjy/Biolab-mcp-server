"""Offline acceptance tests for the A2A Agent Card and JSON-RPC adapter."""

from biolab import evidence
from biolab.web import create_app
from starlette.testclient import TestClient


def _fake_search(_conn, query, agent_id, sources, max_results):
    return {
        "query_echo": query,
        "agent_id": agent_id,
        "sources": {source: [] for source in sources},
        "errors": {},
        "retrieval_count": 0,
        "max_results": max_results,
    }


def test_agent_card_and_a2a_search(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence, "search_evidence", _fake_search)
    with TestClient(create_app(tmp_path / "a2a.db")) as client:
        card = client.get("/.well-known/agent-card.json")
        response = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "request-1",
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": "message-1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "Find evidence about KRAS"}],
                    },
                    "metadata": {"sources": ["uniprot"], "max_results": 2},
                },
            },
        )

        task = response.json()["result"]["task"]
        fetched = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0",
                "id": "request-2",
                "method": "tasks/get",
                "params": {"id": task["id"]},
            },
        )

    assert card.status_code == 200
    assert card.json()["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert card.json()["skills"][0]["id"] == "audited-evidence-search"
    assert client.get("/a2a").json()["methods"] == ["message/send", "tasks/get"]
    assert response.status_code == 200
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert task["artifacts"][0]["parts"][0]["data"]["query_echo"] == "Find evidence about KRAS"
    assert fetched.json()["result"]["id"] == task["id"]


def test_a2a_invalid_task_and_method(tmp_path):
    with TestClient(create_app(tmp_path / "a2a-errors.db")) as client:
        missing = client.post(
            "/a2a",
            json={
                "jsonrpc": "2.0", "id": 1, "method": "tasks/get", "params": {"id": "missing"}
            },
        )
        unknown = client.post(
            "/a2a",
            json={"jsonrpc": "2.0", "id": 2, "method": "unknown", "params": {}},
        )

    assert missing.json()["error"]["code"] == -32001
    assert unknown.json()["error"]["code"] == -32601
