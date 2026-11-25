"""Optional live deployment smoke test.

Run with ``BIOLAB_PRODUCTION_SMOKE=1`` to verify the configured production URL.
It is skipped in normal CI so local tests never depend on the network.
"""

import json
import os
import urllib.error
import urllib.request

import pytest


@pytest.mark.skipif(not os.environ.get("BIOLAB_PRODUCTION_SMOKE"), reason="production smoke is opt-in")
def test_production_health_openapi_and_metrics():
    base = os.environ.get("BIOLAB_PRODUCTION_URL", "https://biolab-a2a.vercel.app").rstrip("/")

    def get(path):
        request = urllib.request.Request(f"{base}{path}")
        smoke_key = os.environ.get("BIOLAB_PRODUCTION_SMOKE_API_KEY", "")
        if smoke_key:
            request.add_header("Authorization", f"Bearer {smoke_key}")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    health_status, health = get("/health")
    schema_status, schema = get("/openapi.json")
    metrics_status, metrics = get("/metrics")
    assert health_status == 200
    assert health == {"status": "ok", "service": "biolab"}
    assert schema_status == 200
    assert "/v1/jobs/{job_id}" in schema["paths"]
    assert metrics_status in {200, 401}
    if metrics_status == 200:
        assert metrics["audit_chain_valid"] is True
        assert "runtime" in metrics
    else:
        assert metrics["error"]["code"] == "authentication_required"
