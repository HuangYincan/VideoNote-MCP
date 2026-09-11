"""抖音扫码登录：SSO get_qrcode / check_qrconnect / 官方域钉死（全 mock，不碰真网）。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.downloaders.douyin_auth import (
    QR_EXPIRED,
    QR_SCANNED,
    QR_SUCCESS,
    QR_WAIT,
    DouyinAuth,
    _map_qr_status,
    format_cookie,
    gen_verify_fp,
    has_sessionid,
    is_douyin_qr_url,
    is_douyin_redirect_url,
    parse_cookie_string,
    verify_douyin_login,
)
from app.services.cookie_manager import CookieConfigManager


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


class _FakeResp:
    def __init__(self, status=200, payload=None, content=b"{}", text=None, headers=None):
        self.status_code = status
        self._payload = payload
        self.content = b"{}" if payload is not None else content
        self.headers = headers or {}
        if text is not None:
            self.text = text
        elif payload is not None:
            self.text = ""
        elif isinstance(content, (bytes, bytearray)):
            self.text = bytes(content).decode("utf-8", "replace")
        else:
            self.text = str(content or "")

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeSession:
    def __init__(self, handler):
        self.headers = {}
        self.cookies = _FakeJar()
        self.handler = handler
        self.closed = False
        self.calls = []

    def get(self, url, params=None, timeout=None, **kwargs):
        self.calls.append((url, params or {}))
        return self.handler(url, params)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _qr_payload(token="tok-1", url="https://www.douyin.com/scan?token=tok-1"):
    return {
        "error_code": 0,
        "message": "success",
        "data": {"token": token, "qrcode_index_url": url},
    }


class UrlAndCookieHelpersTest(unittest.TestCase):
    def test_qr_and_redirect_hosts(self):
        self.assertTrue(is_douyin_qr_url("https://www.douyin.com/scan?token=x"))
        self.assertTrue(is_douyin_qr_url("https://aweme.snssdk.com/service/2/web/redirect/"))
        self.assertTrue(
            is_douyin_qr_url(
                "https://api.amemv.com/ucenter_web/app/aweme/scan_login/index.html?t=1"
            )
        )
        self.assertFalse(is_douyin_qr_url("https://evil.com/scan"))
        self.assertFalse(is_douyin_qr_url("https://douyin.com.evil.com/scan"))
        self.assertFalse(is_douyin_qr_url("https://amemv.com.evil.com/scan"))
        self.assertTrue(is_douyin_redirect_url("https://sso.douyin.com/login/callback"))
        self.assertFalse(is_douyin_redirect_url("https://evil.example/redirect"))
        self.assertFalse(is_douyin_redirect_url("https://api.amemv.com/callback"))

    def test_parse_and_sessionid(self):
        parsed = parse_cookie_string("sessionid=abc; ttwid=tt; sessionid_ss=ss")
        self.assertEqual(parsed["sessionid"], "abc")
        self.assertTrue(has_sessionid(parsed))
        self.assertTrue(has_sessionid({"sessionid_ss": "only-ss"}))
        self.assertFalse(has_sessionid({"ttwid": "tt"}))
        self.assertEqual(format_cookie({"a": "1", "b": ""}), "a=1")

    def test_verify_fp_shape(self):
        fp = gen_verify_fp()
        self.assertTrue(fp.startswith("verify_"))
        self.assertGreaterEqual(fp.count("_"), 5)

    def test_map_qr_status(self):
        self.assertEqual(_map_qr_status({"status": "1"}), QR_WAIT)
        self.assertEqual(_map_qr_status({"status": "new"}), QR_WAIT)
        self.assertEqual(_map_qr_status({"status": "2"}), QR_SCANNED)
        self.assertEqual(_map_qr_status({"status": "scanned"}), QR_SCANNED)
        self.assertEqual(_map_qr_status({"status": "3"}), QR_SUCCESS)
        self.assertEqual(_map_qr_status({"status": "confirmed"}), QR_SUCCESS)
        self.assertEqual(
            _map_qr_status({"status": "4", "redirect_url": "https://www.douyin.com/"}),
            QR_SUCCESS,
        )
        self.assertEqual(_map_qr_status({"redirect_url": "https://www.douyin.com/"}), QR_SUCCESS)
        self.assertEqual(_map_qr_status({"status": "5"}), QR_EXPIRED)
        self.assertEqual(_map_qr_status({"status": "expired"}), QR_EXPIRED)
        self.assertEqual(_map_qr_status({"status": "4"}), QR_EXPIRED)
        self.assertEqual(_map_qr_status({"status": "refused"}), QR_EXPIRED)


class DouyinAuthQrTest(unittest.TestCase):
    def test_create_qr_returns_official_url(self):
        def handler(url, params):
            if "get_qrcode" in url:
                return _FakeResp(payload=_qr_payload())
            return _FakeResp(payload={})

        auth = DouyinAuth(session=_FakeSession(handler))
        created = auth.create_qr()
        self.assertEqual(created["token"], "tok-1")
        self.assertIn("douyin.com", created["url"])

    def test_create_qr_rejects_foreign_host(self):
        def handler(url, params):
            if "get_qrcode" in url:
                return _FakeResp(payload=_qr_payload(url="https://evil.example/qr"))
            return _FakeResp(payload={})

        auth = DouyinAuth(session=_FakeSession(handler))
        with self.assertRaises(RuntimeError) as ei:
            auth.create_qr()
        self.assertIn("官方域名", str(ei.exception))

    def test_create_qr_missing_token(self):
        def handler(url, params):
            if "get_qrcode" in url:
                return _FakeResp(payload={"error_code": 1, "message": "风控", "data": {}})
            return _FakeResp(payload={})

        auth = DouyinAuth(session=_FakeSession(handler))
        with self.assertRaises(RuntimeError) as ei:
            auth.create_qr()
        self.assertIn("风控", str(ei.exception))

    def test_create_qr_html_waf_mentions_chrome(self):
        html = "<!doctype html><html><head><script>gfkadpd</script></head></html>"

        def handler(url, params):
            if "get_qrcode" in url:
                return _FakeResp(
                    status=200,
                    payload=None,
                    content=html.encode(),
                    text=html,
                    headers={"content-type": "text/html; charset=utf-8"},
                )
            return _FakeResp(payload={})

        auth = DouyinAuth(session=_FakeSession(handler))
        with self.assertRaises(RuntimeError) as ei:
            auth.create_qr()
        msg = str(ei.exception)
        self.assertIn("风控页", msg)
        self.assertIn("Chrome", msg)
        self.assertNotIn("HTTP 200", msg)

    def test_create_qr_risk_4031_mentions_chrome(self):
        def handler(url, params):
            if "get_qrcode" in url:
                return _FakeResp(
                    payload={
                        "message": "error",
                        "data": {
                            "error_code": 4031,
                            "description": "您正在尝试访问的网站存在安全风险",
                        },
                    }
                )
            return _FakeResp(payload={})

        auth = DouyinAuth(session=_FakeSession(handler))
        with self.assertRaises(RuntimeError) as ei:
            auth.create_qr()
        msg = str(ei.exception)
        self.assertIn("安全风险", msg)
        self.assertIn("Chrome", msg)

    def test_poll_scanned_then_success(self):
        payloads = [
            {"data": {"status": "1"}},
            {"data": {"status": "2"}},
            {
                "data": {
                    "status": "3",
                    "redirect_url": "https://www.douyin.com/?ticket=1",
                }
            },
        ]

        def handler(url, params):
            if "check_qrconnect" in url:
                return _FakeResp(payload=payloads.pop(0))
            return _FakeResp(payload={})

        auth = DouyinAuth(session=_FakeSession(handler))
        self.assertEqual(auth.poll_qr("tok").get("status"), QR_WAIT)
        self.assertEqual(auth.poll_qr("tok").get("status"), QR_SCANNED)
        poll = auth.poll_qr("tok")
        self.assertEqual(poll["status"], QR_SUCCESS)
        self.assertTrue(poll["redirect_url"].startswith("https://www.douyin.com"))

    def test_poll_expired(self):
        def handler(url, params):
            return _FakeResp(payload={"data": {"status": "5"}})

        auth = DouyinAuth(session=_FakeSession(handler))
        self.assertEqual(auth.poll_qr("tok")["status"], QR_EXPIRED)

    def test_finalize_rejects_foreign_host(self):
        auth = DouyinAuth(session=_FakeSession(lambda *a: _FakeResp(payload={})))
        with self.assertRaises(ValueError) as ei:
            auth.finalize("https://evil.example/callback")
        self.assertIn("官方", str(ei.exception))

    def test_finalize_empty_is_noop(self):
        session = _FakeSession(lambda *a: _FakeResp(payload={}))
        auth = DouyinAuth(session=session)
        auth.finalize("")
        self.assertEqual(session.calls, [])

    def test_persist_saves_sessionid_without_exposing_it(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = CookieConfigManager(filepath=str(Path(td) / "downloader.json"))
            jar = _FakeJar({"sessionid": "sess-secret", "ttwid": "tt"})

            def handler(url, params):
                if "account/info" in url:
                    return _FakeResp(payload={"data": {"user_id": "123", "name": "u"}})
                return _FakeResp(payload={})

            session = _FakeSession(handler)
            session.cookies = jar
            auth = DouyinAuth(session=session, cookie_mgr=mgr)
            with mock.patch("app.downloaders.douyin_auth.PublicOnlySession", return_value=session):
                err = auth.persist()
            self.assertEqual(err, "")
            saved = mgr.get("douyin") or ""
            self.assertIn("sessionid=sess-secret", saved)
            self.assertNotIn("sess-secret", str(err))

    def test_persist_missing_sessionid(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = CookieConfigManager(filepath=str(Path(td) / "downloader.json"))
            auth = DouyinAuth(session=_FakeSession(lambda *a: _FakeResp()), cookie_mgr=mgr)
            self.assertIn("未取到 sessionid", auth.persist())
            self.assertFalse(mgr.get("douyin"))

    def test_close_does_not_close_injected_session(self):
        session = _FakeSession(lambda *a: _FakeResp())
        auth = DouyinAuth(session=session)
        auth.close()
        self.assertFalse(session.closed)


class VerifyDouyinLoginTest(unittest.TestCase):
    def test_missing_cookie(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = CookieConfigManager(filepath=str(Path(td) / "downloader.json"))
            self.assertIn("未配置", verify_douyin_login(cookie_mgr=mgr))

    def test_ok(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = CookieConfigManager(filepath=str(Path(td) / "downloader.json"))
            mgr.set("douyin", "sessionid=abc; ttwid=tt")
            session = _FakeSession(
                lambda url, params: _FakeResp(payload={"data": {"user_id": "1", "name": "u"}})
            )
            with mock.patch("app.downloaders.douyin_auth.PublicOnlySession", return_value=session):
                self.assertEqual(verify_douyin_login(cookie_mgr=mgr), "")

    def test_unauthorized(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = CookieConfigManager(filepath=str(Path(td) / "downloader.json"))
            mgr.set("douyin", "sessionid=abc")
            session = _FakeSession(lambda url, params: _FakeResp(status=401, payload={}))
            with mock.patch("app.downloaders.douyin_auth.PublicOnlySession", return_value=session):
                err = verify_douyin_login(cookie_mgr=mgr)
            self.assertIn("无效", err)
            self.assertNotIn("abc", err)


class DouyinBrowserQrParseTest(unittest.TestCase):
    def test_parse_create_official_url(self):
        from app.downloaders.douyin_browser import parse_qr_create_payload

        out = parse_qr_create_payload({
            "data": {
                "token": "tok-1",
                "qrcode_index_url": (
                    "https://api.amemv.com/ucenter_web/app/aweme/scan_login/index.html"
                ),
                "error_code": 0,
            }
        })
        self.assertEqual(out["token"], "tok-1")
        self.assertIn("amemv.com", out["url"])

    def test_parse_create_drops_risk_payload(self):
        from app.downloaders.douyin_browser import parse_qr_create_payload

        out = parse_qr_create_payload({
            "data": {"error_code": 4031, "description": "安全风险"},
        })
        self.assertEqual(out["token"], "")
        self.assertEqual(out["url"], "")

    def test_cookies_filter_domain(self):
        from app.downloaders.douyin_browser import cookies_from_playwright

        raw = [
            {"name": "sessionid", "value": "s1", "domain": ".douyin.com"},
            {"name": "other", "value": "nope", "domain": ".example.com"},
            {"name": "ttwid", "value": "tt", "domain": "www.douyin.com"},
            {"name": "evil", "value": "x", "domain": "douyin.com.evil.com"},
            {"name": "evil2", "value": "y", "domain": "evildouyin.com"},
        ]
        out = cookies_from_playwright(raw)
        self.assertEqual(out["sessionid"], "s1")
        self.assertEqual(out["ttwid"], "tt")
        self.assertNotIn("other", out)
        self.assertNotIn("evil", out)
        self.assertNotIn("evil2", out)

    def test_on_response_ignores_non_douyin_host(self):
        from app.downloaders.douyin_browser import DouyinBrowserQr

        qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
        fake = mock.Mock()
        fake.url = "https://evil.example/passport/web/get_qrcode/"
        fake.json.return_value = {
            "data": {
                "token": "x",
                "qrcode_index_url": "https://evil.example/phish",
                "error_code": 0,
            }
        }
        qr._on_response(fake)
        self.assertEqual(qr._created, {})

    def test_on_response_rejects_foreign_qr_url(self):
        from app.downloaders.douyin_browser import DouyinBrowserQr

        qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
        fake = mock.Mock()
        fake.url = "https://www.douyin.com/passport/web/get_qrcode/?aid=6383"
        fake.json.return_value = {
            "data": {
                "token": "tok",
                "qrcode_index_url": "https://evil.example/phish",
                "error_code": 0,
            }
        }
        qr._on_response(fake)
        self.assertEqual(qr._created, {})

    def test_on_response_accepts_amemv_qr(self):
        from app.downloaders.douyin_browser import DouyinBrowserQr

        qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
        fake = mock.Mock()
        fake.url = "https://login.douyin.com/passport/web/get_qrcode/?aid=6383"
        fake.json.return_value = {
            "data": {
                "token": "tok",
                "qrcode_index_url": (
                    "https://api.amemv.com/ucenter_web/app/aweme/scan_login/index.html"
                ),
                "error_code": 0,
            }
        }
        qr._on_response(fake)
        self.assertEqual(qr._created["token"], "tok")
        self.assertIn("amemv.com", qr._created["url"])

    def test_on_response_maps_passport_status(self):
        from app.downloaders.douyin_browser import DouyinBrowserQr

        qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
        fake = mock.Mock()
        fake.url = "https://www.douyin.com/passport/web/check_qrconnect/?token=t"
        fake.json.return_value = {"data": {"status": "scanned", "error_code": 0}}
        qr._on_response(fake)
        self.assertEqual(qr._status, QR_SCANNED)
        fake.json.return_value = {
            "data": {"status": "confirmed", "redirect_url": "https://www.douyin.com/"}
        }
        qr._on_response(fake)
        self.assertEqual(qr._status, QR_SUCCESS)
        self.assertEqual(qr._redirect, "https://www.douyin.com/")

    def test_on_response_does_not_downgrade_scanned_to_new(self):
        from app.downloaders.douyin_browser import DouyinBrowserQr

        qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
        fake = mock.Mock()
        fake.url = "https://www.douyin.com/passport/web/check_qrconnect/?token=t"
        fake.json.return_value = {"data": {"status": "scanned"}}
        qr._on_response(fake)
        fake.json.return_value = {"data": {"status": "new"}}
        qr._on_response(fake)
        self.assertEqual(qr._status, QR_SCANNED)

    def test_poll_request_disables_frontier_for_legacy_get(self):
        from app.downloaders.douyin_browser import build_poll_request

        request = mock.Mock(
            url="https://login.douyin.com/passport/web/check_qrconnect/?token=tok-1&is_frontier=1",
            method="GET", headers={},
        )
        options = build_poll_request(request, "tok-1")
        self.assertEqual(options["method"], "GET")
        self.assertIn("token=tok-1", options["url"])
        self.assertIn("is_frontier=false", options["url"])
        self.assertTrue(options["url"].startswith("https://login.douyin.com/"))

    def test_poll_request_rejects_foreign_host(self):
        from app.downloaders.douyin_browser import build_poll_request

        request = mock.Mock(
            url="https://evil.example/passport/web/check_qrconnect/?token=tok",
            method="GET", headers={},
        )
        self.assertEqual(build_poll_request(request, "tok"), {})

    def test_poll_qr_actively_fetches_confirmed(self):
        from app.downloaders.douyin_browser import DouyinBrowserQr

        qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
        qr._status = QR_SCANNED
        qr._created = {"token": "tok"}
        qr._on_request(mock.Mock(
            url="https://www.douyin.com/passport/web/check_qrconnect/?aid=6383",
            method="POST", headers={"content-type": "application/x-www-form-urlencoded"},
            post_data="token=tok&is_frontier=true",
        ))
        qr._page = mock.Mock()
        qr._page.evaluate.return_value = {
            "data": {
                "status": "confirmed",
                "redirect_url": "https://www.douyin.com/?ticket=1",
            }
        }
        qr._cookies = lambda: {}
        poll = qr.poll_qr("tok")
        self.assertEqual(poll["status"], QR_SUCCESS)
        self.assertIn("www.douyin.com", poll["redirect_url"])
        options = qr._page.evaluate.call_args.args[1]
        self.assertIn("check_qrconnect", options["url"])
        self.assertEqual(options["method"], "POST")
        self.assertIn("is_frontier=false", options["body"])
        self.assertIn("token=tok", options["body"])

    def test_ws_payload_confirmed_is_success(self):
        from app.downloaders.douyin_browser import DouyinBrowserQr

        qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
        qr._status = QR_SCANNED
        qr._on_ws_payload('{"data":{"status":"confirmed","redirect_url":"https://www.douyin.com/"}}')
        self.assertEqual(qr._status, QR_SUCCESS)

    def test_poll_sessionid_is_success(self):
        from app.downloaders.douyin_browser import DouyinBrowserQr

        with tempfile.TemporaryDirectory() as td:
            mgr = CookieConfigManager(filepath=str(Path(td) / "downloader.json"))
            qr = DouyinBrowserQr(cookie_mgr=mgr)
            qr._page = None
            qr._cookies = lambda: {"sessionid": "sess", "ttwid": "tt"}
            poll = qr.poll_qr("tok")
            self.assertEqual(poll["status"], QR_SUCCESS)
            session = _FakeSession(
                lambda url, params: _FakeResp(payload={"data": {"user_id": "1", "name": "u"}})
            )
            with mock.patch(
                "app.downloaders.douyin_auth.PublicOnlySession", return_value=session
            ):
                self.assertEqual(qr.persist(), "")
            saved = mgr.get("douyin") or ""
            self.assertIn("sessionid=sess", saved)

    def test_create_qr_failure_closes(self):
        from app.downloaders.douyin_browser import DouyinBrowserQr

        qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
        closed = []
        qr.close = lambda: closed.append(1)
        qr._launch = lambda: None
        qr._page = mock.Mock()
        qr._page.goto.side_effect = RuntimeError("打开失败")
        with self.assertRaises(RuntimeError):
            qr.create_qr()
        self.assertTrue(closed)

    def test_finalize_rejects_foreign_host(self):
        from app.downloaders.douyin_browser import DouyinBrowserQr

        qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
        qr._page = mock.Mock()
        qr._cookies = lambda: {}
        with self.assertRaises(ValueError) as ei:
            qr.finalize("https://evil.example/cb")
        self.assertIn("官方", str(ei.exception))
        self.assertIn("evil.example", str(ei.exception))
