"""Regression tests for the shared REST/MCP HTTP quota boundary."""

from biolab.web import create_app
from starlette.testclient import TestClient


def test_rate_limit_returns_429_and_retry_after(monkeypatch, tmp_path):
    monkeypatch.setenv("BIOLAB_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("BIOLAB_RATE_LIMIT_WINDOW_SECONDS", "60")
    with TestClient(create_app(tmp_path / "limit.db", require_auth=False)) as client:
        first = client.get("/v1/storage")
        second = client.get("/v1/storage")

    assert first.status_code == 200
    assert first.headers["x-ratelimit-limit"] == "1"
    assert first.headers["x-ratelimit-remaining"] == "0"
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limit_exceeded"
    assert second.headers["retry-after"] == "60"


def test_rate_limit_isolated_by_verified_identity(monkeypatch, tmp_path):
    monkeypatch.setenv("BIOLAB_RATE_LIMIT_PER_MINUTE", "1")
    with TestClient(create_app(tmp_path / "identity-limit.db", require_auth=True)) as client:
        key_a = client.app.state.conn  # type: ignore[attr-defined]
        from biolab import auth
        raw_a = auth.create_api_key(key_a, "a", "user:a")
        raw_b = auth.create_api_key(key_a, "b", "user:b")
        first_a = client.get("/v1/storage", headers={"Authorization": f"Bearer {raw_a}"})
        second_a = client.get("/v1/storage", headers={"Authorization": f"Bearer {raw_a}"})
        first_b = client.get("/v1/storage", headers={"Authorization": f"Bearer {raw_b}"})

    assert first_a.status_code == 200
    assert second_a.status_code == 429
    assert first_b.status_code == 200
