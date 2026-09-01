"""小红书原生支持：平台识别 / 笔记解析 / 扫码登录 / 下载器（全 mock，不碰真网）。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.downloaders.xiaohongshu_auth import (
    QR_EXPIRED,
    QR_SUCCESS,
    XiaohongshuAuth,
    format_cookie,
    note_from_state,
    parse_cookie_string,
    parse_initial_state,
    verify_xiaohongshu_login,
)
from app.downloaders.xiaohongshu_downloader import XiaohongshuDownloader
from app.downloaders.xiaohongshu_sign import get_a1_and_web_id, sign
from app.services import constant, pipeline
from app.services.inspect import inspect_video
from app.utils.url_parser import extract_video_id

NOTE_ID = "6411cf99000000001300b6d9"
NOTE_URL = f"https://www.xiaohongshu.com/explore/{NOTE_ID}"
CDN = "https://sns-video-bd.xhscdn.com/spectrum/demo.mp4"


def _video_state(note_id=NOTE_ID, master=CDN, duration_ms=5000):
    return {
        "note": {
            "noteDetailMap": {
                note_id: {
                    "note": {
                        "title": "香妃蛋糕",
                        "desc": "太香了",
                        "type": "video",
                        "video": {
                            "media": {
                                "stream": {
                                    "h264": [
                                        {
                                            "masterUrl": master,
                                            "duration": duration_ms,
                                            "width": 720,
                                            "height": 1280,
                                        }
                                    ]
                                }
                            },
                            "consumer": {"originVideoKey": "spectrum/origin"},
                        },
                        "imageList": [{"urlDefault": "https://sns-webpic-qc.xhscdn.com/c.jpg"}],
                    }
                }
            }
        }
    }


def _image_state(note_id=NOTE_ID):
    return {
        "note": {
            "noteDetailMap": {
                note_id: {
                    "note": {
                        "title": "图文",
                        "type": "normal",
                        "imageList": [{"urlDefault": "https://sns-webpic-qc.xhscdn.com/c.jpg"}],
                    }
                }
            }
        }
    }


def _html(state: dict) -> str:
    return f"<script>window.__INITIAL_STATE__={json.dumps(state, ensure_ascii=False)}</script>"


class _FakeResp:
    def __init__(self, status=200, payload=None, text="", url=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.url = url or ""

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class _FakeJar:
    def __init__(self, initial=None):
        self._d = dict(initial or {})

    def set(self, name, value, domain=None):
        self._d[name] = value

    def __iter__(self):
        for k, v in self._d.items():
            yield _FakeCookie(name=k, value=v)


class _FakeSession:
    def __init__(self, handler):
        self.headers = {}
        self.cookies = _FakeJar({"a1": "a1value", "webId": "webidvalue"})
        self._handler = handler

    def get(self, url, **kwargs):
        return self._handler("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._handler("POST", url, **kwargs)

    def close(self):
        pass


class CookieParseTest(unittest.TestCase):
    def test_roundtrip(self):
        raw = "a1=aaa; web_session=sess; webId=wid"
        parsed = parse_cookie_string(raw)
        self.assertEqual(parsed["web_session"], "sess")
        self.assertEqual(parse_cookie_string(format_cookie(parsed))["a1"], "aaa")

    def test_empty(self):
        self.assertEqual(parse_cookie_string(""), {})
        self.assertEqual(parse_cookie_string(None), {})


class SignTest(unittest.TestCase):
    def test_deterministic_with_ctime(self):
        a = sign("/api/sns/web/v1/login/qrcode/create", {"qr_type": 1}, ctime=1729214251341, a1="abc")
        b = sign("/api/sns/web/v1/login/qrcode/create", {"qr_type": 1}, ctime=1729214251341, a1="abc")
        self.assertEqual(a["x-s"], b["x-s"])
        self.assertEqual(a["x-t"], "1729214251341")
        self.assertTrue(a["x-s-common"])

    def test_a1_length(self):
        a1, web_id = get_a1_and_web_id()
        self.assertEqual(len(a1), 52)
        self.assertEqual(len(web_id), 32)


class ExtractIdTest(unittest.TestCase):
    def test_explore(self):
        self.assertEqual(extract_video_id(NOTE_URL, "xiaohongshu"), NOTE_ID)

    def test_discovery_with_token(self):
        url = f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}?xsec_token=ABC="
        self.assertEqual(extract_video_id(url, "xiaohongshu"), NOTE_ID)

    def test_user_profile_note(self):
        url = f"https://www.xiaohongshu.com/user/profile/5c31698d0000000007018a31/{NOTE_ID}"
        self.assertEqual(extract_video_id(url, "xiaohongshu"), NOTE_ID)

    def test_user_profile_without_note(self):
        self.assertIsNone(
            extract_video_id("https://www.xiaohongshu.com/user/profile/5c31698d0000000007018a31", "xiaohongshu")
        )

    def test_short_link_resolves(self):
        long_url = f"{NOTE_URL}?xsec_token=TOK"
        with mock.patch("app.utils.url_parser.public_head", return_value=mock.Mock(url=long_url)):
            from app.utils.url_parser import resolve_xiaohongshu_short_url

            resolve_xiaohongshu_short_url.cache_clear()
            self.assertEqual(
                extract_video_id("https://xhslink.com/a/AbCdEf", "xiaohongshu"),
                NOTE_ID,
            )

    def test_other_platform_untouched(self):
        self.assertIsNone(extract_video_id(NOTE_URL, "youtube"))


class DetectPlatformTest(unittest.TestCase):
    def test_www_and_short_and_rednote(self):
        self.assertEqual(pipeline.detect_platform(NOTE_URL), "xiaohongshu")
        self.assertEqual(pipeline.detect_platform("https://xhslink.com/a/xxx"), "xiaohongshu")
        self.assertEqual(
            pipeline.detect_platform(f"https://www.rednote.com/explore/{NOTE_ID}"),
            "xiaohongshu",
        )

    def test_does_not_match_lookalike(self):
        self.assertEqual(
            pipeline.detect_platform("https://evilxiaohongshu.com/explore/abc"),
            "generic",
        )


class ParseStateTest(unittest.TestCase):
    def test_video_note(self):
        note = note_from_state(_video_state(), NOTE_ID, NOTE_URL)
        self.assertEqual(note.video_url, CDN)
        self.assertAlmostEqual(note.duration, 5.0)
        self.assertEqual(note.title, "香妃蛋糕")
        self.assertTrue(note.cover_url.startswith("https://"))

    def test_image_note_has_no_video(self):
        note = note_from_state(_image_state(), NOTE_ID, NOTE_URL)
        self.assertIsNone(note.video_url)
        self.assertEqual(note.title, "图文")

    def test_undefined_in_html(self):
        html = '<script>window.__INITIAL_STATE__={"a":undefined,"note":{}}</script>'
        state = parse_initial_state(html)
        self.assertEqual(state["a"], None)


class FetchNoteTest(unittest.TestCase):
    def test_success(self):
        html = _html(_video_state())

        def handler(method, url, **kwargs):
            if "explore" in url:
                return _FakeResp(text=html, url=NOTE_URL)
            return _FakeResp(payload={"success": True, "data": {}})

        mgr = mock.Mock()
        mgr.get.return_value = "web_session=sess; a1=aaa"
        auth = XiaohongshuAuth(session=_FakeSession(handler), cookie_mgr=mgr)
        note = auth.fetch_note(NOTE_URL)
        self.assertEqual(note.note_id, NOTE_ID)
        self.assertEqual(note.video_url, CDN)

    def test_missing_id_raises(self):
        auth = XiaohongshuAuth(session=_FakeSession(lambda *a, **k: _FakeResp()), cookie_mgr=mock.Mock())
        with self.assertRaises(ValueError):
            auth.fetch_note("https://www.xiaohongshu.com/user/profile/abc")

    def test_foreign_host_rejected(self):
        auth = XiaohongshuAuth(session=_FakeSession(lambda *a, **k: _FakeResp()), cookie_mgr=mock.Mock())
        with self.assertRaises(ValueError) as ei:
            auth.fetch_note(f"https://attacker.example/explore/{NOTE_ID}")
        self.assertIn("不是小红书", str(ei.exception))


class QrLoginTest(unittest.TestCase):
    def test_create_and_poll_success(self):
        def handler(method, url, **kwargs):
            if url.endswith("/login/qrcode/create"):
                return _FakeResp(
                    payload={"code": 0, "success": True, "data": {
                        "qr_id": "qid", "code": "280148", "url": "xhsdiscover://login?qid=qid",
                    }}
                )
            if "/login/qrcode/status" in url:
                return _FakeResp(
                    payload={"code": 0, "data": {
                        "code_status": QR_SUCCESS,
                        "login_info": {"session": "sess-from-qr", "user_id": "u1"},
                    }}
                )
            return _FakeResp(payload={"success": True, "data": {"user_id": "u1"}})

        mgr = mock.Mock()
        auth = XiaohongshuAuth(session=_FakeSession(handler), cookie_mgr=mgr)
        created = auth.create_qr()
        self.assertEqual(created["qr_id"], "qid")
        poll = auth.poll_qr("qid", "280148")
        self.assertEqual(poll["code_status"], QR_SUCCESS)
        with mock.patch("app.downloaders.xiaohongshu_auth.verify_xiaohongshu_login", return_value=""):
            err = auth.persist()
        self.assertEqual(err, "")
        mgr.set.assert_called_once()
        saved = mgr.set.call_args.args[1]
        self.assertIn("web_session=sess-from-qr", saved)
        self.assertNotIn("qid", saved)  # qr id 不是 cookie

    def test_poll_expired(self):
        def handler(method, url, **kwargs):
            return _FakeResp(payload={"data": {"code_status": QR_EXPIRED}})

        auth = XiaohongshuAuth(session=_FakeSession(handler), cookie_mgr=mock.Mock())
        self.assertEqual(auth.poll_qr("qid", "c").get("code_status"), QR_EXPIRED)

    def test_create_failure(self):
        def handler(method, url, **kwargs):
            return _FakeResp(status=461, payload={"code": 461, "msg": "sign error"})

        auth = XiaohongshuAuth(session=_FakeSession(handler), cookie_mgr=mock.Mock())
        with self.assertRaises(RuntimeError) as ei:
            auth.create_qr()
        self.assertIn("生成二维码失败", str(ei.exception))

    def test_create_406_mentions_cookie(self):
        def handler(method, url, **kwargs):
            return _FakeResp(status=406, payload={"code": -1, "success": False})

        auth = XiaohongshuAuth(session=_FakeSession(handler), cookie_mgr=mock.Mock())
        with self.assertRaises(RuntimeError) as ei:
            auth.create_qr()
        msg = str(ei.exception)
        self.assertIn("406", msg)
        self.assertIn("--cookie", msg)


class BrowserQrParseTest(unittest.TestCase):
    def test_parse_create_underscores(self):
        from app.downloaders.xiaohongshu_browser import parse_qr_create_payload

        out = parse_qr_create_payload({
            "data": {"url": "xhsdiscover://login", "qr_id": "qid", "code": "c1"},
        })
        self.assertEqual(out["url"], "xhsdiscover://login")
        self.assertEqual(out["qr_id"], "qid")
        self.assertEqual(out["code"], "c1")

    def test_parse_create_from_query(self):
        from app.downloaders.xiaohongshu_browser import parse_qr_create_payload

        url = (
            "https://www.xiaohongshu.com/mobile/login"
            "?qrId=60231788145547560&xhs_code=991693&channel_type=web"
        )
        out = parse_qr_create_payload({"data": {"url": url}})
        self.assertEqual(out["qr_id"], "60231788145547560")
        self.assertEqual(out["code"], "991693")
        self.assertTrue(out["url"].startswith("https://"))

    def test_poll_status_camel_and_snake(self):
        from app.downloaders.xiaohongshu_browser import poll_status_from_payload

        self.assertEqual(poll_status_from_payload({"data": {"codeStatus": 1}}), 1)
        self.assertEqual(poll_status_from_payload({"data": {"code_status": 2}}), 2)
        self.assertIsNone(poll_status_from_payload({"data": {}}))

    def test_cookies_filter_domain(self):
        from app.downloaders.xiaohongshu_browser import cookies_from_playwright

        raw = [
            {"name": "web_session", "value": "s1", "domain": ".xiaohongshu.com"},
            {"name": "other", "value": "nope", "domain": ".example.com"},
            {"name": "a1", "value": "aaa", "domain": "edith.xiaohongshu.com"},
            {"name": "evil", "value": "x", "domain": "xiaohongshu.com.evil.com"},
            {"name": "evil2", "value": "y", "domain": "evilxiaohongshu.com"},
        ]
        out = cookies_from_playwright(raw)
        self.assertEqual(out["web_session"], "s1")
        self.assertEqual(out["a1"], "aaa")
        self.assertNotIn("other", out)
        self.assertNotIn("evil", out)
        self.assertNotIn("evil2", out)

    def test_on_response_ignores_non_xhs_host(self):
        from app.downloaders.xiaohongshu_browser import XiaohongshuBrowserQr

        qr = XiaohongshuBrowserQr(cookie_mgr=mock.Mock())
        fake = mock.Mock()
        fake.url = "https://evil.example/api/sns/web/v1/login/qrcode/create"
        fake.json.return_value = {
            "data": {"url": "https://evil.example/phish", "qr_id": "x", "code": "y"},
        }
        qr._on_response(fake)
        self.assertEqual(qr._created, {})

    def test_create_qr_failure_closes(self):
        from app.downloaders.xiaohongshu_browser import XiaohongshuBrowserQr

        qr = XiaohongshuBrowserQr(cookie_mgr=mock.Mock())
        closed = []
        qr.close = lambda: closed.append(1)
        qr._launch = lambda: None
        qr._page = mock.Mock()
        qr._page.goto.side_effect = RuntimeError("打开失败")
        with self.assertRaises(RuntimeError):
            qr.create_qr()
        self.assertTrue(closed)

    def test_poll_does_not_treat_session_rotation_as_success(self):
        from app.downloaders.xiaohongshu_browser import XiaohongshuBrowserQr

        qr = XiaohongshuBrowserQr(cookie_mgr=mock.Mock())
        qr._guest_session = "guest-sess"
        qr._page = None
        qr._cookies = lambda: {"web_session": "rotated-guest", "a1": "aaa"}
        poll = qr.poll_qr("qid", "c")
        self.assertEqual(poll["code_status"], 0)
        qr._status = QR_SUCCESS
        poll = qr.poll_qr("qid", "c")
        self.assertEqual(poll["code_status"], QR_SUCCESS)
        self.assertEqual(qr.persist(), "")
        qr._cookie_mgr.set.assert_called_once()
        saved = qr._cookie_mgr.set.call_args.args[1]
        self.assertIn("web_session=rotated-guest", saved)


class VerifyLoginTest(unittest.TestCase):
    def test_missing_session(self):
        mgr = mock.Mock()
        mgr.get.return_value = "a1=only"
        self.assertIn("未配置", verify_xiaohongshu_login(cookie_mgr=mgr))

    def test_ok(self):
        html = '<script>window.__INITIAL_STATE__={"user":{"userId":"u1"}}</script>'

        def handler(method, url, **kwargs):
            return _FakeResp(text=html)

        mgr = mock.Mock()
        mgr.get.return_value = "web_session=sess; a1=aaa; webId=wid"
        with mock.patch("app.downloaders.xiaohongshu_auth.XiaohongshuAuth") as cls:
            inst = XiaohongshuAuth(session=_FakeSession(handler), cookie_mgr=mgr)
            cls.return_value = inst
            self.assertEqual(verify_xiaohongshu_login(cookie_mgr=mgr), "")

    def test_guest_homepage_not_logged_in(self):
        html = '<script>window.__INITIAL_STATE__={"user":{"userId":""}}</script>'

        def handler(method, url, **kwargs):
            return _FakeResp(text=html)

        mgr = mock.Mock()
        mgr.get.return_value = "web_session=guest; a1=aaa; webId=wid"
        with mock.patch("app.downloaders.xiaohongshu_auth.XiaohongshuAuth") as cls:
            inst = XiaohongshuAuth(session=_FakeSession(handler), cookie_mgr=mgr)
            cls.return_value = inst
            self.assertIn("未能确认", verify_xiaohongshu_login(cookie_mgr=mgr))


class DownloaderTest(unittest.TestCase):
    def test_skip_download_metadata(self):
        note_mod = mock.Mock()
        note_mod.fetch_note.return_value = note_from_state(_video_state(), NOTE_ID, NOTE_URL)
        dl = XiaohongshuDownloader(auth=note_mod)
        with tempfile.TemporaryDirectory() as td:
            result = dl.download(NOTE_URL, output_dir=td, skip_download=True)
        self.assertEqual(result.platform, "xiaohongshu")
        self.assertEqual(result.video_id, NOTE_ID)
        self.assertEqual(result.title, "香妃蛋糕")

    def test_skip_download_fetch_failure_uses_stub(self):
        note_mod = mock.Mock()
        note_mod.fetch_note.side_effect = RuntimeError("登录墙")
        dl = XiaohongshuDownloader(auth=note_mod)
        with tempfile.TemporaryDirectory() as td:
            result = dl.download(NOTE_URL, output_dir=td, skip_download=True)
        self.assertEqual(result.video_id, NOTE_ID)
        self.assertEqual(result.platform, "xiaohongshu")

    def test_image_note_raises(self):
        note_mod = mock.Mock()
        note_mod.fetch_note.return_value = note_from_state(_image_state(), NOTE_ID, NOTE_URL)
        dl = XiaohongshuDownloader(auth=note_mod)
        with self.assertRaises(RuntimeError) as ei:
            dl.download(NOTE_URL, skip_download=True)
        self.assertIn("不是视频", str(ei.exception))

    def test_download_writes_files(self):
        note_mod = mock.Mock()
        note_mod.fetch_note.return_value = note_from_state(_video_state(), NOTE_ID, NOTE_URL)
        dl = XiaohongshuDownloader(auth=note_mod)
        with tempfile.TemporaryDirectory() as td:
            def _stream(url, path, **kwargs):
                Path(path).write_bytes(b"mp4data")

            with mock.patch("app.downloaders.xiaohongshu_downloader.stream_download", side_effect=_stream), \
                 mock.patch("app.downloaders.xiaohongshu_downloader.subprocess.Popen") as m_ff:
                def _ffmpeg(cmd, **kwargs):
                    Path(cmd[-1]).write_bytes(b"mp3data")
                    proc = mock.Mock()
                    proc.poll.return_value = 0
                    proc.returncode = 0
                    proc.communicate.return_value = ("", "")
                    return proc

                m_ff.side_effect = _ffmpeg
                result = dl.download(NOTE_URL, output_dir=td)
        self.assertTrue(result.file_path.endswith(".mp3"))
        self.assertEqual(result.video_id, NOTE_ID)

    def test_to_mp3_honors_cancel(self):
        import threading

        from app.exceptions.task import TaskCancelledError

        dl = XiaohongshuDownloader(auth=mock.Mock())
        ev = threading.Event()
        ev.set()

        class _Proc:
            def poll(self):
                return None

            def terminate(self):
                pass

            def kill(self):
                pass

            def wait(self, timeout=None):
                return 0

            returncode = -15

        with tempfile.TemporaryDirectory() as td:
            mp4 = Path(td) / "a.mp4"
            mp3 = Path(td) / "a.mp3"
            mp4.write_bytes(b"x")
            with mock.patch(
                "app.downloaders.xiaohongshu_downloader.subprocess.Popen",
                return_value=_Proc(),
            ):
                with self.assertRaises(TaskCancelledError):
                    dl._to_mp3(str(mp4), str(mp3), cancel_event=ev)

    def test_factory(self):
        inst = constant.get_downloader("xiaohongshu")
        self.assertIsInstance(inst, XiaohongshuDownloader)


class InspectTest(unittest.TestCase):
    def test_video(self):
        note = note_from_state(_video_state(), NOTE_ID, NOTE_URL)
        fake = mock.Mock()
        fake.fetch_note.return_value = note
        fake.close = mock.Mock()
        with mock.patch("app.downloaders.xiaohongshu_auth.XiaohongshuAuth", return_value=fake):
            out = inspect_video(NOTE_URL)
        self.assertTrue(out["ok"])
        self.assertEqual(out["platform"], "xiaohongshu")
        self.assertEqual(out["kind"], "single")
        self.assertEqual(out["entries"][0]["video_id"], NOTE_ID)

    def test_inspect_strips_xsec_token(self):
        long_url = f"{NOTE_URL}?xsec_token=SECRETTOKEN"
        note = note_from_state(_video_state(), NOTE_ID, long_url)
        fake = mock.Mock()
        fake.fetch_note.return_value = note
        fake.close = mock.Mock()
        with mock.patch("app.downloaders.xiaohongshu_auth.XiaohongshuAuth", return_value=fake):
            out = inspect_video(NOTE_URL)
        self.assertTrue(out["ok"])
        self.assertNotIn("xsec_token", out["entries"][0]["url"])
        self.assertNotIn("SECRETTOKEN", json.dumps(out))

    def test_image_rejected(self):
        note = note_from_state(_image_state(), NOTE_ID, NOTE_URL)
        fake = mock.Mock()
        fake.fetch_note.return_value = note
        fake.close = mock.Mock()
        with mock.patch("app.downloaders.xiaohongshu_auth.XiaohongshuAuth", return_value=fake):
            out = inspect_video(NOTE_URL)
        self.assertFalse(out["ok"])
        self.assertIn("不是视频", out["error"])


if __name__ == "__main__":
    unittest.main()
