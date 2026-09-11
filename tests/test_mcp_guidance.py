"""来源说明、版本文档入口及失败诊断的建议，不执行任何配置修改。"""
import json

import pytest

from videonote_mcp import __version__, guidance, server


def test_initialization_and_tools_expose_repository_without_skills():
    assert server.mcp.website_url == guidance.REPOSITORY_URL
    assert guidance.REPOSITORY_URL in server.mcp.instructions
    for keyword in ("health_check", "get_config", "README.md", "docs/04-使用手册.md", "server_version", "template"):
        assert keyword in server.mcp.instructions
    info = json.loads(server.get_config())["project"]
    assert info["repository"] == guidance.REPOSITORY_URL
    assert info["server_version"] == __version__
    assert "main/dev" in info["version_policy"]


@pytest.mark.parametrize("platform,expected,excluded", [
    ("darwin", "brew install ffmpeg", "winget install"),
    ("win32", "winget install", "brew install"),
    ("linux", "发行版", "brew install"),
])
def test_ffmpeg_diagnostics_are_os_aware(monkeypatch, platform, expected, excluded):
    monkeypatch.setattr(guidance.sys, "platform", platform)
    check = {"name": "ffmpeg", "ok": False, "detail": "missing"}
    guidance.add_next_steps([check])
    text = " ".join(check["next_steps"])
    assert expected in text and excluded not in text
    assert "health_check" in text
    assert check["code"] == "FFMPEG_NOT_READY"


@pytest.mark.parametrize("name", ["ffmpeg", "db", "encryption", "disk", "transcriber", "provider", "queue"])
def test_failed_check_gets_actions_healthy_check_stays_compatible(name):
    failed = {"name": name, "ok": False, "detail": "original detail"}
    healthy = {"name": name, "ok": True, "detail": "ready"}
    guidance.add_next_steps([failed, healthy])
    assert failed["next_steps"]
    assert failed["detail"] == "original detail" and failed["ok"] is False
    assert healthy == {"name": name, "ok": True, "detail": "ready"}


def test_health_returns_project_and_actions_without_changing_legacy_contract(monkeypatch):
    monkeypatch.setattr(server.shutil, "which", lambda _: None)
    result = json.loads(server.health_check())
    check = next(c for c in result["checks"] if c["name"] == "ffmpeg")
    assert not result["ok"] and not check["ok"] and check["next_steps"]
    assert result["project"]["repository"] == guidance.REPOSITORY_URL
    assert result["skill_refresh"] == ""
