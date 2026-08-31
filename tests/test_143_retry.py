"""第 19 轮 B 站官方 GET 有界重试回归测试（#143 C4）。"""

from unittest import mock

import pytest
import requests

from app.downloaders.common import public_get_retry


class _RetryResponse:
    def __init__(self, status_code):
        self.status_code = status_code
        self.closed = False

    def close(self):
        self.closed = True


def test_public_get_retry_retries_connection_error_then_succeeds():
    import app.downloaders.common as common

    response = _RetryResponse(200)
    with mock.patch.object(
        common,
        "public_get",
        side_effect=[requests.exceptions.ConnectionError("temporary"), response],
    ) as get:
        result = public_get_retry("https://example.com/api", base_delay=0, deadline=1)
    assert result is response
    assert get.call_count == 2


def test_public_get_retry_retries_429_and_5xx():
    import app.downloaders.common as common

    first = _RetryResponse(503)
    second = _RetryResponse(429)
    final = _RetryResponse(200)
    with mock.patch.object(common, "public_get", side_effect=[first, second, final]) as get:
        result = public_get_retry("https://example.com/api", base_delay=0, deadline=1)
    assert result is final
    assert get.call_count == 3
    assert first.closed is True
    assert second.closed is True


def test_public_get_retry_does_not_retry_client_error():
    import app.downloaders.common as common

    response = _RetryResponse(403)
    with mock.patch.object(common, "public_get", return_value=response) as get:
        result = public_get_retry("https://example.com/api", base_delay=0, deadline=1)
    assert result is response
    get.assert_called_once()
    assert response.closed is False


def test_public_get_retry_returns_final_transient_response_after_attempts():
    import app.downloaders.common as common

    responses = [_RetryResponse(500) for _ in range(3)]
    with mock.patch.object(common, "public_get", side_effect=responses) as get:
        result = public_get_retry("https://example.com/api", attempts=3, base_delay=0, deadline=1)
    assert result is responses[-1]
    assert get.call_count == 3
    assert responses[0].closed is True
    assert responses[1].closed is True
    assert responses[2].closed is False


def test_public_get_retry_stops_when_total_deadline_expires():
    import app.downloaders.common as common

    responses = [_RetryResponse(503), _RetryResponse(503), _RetryResponse(200)]
    with mock.patch.object(common, "public_get", side_effect=responses) as get, mock.patch.object(
        common.time,
        "monotonic",
        side_effect=[0.0, 0.1, 0.1, 2.0],
    ) as monotonic, mock.patch.object(common.time, "sleep") as sleep:
        result = public_get_retry("https://example.com/api", base_delay=10, deadline=1)
    assert result is responses[1]
    assert get.call_count == 2
    assert monotonic.call_count == 4
    sleep.assert_called_once_with(0.9)


    with mock.patch("app.downloaders.common.public_get") as get:
        with pytest.raises(ValueError, match="deadline"):
            public_get_retry("https://example.com/api", deadline=0)
    get.assert_not_called()
