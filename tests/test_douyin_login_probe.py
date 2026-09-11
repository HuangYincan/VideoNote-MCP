"""登录后的附加校验：无法确认不能被说成登录态无效（仅使用合成响应）。"""
from __future__ import annotations

from unittest import mock

import pytest

from app.downloaders.douyin_auth import verify_douyin_login
from app.downloaders.douyin_browser import DouyinBrowserQr
from app.services.cookie_manager import CookieConfigManager
from videonote_mcp import cli


@pytest.fixture
def cookie_mgr(tmp_path):
    return CookieConfigManager(str(tmp_path / "cookies.json"))


def probe_response(*, body=None, status=200, content_type="application/json"):
    response = mock.Mock()
    response.status_code = status
    response.headers = {"Content-Type": content_type}
    if body is None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = body
    return response


def install_probe(monkeypatch, response):
    session = mock.MagicMock()
    session.__enter__.return_value = session
    session.get.return_value = response
    monkeypatch.setattr("app.downloaders.douyin_auth.PublicOnlySession", lambda: session)
    return session


def test_cli_saved_session_unknown_probe_does_not_require_another_scan(cookie_mgr, monkeypatch, capsys):
    qr = DouyinBrowserQr(cookie_mgr=cookie_mgr)
    monkeypatch.setattr(qr, "create_qr", lambda: {
        "token": "fixture-qr", "url": "https://www.douyin.com/scan?token=fixture-qr",
    })
    monkeypatch.setattr(qr, "_cookies", lambda: {"sessionid": "fixture-only"})
    monkeypatch.setattr(cli, "_open_douyin_qr_session", lambda: qr)
    monkeypatch.setattr(cli, "_print_ascii_qr", lambda *_: None)
    monkeypatch.setattr("builtins.input", lambda *_: "")
    install_probe(monkeypatch, probe_response(content_type="text/html"))

    cli._login_douyin([], exit_on_fail=False)

    out = capsys.readouterr().out
    assert cookie_mgr.get("douyin") == "sessionid=fixture-only"
    assert "无效" not in out, "非 JSON 校验响应不足以证明刚保存的登录态无效"
    assert "请重新" not in out, "校验未知不能要求已经登录的用户重新扫码"
    assert "fixture-only" not in out
    assert "未能确认" in out
    assert "下载视频笔记可用了" not in out, "同样不能把未知结果当成确认成功"


@pytest.mark.parametrize("body", [{}, {"data": {}}, {"data": {"error_code": 2046}}])
def test_unknown_account_response_is_not_reported_as_invalid(cookie_mgr, monkeypatch, body):
    cookie_mgr.set("douyin", "sessionid=fixture-only")
    install_probe(monkeypatch, probe_response(body=body))
    result = verify_douyin_login(cookie_mgr=cookie_mgr)
    assert result and "未能确认" in result
    assert "无效" not in result
    assert "请重新" not in result


@pytest.mark.parametrize("body", [
    {"message": "success", "data": {"error_code": 2046}},
    {"error_code": 4031, "data": {"user_id": "fixture-user"}},
    {"data": {"error_code": "2046", "user_id": "fixture-user"}},
    {"message": "success", "data": {"unexpected": "fixture-only"}},
])
def test_success_message_or_identity_does_not_override_probe_error(cookie_mgr, monkeypatch, body):
    cookie_mgr.set("douyin", "sessionid=fixture-only")
    install_probe(monkeypatch, probe_response(body=body))
    result = verify_douyin_login(cookie_mgr=cookie_mgr)
    assert result and "未能确认" in result
    assert "fixture-only" not in result
    assert "fixture-user" not in result


@pytest.mark.parametrize("body", [
    {"data": {"user_id": "fixture-user"}},
    {"message": "success", "data": {"uid": "fixture-user", "error_code": 0}},
    {"error_code": "0", "data": {"user_id": "fixture-user"}},
])
def test_account_identity_still_confirms_login(cookie_mgr, monkeypatch, body):
    cookie_mgr.set("douyin", "sessionid=fixture-only")
    install_probe(monkeypatch, probe_response(body=body))
    assert verify_douyin_login(cookie_mgr=cookie_mgr) == ""


@pytest.mark.parametrize("status", [403, 429, 500])
def test_blocked_probe_is_unknown_not_invalid(cookie_mgr, monkeypatch, status):
    cookie_mgr.set("douyin", "sessionid=fixture-only")
    install_probe(monkeypatch, probe_response(status=status))
    result = verify_douyin_login(cookie_mgr=cookie_mgr)
    assert "未能确认" in result
    assert f"HTTP {status}" in result
    assert "无效" not in result
    assert "请重新" not in result


def test_unauthorized_probe_still_reports_rejected_authentication(cookie_mgr, monkeypatch):
    cookie_mgr.set("douyin", "sessionid=fixture-only")
    install_probe(monkeypatch, probe_response(status=401))
    result = verify_douyin_login(cookie_mgr=cookie_mgr)
    assert "无效" in result
    assert "fixture-only" not in result


def test_probe_diagnostics_never_echo_response_details(cookie_mgr, monkeypatch):
    cookie_mgr.set("douyin", "sessionid=fixture-only")
    install_probe(monkeypatch, probe_response(body={"data": {
        "error_code": "fixture-secret-code", "description": "fixture-only",
    }}))
    result = verify_douyin_login(cookie_mgr=cookie_mgr)
    assert result and "未能确认" in result
    assert "fixture" not in result


def test_probe_returns_safe_error_code_for_diagnosis(cookie_mgr, monkeypatch):
    cookie_mgr.set("douyin", "sessionid=fixture-only")
    install_probe(monkeypatch, probe_response(body={"data": {"error_code": 2046}}))
    result = verify_douyin_login(cookie_mgr=cookie_mgr)
    assert "error_code=2046" in result
