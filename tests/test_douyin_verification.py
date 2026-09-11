"""回放真实卡点：check_qrconnect 返回 2046，网页要求身份验证而非 sessionid。

2026-09-10 的人工扫码诊断确认了响应错误码与「身份验证」弹窗同时出现。
只保留错误码/状态；token、Cookie、用户信息均用测试占位值，不保存真实抓包。
"""
from __future__ import annotations

import time
from unittest import mock

from app.downloaders.douyin_auth import QR_SCANNED
from app.downloaders.douyin_browser import DouyinBrowserQr
from app.services.cookie_manager import CookieConfigManager
from videonote_mcp import cli


def verification_response():
    response = mock.Mock()
    response.url = "https://www.douyin.com/passport/web/check_qrconnect/?aid=6383"
    response.status = 200
    response.request.url = response.url
    response.request.method = "POST"
    response.request.post_data = "token=test-qr&is_frontier=true"
    response.request.headers = {"content-type": "application/x-www-form-urlencoded"}
    response.json.return_value = {"data": {"error_code": 2046}}
    return response


def test_scanned_then_real_identity_verification_response_is_not_ignored():
    qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
    qr._created = {"token": "test-qr"}
    qr._status = QR_SCANNED
    qr._on_response(verification_response())
    assert qr._status == "verification_required", "手机已确认，但 2046 被忽略而仍停在 scanned"


def test_cli_waits_for_human_verification_past_qr_expiry(tmp_path, monkeypatch, capsys):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    monkeypatch.setattr(time, "sleep", lambda seconds: now.__setitem__(0, now[0] + seconds))
    manager = CookieConfigManager(str(tmp_path / "cookies.json"))
    qr = DouyinBrowserQr(cookie_mgr=manager)
    qr._page = mock.Mock()
    qr._page.evaluate.return_value = {"data": {"error_code": 2046}}
    qr._created = {"token": "test-qr", "url": "https://www.douyin.com/scan?token=test-qr"}
    qr._expires_at = 60
    qr._status = QR_SCANNED
    monkeypatch.setattr(qr, "_launch", lambda: None)
    monkeypatch.setattr(qr, "_cookies", lambda: {"sessionid": "test-session"} if now[0] >= 90 else {})

    def pump(ms):
        now[0] += ms / 1000
        if now[0] == 2:
            qr._on_response(verification_response())

    qr._page.wait_for_timeout.side_effect = pump
    monkeypatch.setattr(cli, "_open_douyin_qr_session", lambda: qr)
    monkeypatch.setattr(cli, "_print_ascii_qr", lambda *_: None)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    monkeypatch.setattr("app.downloaders.douyin_browser.verify_douyin_login", lambda **_: "")

    cli._login_douyin([], exit_on_fail=False)

    out = capsys.readouterr().out
    assert "二次验证" in out, "不能把需要电脑身份验证继续说成在手机上确认"
    assert manager.get("douyin") == "sessionid=test-session", "二维码已扫过，60 秒到期不能中断身份验证"
    assert "已保存抖音登录态" in out
    assert "等待扫码超时" not in out
    assert now[0] >= 90


def test_default_launch_displays_browser_for_manual_verification(monkeypatch):
    runner = mock.Mock()
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: runner)
    monkeypatch.setattr("app.downloaders.douyin_browser.ProxyConfigManager.get_proxy_url", lambda _: None)
    qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
    try:
        qr._launch()
        options = runner.start.return_value.chromium.launch.call_args.kwargs
        assert options["headless"] is False
        assert qr.interactive_verification is True
    finally:
        qr.close()


def test_verification_pauses_request_replay_and_only_cookies_complete_login():
    from app.downloaders.douyin_auth import QR_SUCCESS

    qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
    qr._created = {"token": "test-qr"}
    qr._page = mock.Mock()
    qr._context = mock.Mock()
    qr._context.cookies.return_value = []
    qr._on_response(verification_response())
    assert qr.poll_qr("test-qr")["status"] == "verification_required"
    qr._active_poll("test-qr")
    qr._page.evaluate.assert_not_called()
    qr._cookie_mgr.set.assert_not_called()
    assert "未取到 sessionid" in qr.persist()
    qr._cookie_mgr.set.assert_not_called()

    # 来自已完成官方身份验证的浏览器 Cookie，而不是单看手机确认/HTTP 200。
    qr._context.cookies.return_value = [
        {"name": "sessionid", "value": "test-session", "domain": ".douyin.com"},
    ]
    assert qr.poll_qr("test-qr")["status"] == QR_SUCCESS
    assert not qr.verification_required
    qr._on_response(verification_response())  # 迟到响应不能覆盖已取得的登录态。
    assert not qr.verification_required


def test_pending_verification_survives_late_poll_and_qr_refresh():
    qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
    qr._created = {"token": "test-qr"}
    qr._displayed_token = "test-qr"
    qr._on_response(verification_response())
    for status in ("new", "scanned", "confirmed", "expired"):
        resp = verification_response()
        resp.json.return_value = {"data": {"error_code": 0, "status": status}}
        qr._on_response(resp)
        assert qr.verification_required, status
    refresh = verification_response()
    refresh.url = "https://www.douyin.com/passport/web/get_qrcode/"
    refresh.json.return_value = {"data": {
        "error_code": 0, "token": "new-qr", "qrcode_index_url": "https://www.douyin.com/scan",
    }}
    qr._on_response(refresh)
    assert qr.verification_required
    assert qr._displayed_token == qr._created["token"] == "test-qr"


