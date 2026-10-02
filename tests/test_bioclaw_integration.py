"""Offline tests for the distributable BioClaw REST client."""

import importlib.util
from pathlib import Path

SCRIPT = (
    Path(__file__).parent.parent
    / "integrations"
    / "bioclaw"
    / "biolab-evidence"
    / "scripts"
    / "biolab_client.py"
)


def _load_client():
    spec = importlib.util.spec_from_file_location("biolab_bioclaw_client", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_search_command_sends_expected_payload(monkeypatch, capsys):
    client = _load_client()
    captured = {}

    def fake_request(method, path, payload=None):
        captured.update(method=method, path=path, payload=payload)
        return 200, {"retrieval_count": 1, "errors": {}}

    monkeypatch.setattr(client, "request_json", fake_request)
    exit_code = client.main(
        ["search", "KRAS", "--sources", "pubmed,clinicaltrials", "--max-results", "2"]
    )

    assert exit_code == 0
    assert captured == {
        "method": "POST",
        "path": "/v1/evidence/search",
        "payload": {
            "query": "KRAS",
            "sources": ["pubmed", "clinicaltrials"],
            "max_results": 2,
            "agent_id": "bioclaw:research",
        },
    }
    assert '"retrieval_count": 1' in capsys.readouterr().out


def test_verify_returns_failure_for_broken_chain(monkeypatch):
    client = _load_client()
    monkeypatch.setattr(
        client,
        "request_json",
        lambda method, path, payload=None: (409, {"valid": False}),
    )
    assert client.main(["verify"]) == 1


def test_get_returns_failure_when_api_is_unreachable(monkeypatch):
    client = _load_client()
    monkeypatch.setattr(
        client,
        "request_json",
        lambda method, path, payload=None: (0, {"error": "unreachable"}),
    )
    assert client.main(["get", "missing"]) == 1
