"""抖音登录完整时序：按官网 POST 轮询、绑定二维码、等待 Cookie 与 CLI 截止时间。"""
from __future__ import annotations

import time
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.downloaders.douyin_auth import QR_EXPIRED, QR_SCANNED, QR_SUCCESS, QR_WAIT
from app.downloaders.douyin_browser import DouyinBrowserQr, next_qr_status
from videonote_mcp import cli


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(time, "monotonic", clock.monotonic)
    monkeypatch.setattr(time, "sleep", clock.sleep)
    return clock


def response(endpoint="check_qrconnect", *, token="tok", status="scanned"):
    resp = mock.Mock()
    resp.url = f"https://www.douyin.com/passport/web/{endpoint}/?aid=6383&a_bogus=old-signature"
    resp.request.url = resp.url
    resp.request.method = "POST"
    resp.request.post_data = f"token={token}&is_frontier=true&next=https%3A%2F%2Fwww.douyin.com&need_logo=false"
    resp.request.headers = {"content-type": "application/x-www-form-urlencoded"}
    resp.json.return_value = {"data": {"status": status, "error_code": 0}}
    return resp


def browser():
    qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
    qr._created = {"token": "tok", "url": "https://www.douyin.com/scan?token=tok"}
    qr._page = mock.Mock()
    qr._cookies = lambda: {}
    return qr


def test_active_poll_uses_observed_post_in_page_not_api_request():
    qr = browser()
    qr._on_response(response())
    qr._page.evaluate.return_value = {"data": {"status": "confirmed", "error_code": 0}}
    result = qr.poll_qr("tok")
    qr._page.request.get.assert_not_called()
    assert result["status"] == QR_SUCCESS
    options = qr._page.evaluate.call_args.args[1]
    assert options["method"] == "POST"
    assert parse_qs(options["body"])["token"] == ["tok"]
    assert parse_qs(options["body"])["is_frontier"] == ["false"]
    assert parse_qs(options["body"])["next"] == ["https://www.douyin.com"]
    assert "a_bogus" not in parse_qs(urlsplit(options["url"]).query)


def test_no_synthetic_poll_until_official_request_is_observed():
    qr = browser()
    resp = response("get_qrcode")
    resp.json.return_value = {"data": {
        "token": "tok", "qrcode_index_url": "https://www.douyin.com/scan?token=tok", "error_code": 0,
    }}
    qr._on_response(resp)
    qr.poll_qr("tok")
    qr._page.request.get.assert_not_called()
    qr._page.evaluate.assert_not_called()


def test_poll_ignores_a_different_qr_token():
    qr = browser()
    qr._on_response(response(token="other-qr", status="confirmed"))
    assert qr._status == QR_WAIT
    assert qr._redirect == ""


def test_response_filter_does_not_read_unrelated_bodies():
    qr = browser()
    resp = response("account/info")
    qr._on_response(resp)
    resp.json.assert_not_called()


def test_error_payload_cannot_confirm_login():
    qr = browser()
    resp = response(status="confirmed")
    resp.json.return_value["data"]["error_code"] = 4031
    qr._on_response(resp)
    assert qr._status != QR_SUCCESS


def test_success_is_not_overwritten_by_a_late_expiry():
    assert next_qr_status(QR_SUCCESS, QR_EXPIRED) == QR_SUCCESS


def test_finalize_waits_for_delayed_cookies_without_redirect(clock):
    qr = browser()
    qr._status = QR_SUCCESS
    qr._page.wait_for_timeout.side_effect = lambda ms: clock.sleep(ms / 1000)
    qr._cookies = lambda: {"sessionid": "test-session"} if clock.now >= 3 else {}
    qr.finalize("")
    assert clock.now >= 3
    qr._page.goto.assert_not_called()
    with mock.patch("app.downloaders.douyin_browser.verify_douyin_login", return_value=""):
        assert qr.persist() == ""


def test_finalize_waits_before_interrupting_official_callback(clock):
    qr = browser()
    qr._status = QR_SUCCESS
    qr._page.wait_for_timeout.side_effect = lambda ms: clock.sleep(ms / 1000)
    qr._cookies = lambda: {"sessionid": "test-session"} if clock.now >= 1 else {}
    qr.finalize("https://www.douyin.com/?ticket=callback")
    qr._page.goto.assert_not_called()


def test_persist_uses_final_cookies_not_an_early_snapshot():
    qr = browser()
    qr._logged_cookies = {"sessionid": "early"}
    qr._cookies = lambda: {"sessionid": "final", "ttwid": "new"}
    with mock.patch("app.downloaders.douyin_browser.verify_douyin_login", return_value=""):
        assert qr.persist() == ""
    saved = qr._cookie_mgr.set.call_args.args[1]
    assert "sessionid=final" in saved
    assert "early" not in saved


