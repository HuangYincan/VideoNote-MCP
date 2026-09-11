"""抖音预检复用原生下载器，不误入 yt-dlp 的未签名接口。"""
import json
from copy import deepcopy
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.services.inspect import inspect_video

VIDEO_ID = "7664861474845658402"
VIDEO_URL = f"https://www.douyin.com/video/{VIDEO_ID}"
FRESH_COOKIES_ERROR = "Fresh cookies (not necessarily logged in) are needed"
DETAIL = {
    "aweme_detail": {
        "aweme_id": VIDEO_ID,
        "desc": "测试视频简介",
        "item_title": "测试视频",
        "video": {"duration": 15300},
    }
}


@pytest.fixture(autouse=True)
def isolated_cookie_manager():
    # 只使用合成 Cookie；不读取真实账号配置，也不落盘。
    with (
        mock.patch("app.downloaders.douyin_downloader._get_cfm") as get_cfm,
        mock.patch("app.services.inspect._inspect_ytdlp", side_effect=RuntimeError(FRESH_COOKIES_ERROR)),
    ):
        get_cfm.return_value.get.return_value = "sessionid=synthetic-session"
        yield


@pytest.mark.parametrize(
    "url",
    [
        VIDEO_URL,
        f"https://www.douyin.com/jingxuan?modal_id={VIDEO_ID}",
        "https://v.douyin.com/_pnGDe3Hk3E/",
    ],
)
def test_inspect_douyin_uses_native_metadata_not_ytdlp(url):
    with (
        mock.patch(
            "app.downloaders.douyin_downloader.DouyinDownloader.fetch_video_info",
            return_value=DETAIL,
        ) as fetch,
        mock.patch(
            "app.services.inspect._inspect_ytdlp",
            side_effect=RuntimeError(FRESH_COOKIES_ERROR),
        ) as ytdlp,
    ):
        result = inspect_video(url)

    assert result["ok"], result.get("error")
    assert result["platform"] == "douyin"
    assert result["kind"] == "single"
    assert result["video_id"] == VIDEO_ID
    assert result["title"] == "测试视频"
    assert result["total"] == 1
    assert result["truncated"] is False
    assert result["entries"] == [
        {
            "p": 1,
            "title": "测试视频",
            "duration": 15.3,
            "url": VIDEO_URL,
            "video_id": VIDEO_ID,
        }
    ]
    fetch.assert_called_once_with(url)
    ytdlp.assert_not_called()


@pytest.mark.parametrize("payload", [None, [], {}, {"aweme_detail": None}, {"aweme_detail": []}])
def test_inspect_douyin_missing_metadata_is_not_cookie_expiry(payload):
    with mock.patch(
        "app.downloaders.douyin_downloader.DouyinDownloader.fetch_video_info",
        return_value=payload,
    ):
        result = inspect_video(VIDEO_URL)

    assert result["ok"] is False
    assert result["platform"] == "douyin"
    assert "未返回视频详情" in result["error"]
    assert "login douyin" not in result["error"]
    assert FRESH_COOKIES_ERROR not in result["error"]


@pytest.mark.parametrize("aweme_id", [None, "", "not-an-id", "../bad", True, {}])
def test_inspect_douyin_rejects_missing_or_malformed_video_id(aweme_id):
    payload = deepcopy(DETAIL)
    payload["aweme_detail"]["aweme_id"] = aweme_id
    with mock.patch(
        "app.downloaders.douyin_downloader.DouyinDownloader.fetch_video_info",
        return_value=payload,
    ):
        result = inspect_video(VIDEO_URL)

    assert result["ok"] is False
    assert "视频 ID" in result["error"]


@pytest.mark.parametrize("video", [None, {}, [], "invalid"])
def test_inspect_douyin_non_video_is_not_transcribable(video):
    payload = deepcopy(DETAIL)
    payload["aweme_detail"]["video"] = video
    with mock.patch(
        "app.downloaders.douyin_downloader.DouyinDownloader.fetch_video_info",
        return_value=payload,
    ):
        result = inspect_video(VIDEO_URL)

    assert result["ok"] is False
    assert "视频" in result["error"]


