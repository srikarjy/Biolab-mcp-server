"""Phase 1 API contract checks."""

from biolab.web import create_app
from starlette.testclient import TestClient


def test_every_http_response_has_a_request_id(tmp_path):
    with TestClient(create_app(tmp_path / "request-id.db", require_auth=False)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert len(response.headers["x-request-id"]) == 32


def test_authentication_errors_use_the_structured_contract(tmp_path):
    with TestClient(create_app(tmp_path / "auth-contract.db", require_auth=True)) as client:
        response = client.get("/v1/projects")

    assert response.status_code == 401
    assert response.json()["error"] == {
        "code": "authentication_required",
        "message": "API key required",
    }
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_invalid_api_key_is_structured_and_not_a_project_error(tmp_path):
    with TestClient(create_app(tmp_path / "invalid-key.db", require_auth=True)) as client:
        response = client.get("/v1/projects", headers={"Authorization": "Bearer blk_invalid"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_mutations_are_idempotent_for_the_same_request_key(tmp_path):
    with TestClient(create_app(tmp_path / "idempotency.db", require_auth=False)) as client:
        first = client.post(
            "/v1/projects",
            json={"name": "One project"},
            headers={"Idempotency-Key": "create-one"},
        )
        replay = client.post(
            "/v1/projects",
            json={"name": "One project"},
            headers={"Idempotency-Key": "create-one"},
        )
        conflict = client.post(
            "/v1/projects",
            json={"name": "Different project"},
            headers={"Idempotency-Key": "create-one"},
        )

    assert first.status_code == 201
    assert replay.status_code == 201
    assert replay.json() == first.json()
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"
