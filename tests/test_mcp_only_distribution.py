"""MCP 独立分发：不安装/打包 Skills，工具协议保持兼容。"""
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest import mock

from videonote_mcp import server

REPO = Path(__file__).resolve().parents[1]


def test_skills_and_skill_commands_are_not_distributed():
    assert not (REPO / "skills").exists()
    assert not (REPO / "commands").exists()


def test_wheel_only_packages_mcp_and_pipeline():
    config = tomllib.loads((REPO / "pyproject.toml").read_text())
    assert config["tool"]["hatch"]["build"]["targets"]["wheel"]["include"] == [
        "videonote_mcp/**", "app/**",
    ]


def test_optional_plugin_only_registers_mcp():
    plugin = json.loads((REPO / ".claude-plugin/plugin.json").read_text())
    assert "skills" not in plugin
    assert "commands" not in plugin
    assert plugin["mcpServers"]["videonote"]["command"] == "uvx"
    assert plugin["mcpServers"]["videonote"]["args"] == ["videonote@latest"]


def test_health_check_keeps_legacy_field_without_skill_install_advice():
    result = json.loads(server.health_check(need_provider=False))
    assert result["skill_refresh"] == ""


def _installer(tmp_path):
    repo = tmp_path / "checkout with spaces"
    repo.mkdir()
    shutil.copy2(REPO / "install.sh", repo / "install.sh")
    cli = repo / ".venv/bin/videonote"
    cli.parent.mkdir(parents=True)
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    (cli.parent / "python").symlink_to(sys.executable)
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    uv = fakebin / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "PATH": f"{fakebin}:/usr/bin:/bin", "HOME": str(home)}
    return repo, cli, fakebin, home, env


def test_installer_registers_mcp_without_installing_or_linking_skills(tmp_path):
    repo, cli, fakebin, home, env = _installer(tmp_path)
    calls = tmp_path / "claude-args"
    env["CLAUDE_CALLS"] = str(calls)
    claude = fakebin / "claude"
    claude.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$CLAUDE_CALLS"\n')
    claude.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(repo / "install.sh")], cwd=repo, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
    )

    assert calls.read_text().splitlines() == [
        "plugin", "list", "mcp", "add", "--scope", "user", "videonote", "--", str(cli),
    ]
    assert not (home / ".claude/skills").exists()
    assert "/videonote-setup" not in result.stdout


def test_installer_without_claude_prints_standalone_json(tmp_path):
    repo, cli, _, home, env = _installer(tmp_path)
    result = subprocess.run(
        ["/bin/bash", str(repo / "install.sh")], cwd=repo, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
    )

    payload = next(line for line in result.stdout.splitlines() if line.startswith("{"))
    params = json.loads(payload)["mcpServers"]["videonote"]
    assert params == {"type": "stdio", "command": str(cli), "args": []}
    assert not (home / ".claude/skills").exists()


def test_marketplace_does_not_reference_removed_assets():
    marketplace = json.loads((REPO / ".claude-plugin/marketplace.json").read_text())
    for plugin in marketplace["plugins"]:
        assert "skills" not in plugin
        assert "commands" not in plugin
        assert plugin["source"] == "./"


def test_example_config_works_without_a_source_checkout():
    config = json.loads((REPO / "examples/mcp.example.json").read_text())
    assert config["mcpServers"]["videonote"] == {
        "type": "stdio", "command": "uvx", "args": ["videonote@latest"],
    }


def test_existing_mcp_is_not_removed_or_overwritten_by_installer(tmp_path):
    repo, _, fakebin, home, env = _installer(tmp_path)
    calls = tmp_path / "claude-args"
    env["CLAUDE_CALLS"] = str(calls)
    claude = fakebin / "claude"
    claude.write_text('#!/bin/sh\nprintf "%s\\n" "$@" >> "$CLAUDE_CALLS"\nexit 1\n')
    claude.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(repo / "install.sh")], cwd=repo, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
    )

    args = calls.read_text().splitlines()
    assert args[:4] == ["plugin", "list", "mcp", "add"]
    assert args.count("mcp") == 1
    assert "remove" not in args
    assert "install" not in args
    assert "uninstall" not in args
    assert "未自动移除或覆盖" in result.stderr
    assert not (home / ".claude/skills").exists()


def test_provider_preflight_uses_cli_instead_of_deleted_skill_command():
    with mock.patch.object(server, "_resolve_default_provider_id", return_value=None):
        ok, detail = server._preflight_provider(None)
    assert ok is False
    assert "videonote setup" in detail
    assert "/videonote-setup" not in detail


def test_installer_does_not_shadow_an_existing_plugin_mcp(tmp_path):
    repo, _, fakebin, _, env = _installer(tmp_path)
    calls = tmp_path / "claude-args"
    env["CLAUDE_CALLS"] = str(calls)
    claude = fakebin / "claude"
    claude.write_text(
        '#!/bin/sh\nprintf "%s\\n" "$@" >> "$CLAUDE_CALLS"\n'
        'if [ "$1" = plugin ] && [ "$2" = list ]; then\n'
        '  printf "videonote@videonote\\n"\n  exit 0\nfi\nexit 97\n'
    )
    claude.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(repo / "install.sh")], cwd=repo, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=True,
    )

    assert calls.read_text().splitlines() == ["plugin", "list"]
    assert "避免遮蔽插件配置" in result.stdout


def test_source_config_pins_checkout_without_personal_configuration():
    config = json.loads((REPO / "examples/mcp.source.example.json").read_text())
    assert config["mcpServers"]["videonote"] == {
        "type": "stdio",
        "command": "uv",
        "args": [
            "--directory", "/absolute/path/to/VideoNote-MCP",
            "run", "--frozen", "videonote",
        ],
    }


def test_readme_json_matches_published_and_source_examples():
    import re

    configs = [
        json.loads((REPO / name).read_text())
        for name in ("examples/mcp.example.json", "examples/mcp.source.example.json")
    ]
    for name in ("README.md", "README_EN.md"):
        blocks = re.findall(r"```json\n(.*?)\n```", (REPO / name).read_text(), re.DOTALL)
        parsed = [json.loads(block) for block in blocks]
        assert all(config in parsed for config in configs), name


def test_architecture_diagram_is_mcp_only_without_embedded_assets():
    diagram = json.loads((REPO / "docs/videonote-mcp-architecture.excalidraw").read_text())
    assert diagram["type"] == "excalidraw"
    assert not diagram.get("files")
    text = "\n".join(e.get("text", "") for e in diagram["elements"])
    assert "MCP-only" in text
    assert "Skill" not in text
    assert "skills/" not in text
    assert "douyin" in text
