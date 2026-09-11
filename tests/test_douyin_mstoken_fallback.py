"""msToken 初始化不可用时，仍使用已有 Cookie 与签名请求官方详情（无真实网络）。"""
import json
import threading
from copy import deepcopy
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.downloaders import douyin_downloader as module
from app.exceptions.task import TaskCancelledError
from app.services.inspect import inspect_video

VIDEO_ID = "7664861474845658402"
VIDEO_URL = f"https://www.douyin.com/video/{VIDEO_ID}"
DETAIL = {
    "status_code": 0,
    "aweme_detail": {
        "aweme_id": VIDEO_ID,
        "item_title": "测试视频",
        "video": {"duration": 15300},
    },
}


@pytest.fixture
def native_io():
    with (
        mock.patch.object(module, "_get_cfm") as cfm,
        mock.patch.object(module.httpx, "Client") as client,
        mock.patch.object(module, "public_get") as get,
        mock.patch.object(module.ABogus, "get_value", return_value="synthetic-signature") as sign,
        mock.patch("app.services.inspect._inspect_ytdlp", side_effect=AssertionError("不应使用 yt-dlp")),
    ):
        cfm.return_value.get.return_value = "sessionid=synthetic-session"
        post = client.return_value.__enter__.return_value.post
        post.side_effect = httpx.ConnectError("TLS connection failed")
        get.return_value.json.return_value = deepcopy(DETAIL)
        yield post, get, sign


def test_inspect_reaches_detail_when_real_mstoken_initialization_fails(native_io):
    post, get, sign = native_io

    result = inspect_video(VIDEO_URL)

    assert result["ok"], result.get("error")
    assert result["video_id"] == VIDEO_ID
    assert result["entries"][0]["duration"] == 15.3
    post.assert_called_once()
    get.assert_called_once()
    assert get.call_args.kwargs["headers"]["Cookie"] == "sessionid=synthetic-session"
    assert "a_bogus=synthetic-signature" in get.call_args.args[0]
    assert sign.call_args.args[0]["msToken"] == ""
    assert sign.call_args.args[0]["aweme_id"] == VIDEO_ID


def _token_response(status=200, token=""):
    return httpx.Response(
        status,
        headers={"Set-Cookie": f"msToken={token}; Path=/; Secure"} if token else {},
        request=httpx.Request("POST", "https://mssdk.bytedance.com/web/report"),
    )


@pytest.mark.parametrize("token", ["x" * 120, "x" * 128])
def test_valid_token_is_still_used_and_not_replaced(native_io, token):
    post, get, sign = native_io
    post.side_effect = None
    post.return_value = _token_response(token=token)

    result = module.DouyinDownloader().fetch_video_info(VIDEO_URL)

    assert result == DETAIL
    assert sign.call_args.args[0]["msToken"] == token
    query = parse_qs(urlsplit(get.call_args.args[0]).query, keep_blank_values=True)
    assert query["msToken"] == [token]
    get.assert_called_once()


@pytest.mark.parametrize("response", [_token_response(503), _token_response(), _token_response(token="short-secret-token")])
def test_bad_token_response_can_fall_back_without_logging_credentials(native_io, response, caplog):
    post, get, _ = native_io
    post.side_effect = None
    post.return_value = response

    result = inspect_video(VIDEO_URL)

    assert result["ok"], result.get("error")
    assert "msToken" in caplog.text
    assert "short-secret-token" not in caplog.text
    assert "synthetic-session" not in caplog.text
    assert "synthetic-signature" not in caplog.text
    get.assert_called_once()


def test_fallback_details_are_cached_within_the_same_downloader(native_io):
    post, get, _ = native_io
    downloader = module.DouyinDownloader()

    first = downloader.fetch_video_info(VIDEO_URL)
    second = downloader.fetch_video_info(VIDEO_URL)

    assert first == second == DETAIL
    post.assert_called_once()
    get.assert_called_once()


