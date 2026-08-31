"""小宇宙官方文稿：平台识别 / token 解析 / 文稿抓取 / 下载器（全 mock，不碰真网）。"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.downloaders.xiaoyuzhou_downloader import XiaoyuzhouDownloader
from app.downloaders.xiaoyuzhou_subtitle import (
    XiaoyuzhouTranscriptFetcher,
    format_xiaoyuzhou_cookie,
    parse_xiaoyuzhou_tokens,
    verify_xiaoyuzhou_login,
)
from app.models.audio_model import AudioDownloadResult
from app.services import constant, pipeline
from app.utils.url_parser import extract_video_id

EID = "69b3b675772ac2295bfc01d0"
EP_URL = f"https://www.xiaoyuzhoufm.com/episode/{EID}"
MEDIA_ID = "native-media-id"
CDN_URL = "https://cdn.example.com/transcript.json"


class _FakeResp:
    def __init__(self, status=200, payload=None, headers=None, text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _episode_payload(transcript_media_id=MEDIA_ID, media_id="external-url", duration=120):
    return {
        "data": {
            "eid": EID,
            "title": "demo",
            "duration": duration,
            "transcriptMediaId": transcript_media_id,
            "media": {"id": media_id},
        }
    }


class TokenParseTest(unittest.TestCase):
    def test_cookie_string(self):
        raw = (
            "x-jike-access-token=aaa.bbb.ccc; "
            "x-jike-refresh-token=rrr; "
            "x-jike-device-id=dev-1"
        )
        self.assertEqual(
            parse_xiaoyuzhou_tokens(raw),
            {"access": "aaa.bbb.ccc", "refresh": "rrr", "device": "dev-1"},
        )

    def test_alias_keys(self):
        raw = "access_token=tok; refresh_token=ref"
        self.assertEqual(parse_xiaoyuzhou_tokens(raw)["access"], "tok")
        self.assertEqual(parse_xiaoyuzhou_tokens(raw)["refresh"], "ref")

    def test_raw_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJkYXRhIjoiIn0.sig"
        self.assertEqual(parse_xiaoyuzhou_tokens(jwt)["access"], jwt)
        self.assertIsNone(parse_xiaoyuzhou_tokens(jwt)["refresh"])

    def test_empty(self):
        self.assertIsNone(parse_xiaoyuzhou_tokens("")["access"])
        self.assertIsNone(parse_xiaoyuzhou_tokens(None)["access"])  # type: ignore[arg-type]

    def test_roundtrip_format(self):
        cookie = format_xiaoyuzhou_cookie("acc", "ref", "dev")
        self.assertEqual(
            parse_xiaoyuzhou_tokens(cookie),
            {"access": "acc", "refresh": "ref", "device": "dev"},
        )


class ExtractIdTest(unittest.TestCase):
    def test_episode_url(self):
        self.assertEqual(extract_video_id(EP_URL, "xiaoyuzhou"), EID)

    def test_episode_url_with_query(self):
        self.assertEqual(
            extract_video_id(f"{EP_URL}?s=abc", "xiaoyuzhou"),
            EID,
        )

    def test_podcast_url_is_not_episode(self):
        self.assertIsNone(
            extract_video_id("https://www.xiaoyuzhoufm.com/podcast/6013f9f58e2f7ee375cf4216", "xiaoyuzhou")
        )

    def test_other_platform_untouched(self):
        self.assertIsNone(extract_video_id(EP_URL, "youtube"))


class DetectPlatformTest(unittest.TestCase):
    def test_www_and_apex(self):
        self.assertEqual(pipeline.detect_platform(EP_URL), "xiaoyuzhou")
        self.assertEqual(
            pipeline.detect_platform("https://xiaoyuzhoufm.com/episode/abc"),
            "xiaoyuzhou",
        )

    def test_does_not_match_lookalike_host(self):
        self.assertEqual(
            pipeline.detect_platform("https://evilxiaoyuzhoufm.com/episode/abc"),
            "generic",
        )


class FetcherTest(unittest.TestCase):
    def setUp(self):
        self.mgr = mock.Mock()
        self.mgr.get.return_value = format_xiaoyuzhou_cookie("acc-token", "ref-token", "dev-1")
        self.fetcher = XiaoyuzhouTranscriptFetcher(cookie_mgr=self.mgr)

    def test_no_token_returns_none(self):
        self.mgr.get.return_value = ""
        self.assertIsNone(self.fetcher.fetch_subtitles(EP_URL))

    def test_no_eid_returns_none(self):
        self.assertIsNone(self.fetcher.fetch_subtitles("https://www.xiaoyuzhoufm.com/podcast/abc"))

    def test_success(self):
        def _route(method, url, **kwargs):
            if url.endswith("/v1/episode/get"):
                return _FakeResp(payload=_episode_payload())
            if url.endswith("/v1/episode-transcript/get"):
                self.assertEqual(kwargs.get("json"), {"eid": EID, "mediaId": MEDIA_ID})
                return _FakeResp(payload={"data": {"transcriptUrl": CDN_URL}})
            if url == CDN_URL:
                return _FakeResp(payload=[
                    {"text": "你好", "startMs": 0},
                    {"text": "世界", "startMs": 1500},
                ])
            raise AssertionError(f"unexpected {method} {url}")

        with mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_get", side_effect=lambda url, **kw: _route("GET", url, **kw)), \
             mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_post", side_effect=lambda url, **kw: _route("POST", url, **kw)):
            result = self.fetcher.fetch_subtitles(EP_URL)
        self.assertIsNotNone(result)
        self.assertEqual(result.language, "zh")
        self.assertEqual(result.full_text, "你好 世界")
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(result.segments[0].start, 0.0)
        self.assertEqual(result.segments[0].end, 1.5)
        self.assertEqual(result.segments[1].end, 120.0)
        self.assertEqual(result.raw["source"], "xiaoyuzhou_official_transcript")
        self.assertEqual(result.raw["eid"], EID)

    def test_prefers_transcript_media_id(self):
        seen = {}

        def _route(method, url, **kwargs):
            if url.endswith("/v1/episode/get"):
                return _FakeResp(payload=_episode_payload(transcript_media_id="native-1", media_id="https://rss.example/a.mp3"))
            if url.endswith("/v1/episode-transcript/get"):
                seen["payload"] = kwargs.get("json")
                return _FakeResp(payload={"data": {}})
            raise AssertionError(url)

        with mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_get", side_effect=lambda url, **kw: _route("GET", url, **kw)), \
             mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_post", side_effect=lambda url, **kw: _route("POST", url, **kw)):
            self.assertIsNone(self.fetcher.fetch_subtitles(EP_URL))
        self.assertEqual(seen["payload"]["mediaId"], "native-1")

    def test_no_subtitle_returns_none(self):
        def _route(method, url, **kwargs):
            if url.endswith("/v1/episode/get"):
                return _FakeResp(payload=_episode_payload())
            if url.endswith("/v1/episode-transcript/get"):
                return _FakeResp(payload={"data": {"transcriptUrl": None}})
            raise AssertionError(url)

        with mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_get", side_effect=lambda url, **kw: _route("GET", url, **kw)), \
             mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_post", side_effect=lambda url, **kw: _route("POST", url, **kw)):
            self.assertIsNone(self.fetcher.fetch_subtitles(EP_URL))

    def test_401_refresh_then_success(self):
        state = {"transcript_calls": 0}

        def _post(url, **kwargs):
            if url.endswith("/app_auth_tokens.refresh"):
                return _FakeResp(
                    payload={},
                    headers={
                        "x-jike-access-token": "new-acc",
                        "x-jike-refresh-token": "new-ref",
                    },
                )
            if url.endswith("/v1/episode-transcript/get"):
                state["transcript_calls"] += 1
                if state["transcript_calls"] == 1:
                    return _FakeResp(status=401)
                return _FakeResp(payload={"data": {"transcriptUrl": CDN_URL}})
            raise AssertionError(url)

        def _get(url, **kwargs):
            if url.endswith("/v1/episode/get"):
                return _FakeResp(payload=_episode_payload())
            if url == CDN_URL:
                return _FakeResp(payload=[{"text": "段", "startMs": 0, "endMs": 2000}])
            raise AssertionError(url)

        with mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_get", side_effect=_get), \
             mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_post", side_effect=_post):
            result = self.fetcher.fetch_subtitles(EP_URL)
        self.assertIsNotNone(result)
        self.assertEqual(result.segments[0].end, 2.0)
        self.mgr.set.assert_called()
        saved = self.mgr.set.call_args.args[1]
        self.assertIn("new-acc", saved)
        self.assertIn("new-ref", saved)

    def test_401_without_refresh_raises(self):
        from app.exceptions.task import OfficialTranscriptFetchError

        self.mgr.get.return_value = format_xiaoyuzhou_cookie("acc-only")

        def _get(url, **kwargs):
            if url.endswith("/v1/episode/get"):
                return _FakeResp(status=401)
            raise AssertionError(url)

        with mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_get", side_effect=_get), \
             mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_post") as m_post:
            with self.assertRaises(OfficialTranscriptFetchError):
                self.fetcher.fetch_subtitles(EP_URL)
        m_post.assert_not_called()

    def test_http_500_raises(self):
        from app.exceptions.task import OfficialTranscriptFetchError

        def _get(url, **kwargs):
            if url.endswith("/v1/episode/get"):
                return _FakeResp(status=500)
            raise AssertionError(url)

        with mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_get", side_effect=_get), \
             mock.patch("app.downloaders.xiaoyuzhou_subtitle.public_post"):
            with self.assertRaises(OfficialTranscriptFetchError) as ei:
                self.fetcher.fetch_subtitles(EP_URL)
        self.assertIn("HTTP 500", str(ei.exception))


class VerifyLoginTest(unittest.TestCase):
    def test_ok(self):
        with mock.patch(
            "app.downloaders.xiaoyuzhou_subtitle.CookieConfigManager"
        ) as m_cls, mock.patch(
            "app.downloaders.xiaoyuzhou_subtitle.public_post",
            return_value=_FakeResp(payload={"data": []}),
        ):
            m_cls.return_value.get.return_value = format_xiaoyuzhou_cookie("acc", "ref")
            self.assertEqual(verify_xiaoyuzhou_login(), "")

    def test_missing_token(self):
        with mock.patch("app.downloaders.xiaoyuzhou_subtitle.CookieConfigManager") as m_cls:
            m_cls.return_value.get.return_value = ""
            self.assertIn("未配置", verify_xiaoyuzhou_login())


class DownloaderTest(unittest.TestCase):
    def test_factory(self):
        inst = constant.get_downloader("xiaoyuzhou")
        self.assertIsInstance(inst, XiaoyuzhouDownloader)

    def test_download_rewrites_platform_and_id(self):
        dl = XiaoyuzhouDownloader()
        fake = AudioDownloadResult(
            file_path="/tmp/x.m4a",
            title="ep",
            duration=10,
            cover_url=None,
            platform="generic",
            video_id="yt-dlp-id",
            raw_info={},
        )
        with mock.patch(
            "app.downloaders.generic_downloader.GenericDownloader.download",
            return_value=fake,
        ):
            result = dl.download(EP_URL)
        self.assertEqual(result.platform, "xiaoyuzhou")
        self.assertEqual(result.video_id, EID)

    def test_cookie_not_injected_as_browser_cookie(self):
        dl = XiaoyuzhouDownloader()
        self.assertEqual(dl._get_cookie(), "")

    def test_download_subtitles_delegates(self):
        dl = XiaoyuzhouDownloader()
        fake_tr = mock.Mock(segments=[1])
        with mock.patch(
            "app.downloaders.xiaoyuzhou_downloader.XiaoyuzhouTranscriptFetcher"
        ) as m_cls, mock.patch(
            "app.downloaders.xiaoyuzhou_downloader.CookieConfigManager"
        ) as m_cookie:
            m_cookie.return_value.get.return_value = "x-jike-access-token=acc"
            m_cls.return_value.fetch_subtitles.return_value = fake_tr
            self.assertIs(dl.download_subtitles(EP_URL), fake_tr)

    def test_download_subtitles_propagates_official_fetch_error(self):
        from app.exceptions.task import OfficialTranscriptFetchError

        dl = XiaoyuzhouDownloader()
        with mock.patch(
            "app.downloaders.xiaoyuzhou_downloader.XiaoyuzhouTranscriptFetcher"
        ) as m_cls, mock.patch(
            "app.downloaders.xiaoyuzhou_downloader.CookieConfigManager"
        ) as m_cookie:
            m_cookie.return_value.get.return_value = "x-jike-access-token=acc"
            m_cls.return_value.fetch_subtitles.side_effect = OfficialTranscriptFetchError("cdn 500")
            with self.assertRaises(OfficialTranscriptFetchError):
                dl.download_subtitles(EP_URL)


if __name__ == "__main__":
    unittest.main()