class LoginSession:
    pumps_events = True

    def __init__(self, clock, success_at=60, poll_duration=0.5):
        self.clock = clock
        self.success_at = success_at
        self.poll_duration = poll_duration
        self.closed = False
        self.saved = False

    def create_qr(self):
        return {"token": "tok", "url": "https://www.douyin.com/scan?token=tok"}

    def poll_qr(self, token):
        self.clock.sleep(self.poll_duration)
        return {"status": QR_SUCCESS if self.clock.now >= self.success_at else QR_SCANNED}

    def finalize(self, redirect_url):
        pass

    def persist(self):
        self.saved = True
        return ""

    def close(self):
        self.closed = True


@pytest.fixture
def cli_session(clock, monkeypatch):
    session = LoginSession(clock)
    monkeypatch.setattr(cli, "_open_douyin_qr_session", lambda: session)
    monkeypatch.setattr(cli, "_print_ascii_qr", lambda *_: None)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    return session


def test_cli_does_not_expire_after_90_fast_browser_polls(cli_session, capsys):
    cli._login_douyin([])
    assert cli_session.saved, f"expired after only {cli_session.clock.now} seconds"
    out, err = capsys.readouterr()
    assert "已保存抖音登录态" in out + err
    assert cli_session.closed


def test_cli_slow_polls_do_not_extend_the_three_minute_deadline(cli_session):
    cli_session.success_at = float("inf")
    cli_session.poll_duration = 3
    cli._login_douyin([])
    assert 180 <= cli_session.clock.now <= 183
    assert cli_session.closed


@pytest.mark.parametrize("failure", ["create", "invalid_url", "missing_token", "render", "cancel"])
def test_cli_always_closes_browser_even_before_polling(cli_session, monkeypatch, failure):
    if failure == "create":
        cli_session.create_qr = mock.Mock(side_effect=RuntimeError("create failed"))
    elif failure == "invalid_url":
        cli_session.create_qr = lambda: {"token": "tok", "url": "https://evil.example/"}
    elif failure == "missing_token":
        cli_session.create_qr = lambda: {"url": "https://www.douyin.com/scan"}
    elif failure == "render":
        monkeypatch.setattr(cli, "_print_ascii_qr", mock.Mock(side_effect=RuntimeError("render failed")))
    else:
        cli_session.create_qr = mock.Mock(side_effect=KeyboardInterrupt())
    try:
        cli._login_douyin([], exit_on_fail=False)
    except (RuntimeError, KeyboardInterrupt):
        pass
    assert cli_session.closed


def test_cli_does_not_report_success_for_unknown_verification_error(cli_session, capsys):
    cli_session.success_at = 0
    cli_session.persist = lambda: "HTTP 500"
    cli._login_douyin([])
    out, err = capsys.readouterr()
    assert "HTTP 500" in out + err
    assert "下载视频笔记可用了" not in out + err


def test_qrcode_expiry_comes_from_official_response(clock, monkeypatch):
    monkeypatch.setattr(time, "time", lambda: 1000)
    qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
    resp = response("get_qrcode")
    resp.json.return_value = {"data": {
        "token": "tok", "qrcode_index_url": "https://www.douyin.com/scan?token=tok",
        "error_code": 0, "expire_time": 1060,
    }}
    qr._on_response(resp)
    assert qr._expires_at == 60
    qr._page = mock.Mock()
    qr._launch = lambda: None
    clock.sleep(4)
    created = qr.create_qr()
    assert created["expires_in"] == 56


def test_cli_honors_server_ttl_and_shows_it(cli_session, capsys):
    cli_session.success_at = float("inf")
    cli_session.create_qr = lambda: {
        "token": "tok", "url": "https://www.douyin.com/scan?token=tok", "expires_in": 60,
    }
    cli._login_douyin([])
    assert 60 <= cli_session.clock.now <= 61
    out, err = capsys.readouterr()
    assert "60 秒" in out + err
    assert cli_session.closed


def test_refresh_does_not_silently_replace_the_displayed_qr():
    qr = browser()
    qr._displayed_token = "tok"
    resp = response("get_qrcode")
    resp.json.return_value = {"data": {
        "token": "new-token", "qrcode_index_url": "https://www.douyin.com/scan?token=new-token",
        "error_code": 0,
    }}
    qr._on_response(resp)
    assert qr._created["token"] == "tok"
    assert qr._status == QR_EXPIRED
    qr._on_response(response(token="new-token", status="confirmed"))
    assert qr._status == QR_EXPIRED


def test_waiting_for_missing_cookies_is_bounded_and_does_not_persist(clock):
    qr = browser()
    qr._page.wait_for_timeout.side_effect = lambda ms: clock.sleep(ms / 1000)
    qr.finalize("")
    assert clock.now == 15
    assert "未取到 sessionid" in qr.persist()
    qr._cookie_mgr.set.assert_not_called()


