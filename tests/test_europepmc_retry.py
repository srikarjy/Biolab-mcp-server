"""Offline tests for Europe PMC transient-failure handling."""

import io

from biolab import europepmc_client


def test_search_retries_a_transient_timeout(monkeypatch):
    response = io.BytesIO(
        b"<responseWrapper><resultList><result><id>PMC1</id></result></resultList></responseWrapper>"
    )
    attempts = iter([TimeoutError("slow upstream"), response])

    def fake_urlopen(*_args, **_kwargs):
        result = next(attempts)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(europepmc_client, "urlopen", fake_urlopen)
    monkeypatch.setattr(europepmc_client.time, "sleep", lambda _seconds: None)

    results = europepmc_client.search("KRAS", 1)

    assert results[0].findtext("id") == "PMC1"