def test_finalize_must_not_navigate_away_from_identity_verification(monkeypatch):
    from app.downloaders.douyin_auth import QR_SUCCESS

    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
    qr._created = {"token": "test-qr"}
    qr._page = mock.Mock()
    qr._status = QR_SUCCESS
    monkeypatch.setattr(qr, "_cookies", lambda: {})

    def pump(ms):
        now[0] += ms / 1000
        qr._on_response(verification_response())

    qr._page.wait_for_timeout.side_effect = pump
    qr.finalize("https://www.douyin.com/callback")
    assert qr.verification_required
    qr._page.goto.assert_not_called()
    assert now[0] < 1


def test_cli_does_not_persist_or_close_when_finalize_discovers_verification(monkeypatch, capsys):
    from app.downloaders.douyin_auth import QR_SUCCESS

    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    qr = mock.Mock()
    qr.pumps_events = True
    qr.interactive_verification = True
    qr.verification_required = False
    qr.create_qr.return_value = {
        "token": "test-qr", "url": "https://www.douyin.com/scan", "expires_in": 60,
    }

    def poll(_):
        now[0] += 2
        if now[0] > 90:
            qr.verification_required = False
            return {"status": QR_SUCCESS}
        return {"status": "verification_required" if qr.verification_required else QR_SUCCESS}

    def finalize(_):
        qr.verification_required = now[0] < 90

    def persist():
        assert now[0] > 90
        assert not qr.verification_required
        return ""

    qr.poll_qr.side_effect = poll
    qr.finalize.side_effect = finalize
    qr.persist.side_effect = persist
    monkeypatch.setattr(cli, "_open_douyin_qr_session", lambda: qr)
    monkeypatch.setattr(cli, "_print_ascii_qr", lambda *_: None)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    cli._login_douyin([], exit_on_fail=False)
    out = capsys.readouterr().out
    assert "二次验证" in out and "已保存抖音登录态" in out
    qr.persist.assert_called_once()
    qr.close.assert_called_once()


def test_cli_verification_timeout_is_bounded_and_never_saves(monkeypatch, capsys):
    now = [0.0]
    monkeypatch.setattr(time, "monotonic", lambda: now[0])
    qr = mock.Mock()
    qr.pumps_events = True
    qr.interactive_verification = True
    qr.create_qr.return_value = {
        "token": "test-qr", "url": "https://www.douyin.com/scan", "expires_in": 60,
    }

    def poll(_):
        now[0] += 2
        return {"status": "verification_required"}

    qr.poll_qr.side_effect = poll
    monkeypatch.setattr(cli, "_open_douyin_qr_session", lambda: qr)
    monkeypatch.setattr(cli, "_print_ascii_qr", lambda *_: None)
    cli._login_douyin([], exit_on_fail=False)
    out = capsys.readouterr().out
    assert out.count("抖音要求二次验证") == 1
    assert "等待二次验证超时" in out
    assert "等待扫码超时" not in out
    assert now[0] == 302
    qr.persist.assert_not_called()
    qr.close.assert_called_once()


def test_closed_verification_browser_cancels_instead_of_retrying(monkeypatch, capsys):
    qr = DouyinBrowserQr(cookie_mgr=mock.Mock())
    qr._created = {"token": "test-qr", "url": "https://www.douyin.com/scan"}
    qr._page = mock.Mock()
    qr._page.is_closed.return_value = True
    monkeypatch.setattr(qr, "_launch", lambda: None)
    monkeypatch.setattr(qr, "_cookies", lambda: {})
    monkeypatch.setattr(cli, "_open_douyin_qr_session", lambda: qr)
    monkeypatch.setattr(cli, "_print_ascii_qr", lambda *_: None)
    qr._on_response(verification_response())
    cli._login_douyin([], exit_on_fail=False)
    out, err = capsys.readouterr()
    assert "登录窗口已关闭" in out
    assert "轮询失败" not in err
    assert qr._page is None
    qr._cookie_mgr.set.assert_not_called()


def test_http_fallback_requires_browser_instead_of_silently_waiting(monkeypatch, capsys):
    from app.downloaders.douyin_browser import BrowserQrUnavailable

    browser = mock.Mock()
    browser.create_qr.side_effect = BrowserQrUnavailable("no local browser")
    http = mock.Mock()
    http.pumps_events = False
    http.interactive_verification = False
    http.create_qr.return_value = {"token": "test-qr", "url": "https://www.douyin.com/scan"}
    http.poll_qr.return_value = {"status": "verification_required"}
    monkeypatch.setattr(cli, "_open_douyin_qr_session", lambda: browser)
    monkeypatch.setattr("app.downloaders.douyin_auth.DouyinAuth", lambda: http)
    monkeypatch.setattr(cli, "_print_ascii_qr", lambda *_: None)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    cli._login_douyin([], exit_on_fail=False)
    out, err = capsys.readouterr()
    assert "直连接口无法完成" in err
    assert "最多等待 5 分钟" not in out
    http.persist.assert_not_called()
    http.close.assert_called_once()
