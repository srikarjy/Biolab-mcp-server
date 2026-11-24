from urllib.error import HTTPError

import pytest
from biolab.http_retry import urlopen_with_retry


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_retry_recovers_from_transient_http_errors_without_unbounded_loop():
    attempts = []
    sleeps = []

    def opener(_target, timeout):
        attempts.append(timeout)
        if len(attempts) < 3:
            raise HTTPError("https://example.test", 503, "busy", {}, None)
        return _Response()

    result = urlopen_with_retry(opener, "url", timeout=4, backoff_seconds=0.25, sleeper=sleeps.append)

    assert isinstance(result, _Response)
    assert attempts == [4, 4, 4]
    assert sleeps == [0.25, 0.5]


def test_non_retryable_http_error_fails_immediately():
    attempts = []

    def opener(_target, timeout):
        attempts.append(timeout)
        raise HTTPError("https://example.test", 401, "unauthorized", {}, None)

    with pytest.raises(HTTPError):
        urlopen_with_retry(opener, "url", timeout=4, sleeper=lambda _delay: pytest.fail("should not sleep"))

    assert attempts == [4]
