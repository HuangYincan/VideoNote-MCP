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
    def __init__(self, status=200, payload=None, content=b"{}"):
        self.status_code = status
        self._payload = payload
        self.content = b"{}" if payload is not None else content

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
        self.assertFalse(is_douyin_qr_url("https://evil.com/scan"))
        self.assertFalse(is_douyin_qr_url("https://douyin.com.evil.com/scan"))
        self.assertTrue(is_douyin_redirect_url("https://sso.douyin.com/login/callback"))
        self.assertFalse(is_douyin_redirect_url("https://evil.example/redirect"))

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
        self.assertEqual(_map_qr_status({"status": "2"}), QR_SCANNED)
        self.assertEqual(_map_qr_status({"status": "3"}), QR_SUCCESS)
        self.assertEqual(
            _map_qr_status({"status": "4", "redirect_url": "https://www.douyin.com/"}),
            QR_SUCCESS,
        )
        self.assertEqual(_map_qr_status({"redirect_url": "https://www.douyin.com/"}), QR_SUCCESS)
        self.assertEqual(_map_qr_status({"status": "5"}), QR_EXPIRED)


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