def test_fallback_callback_has_only_remaining_cookie_wait_budget(clock):
    qr = browser()
    qr._page.wait_for_timeout.side_effect = lambda ms: clock.sleep(ms / 1000)

    def navigate(*args, **kwargs):
        clock.sleep(kwargs["timeout"] / 1000)
        raise RuntimeError("navigation timeout")

    qr._page.goto.side_effect = navigate
    qr.finalize("https://www.douyin.com/?ticket=callback")
    assert clock.now == 15
    qr._page.goto.assert_called_once()


def test_no_replay_after_browser_callback_already_set_session():
    qr = browser()
    qr._on_response(response())
    qr._cookies = lambda: {"sessionid": "test-session"}
    assert qr.poll_qr("tok")["status"] == QR_SUCCESS
    qr._page.evaluate.assert_not_called()


def test_active_poll_errors_keep_waiting_without_logging_token(caplog):
    qr = browser()
    qr._on_response(response())
    qr._page.evaluate.side_effect = RuntimeError("fetch failed token=secret-cookie-token")
    assert qr.poll_qr("tok")["status"] == QR_SCANNED
    assert "secret-cookie-token" not in caplog.text


@pytest.mark.parametrize("method,body,headers", [
    ("POST", "token=other-token&is_frontier=true", {"content-type": "application/x-www-form-urlencoded"}),
    ("POST", '{"token":"tok"}', {"content-type": "application/json"}),
    ("DELETE", "token=tok", {}),
])
def test_unsupported_or_wrong_token_requests_are_not_replayed(method, body, headers):
    from app.downloaders.douyin_browser import build_poll_request

    req = response().request
    req.method, req.post_data, req.headers = method, body, headers
    assert build_poll_request(req, "tok") == {}


@pytest.mark.parametrize("url", [
    "https://evil.example/passport/web/check_qrconnect/?token=tok",
    "http://www.douyin.com/passport/web/check_qrconnect/?token=tok",
    "https://user:secret@www.douyin.com/passport/web/check_qrconnect/?token=tok",
    "https://www.douyin.com/passport/web/get_qrcode/?token=tok",
    "https://www.douyin.com/?next=/check_qrconnect&token=tok",
])
def test_replay_rejects_untrusted_or_non_poll_urls(url):
    from app.downloaders.douyin_browser import build_poll_request

    req = mock.Mock(url=url, method="GET", headers={})
    assert build_poll_request(req, "tok") == {}


def test_create_error_with_token_and_url_is_still_rejected():
    from app.downloaders.douyin_browser import parse_qr_create_payload

    result = parse_qr_create_payload({"data": {
        "token": "tok", "qrcode_index_url": "https://www.douyin.com/scan?token=tok",
        "error_code": 4031,
    }})
    assert not result["token"]


def test_unrelated_websocket_cannot_confirm_current_qr():
    qr = browser()
    qr._on_ws_payload('{"data":{"status":"confirmed","token":"other-token"}}')
    qr._on_ws_payload('{"data":{"status":"confirmed"}}')
    assert qr._status == QR_WAIT


def test_matching_websocket_can_confirm_current_qr():
    qr = browser()
    qr._on_ws_payload('{"data":{"status":"confirmed","token":"tok"}}')
    assert qr._status == QR_SUCCESS


def test_failed_http_fallback_closes_both_sessions(cli_session, monkeypatch):
    from app.downloaders.douyin_browser import BrowserQrUnavailable

    cli_session.create_qr = mock.Mock(side_effect=BrowserQrUnavailable("no chrome"))
    http = mock.Mock()
    http.create_qr.side_effect = RuntimeError("HTTP unavailable")
    monkeypatch.setattr("app.downloaders.douyin_auth.DouyinAuth", lambda: http)
    cli._login_douyin([], exit_on_fail=False)
    assert cli_session.closed
    http.close.assert_called_once()


@pytest.mark.parametrize("http_status", [403, 500])
def test_http_error_cannot_confirm_login(http_status):
    qr = browser()
    resp = response(status="confirmed")
    resp.status = http_status
    qr._on_response(resp)
    assert qr._status == QR_WAIT
    resp.json.assert_not_called()


def test_cookie_collection_is_scoped_to_the_main_site():
    qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
    qr._context = mock.Mock()
    qr._context.cookies.return_value = [
        {"name": "sessionid", "value": "main-site-session", "domain": ".douyin.com"},
    ]
    assert qr._cookies() == {"sessionid": "main-site-session"}
    qr._context.cookies.assert_called_once_with("https://www.douyin.com")


def test_page_closed_after_login_does_not_hide_session_cookies():
    qr = browser()
    qr._page.wait_for_timeout.side_effect = RuntimeError("page already closed")
    qr._cookies = lambda: {"sessionid": "test-session"}
    assert qr.poll_qr("tok")["status"] == QR_SUCCESS
    qr._page.wait_for_timeout.assert_not_called()