@pytest.mark.parametrize("detail", [{}, {"status_code": 8}, {"aweme_detail": None}])
def test_fallback_does_not_report_success_without_video_details(native_io, detail):
    _, get, _ = native_io
    get.return_value.json.return_value = detail

    result = inspect_video(VIDEO_URL)

    assert result["ok"] is False
    assert "未返回视频详情" in result["error"]
    assert "login douyin" not in result["error"]
    get.assert_called_once()


def test_fallback_preserves_the_detail_error_not_the_initialization_error(native_io):
    _, get, _ = native_io
    error = ConnectionError("video detail unavailable")
    get.side_effect = error

    with pytest.raises(ValueError, match="video detail unavailable") as caught:
        module.DouyinDownloader().fetch_video_info(VIDEO_URL)

    assert caught.value.__cause__ is error
    get.assert_called_once()


def test_cancelled_initialization_never_sends_detail_request(native_io):
    post, get, sign = native_io
    post.side_effect = TaskCancelledError("cancelled during initialization")

    with pytest.raises(TaskCancelledError):
        module.DouyinDownloader().fetch_video_info(VIDEO_URL)

    get.assert_not_called()
    sign.assert_not_called()


def test_cancel_event_set_when_token_request_fails_prevents_fallback(native_io):
    post, get, sign = native_io
    event = threading.Event()

    def cancel_then_fail(*args, **kwargs):
        event.set()
        raise httpx.ConnectError("TLS connection failed")

    post.side_effect = cancel_then_fail
    with pytest.raises(TaskCancelledError):
        module.DouyinDownloader().fetch_video_info(VIDEO_URL, cancel_event=event)

    get.assert_not_called()
    sign.assert_not_called()


def test_cancelled_before_initialization_sends_no_requests(native_io):
    post, get, sign = native_io
    event = threading.Event()
    event.set()

    with pytest.raises(TaskCancelledError):
        module.DouyinDownloader().fetch_video_info(VIDEO_URL, cancel_event=event)

    post.assert_not_called()
    get.assert_not_called()
    sign.assert_not_called()


def test_unexpected_initialization_programming_error_is_not_silently_ignored(native_io):
    _, get, _ = native_io
    error = RuntimeError("unexpected implementation failure")
    with mock.patch.object(module.DouyinDownloader, "gen_real_msToken", side_effect=error):
        with pytest.raises(ValueError) as caught:
            module.DouyinDownloader().fetch_video_info(VIDEO_URL)

    assert caught.value.__cause__ is error
    get.assert_not_called()


@pytest.mark.parametrize("skip_download", [True, False])
def test_download_entry_uses_the_same_token_failure_fallback(native_io, tmp_path, skip_download):
    _, get, _ = native_io
    detail = deepcopy(DETAIL)
    detail["aweme_detail"]["music"] = {"play_url": {"url_list": ["https://example.com/audio.mp3"]}}
    get.return_value.json.return_value = detail

    with mock.patch.object(module, "stream_download") as stream:
        result = module.DouyinDownloader().download(
            VIDEO_URL, output_dir=str(tmp_path), skip_download=skip_download,
        )

    assert result.video_id == VIDEO_ID
    assert result.duration == 15.3
    if skip_download:
        stream.assert_not_called()
    else:
        stream.assert_called_once()
        assert stream.call_args.args[0] == "https://example.com/audio.mp3"
        assert stream.call_args.kwargs["headers"]["Cookie"] == "sessionid=synthetic-session"


def test_mcp_inspect_uses_token_failure_fallback_without_leaking_credentials(native_io):
    from videonote_mcp import server

    result = json.loads(server.inspect_video(VIDEO_URL))

    assert result["ok"], result.get("error")
    assert result["video_id"] == VIDEO_ID
    assert "synthetic-session" not in json.dumps(result)
    assert "synthetic-signature" not in json.dumps(result)