@pytest.mark.parametrize(
    ("duration", "expected"),
    [(0, None), (None, None), ("15300", 15.3), ("unknown", None), (-1, None),
     (True, None), (float("inf"), None), (float("nan"), None)],
)
def test_inspect_douyin_optional_duration_does_not_break_metadata(duration, expected):
    payload = deepcopy(DETAIL)
    payload["aweme_detail"]["video"]["duration"] = duration
    with mock.patch(
        "app.downloaders.douyin_downloader.DouyinDownloader.fetch_video_info",
        return_value=payload,
    ):
        result = inspect_video(VIDEO_URL)

    assert result["ok"], result.get("error")
    assert result["entries"][0]["duration"] == expected


@pytest.mark.parametrize("description", ["视频简介", ""])
def test_inspect_douyin_title_falls_back_to_description(description):
    payload = deepcopy(DETAIL)
    payload["aweme_detail"].pop("item_title")
    payload["aweme_detail"]["desc"] = description
    with mock.patch(
        "app.downloaders.douyin_downloader.DouyinDownloader.fetch_video_info",
        return_value=payload,
    ):
        result = inspect_video(VIDEO_URL)

    assert result["ok"]
    assert result["title"] == (description or "抖音视频")


def test_inspect_douyin_native_error_is_returned_without_ytdlp_fallback():
    with (
        mock.patch(
            "app.downloaders.douyin_downloader.DouyinDownloader.fetch_video_info",
            side_effect=ValueError("原生接口请求失败"),
        ),
        mock.patch("app.services.inspect._inspect_ytdlp") as ytdlp,
    ):
        result = inspect_video(VIDEO_URL)

    assert result["ok"] is False
    assert result["error"] == "原生接口请求失败"
    ytdlp.assert_not_called()


def test_inspect_douyin_still_rejects_private_network_before_native_request():
    with mock.patch("app.downloaders.douyin_downloader.DouyinDownloader.fetch_video_info") as fetch:
        result = inspect_video("http://127.0.0.1/video/123", platform="douyin")

    assert result["ok"] is False
    fetch.assert_not_called()


@pytest.mark.parametrize(
    "url", [VIDEO_URL, f"https://www.douyin.com/jingxuan?modal_id={VIDEO_ID}",
            "https://v.douyin.com/_pnGDe3Hk3E/"],
)
def test_inspect_douyin_actual_native_request_keeps_cookie_and_signature(url):
    response = mock.Mock()
    response.json.return_value = deepcopy(DETAIL)
    with (
        mock.patch("app.utils.url_parser.resolve_douyin_short_url", return_value=VIDEO_URL),
        mock.patch(
            "app.downloaders.douyin_downloader.DouyinDownloader.gen_real_msToken",
            return_value="synthetic-ms-token",
        ),
        mock.patch("app.downloaders.douyin_downloader.ABogus.get_value", return_value="synthetic-signature"),
        mock.patch("app.downloaders.douyin_downloader.public_get", return_value=response) as get,
    ):
        result = inspect_video(url)

    assert result["ok"], result.get("error")
    assert result["entries"][0]["url"] == VIDEO_URL
    get.assert_called_once()
    parts = urlsplit(get.call_args.args[0])
    assert parts.hostname == "www.douyin.com"
    assert parts.path == "/aweme/v1/web/aweme/detail/"
    params = parse_qs(parts.query)
    assert params["aweme_id"] == [VIDEO_ID]
    assert params["a_bogus"] == ["synthetic-signature"]
    assert params["msToken"] == ["synthetic-ms-token"]
    assert get.call_args.kwargs["headers"]["Cookie"] == "sessionid=synthetic-session"


@pytest.mark.parametrize("url", [VIDEO_URL, f"https://www.douyin.com/jingxuan?modal_id={VIDEO_ID}"])
def test_mcp_inspect_douyin_returns_native_metadata(url):
    from videonote_mcp import server

    with mock.patch(
        "app.downloaders.douyin_downloader.DouyinDownloader.fetch_video_info",
        return_value=deepcopy(DETAIL),
    ):
        result = json.loads(server.inspect_video(url))

    assert result["ok"], result.get("error")
    assert result["platform"] == "douyin"
    assert result["entries"][0]["url"] == VIDEO_URL
    assert "synthetic-session" not in json.dumps(result)
