"""Client toggles and terminal interaction using isolated config snapshots."""
import json
import os
import stat
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from videonote_mcp import client_management as management
from videonote_mcp import installer, mcp_clients, terminal_ui
from videonote_mcp.hardware import Hardware
from videonote_mcp.mcp_clients import Client, ConfigError


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("VIDEONOTE_CONFIG_DIR", str(tmp_path / "config"))
    return tmp_path / "config"


@pytest.fixture
def entry():
    return {"command": "uvx", "args": ["videonote==0.2.4"],
            "env": {"PRIVATE_TOKEN": "secret", "CUSTOM": "keep"}, "timeout": 61}


def json_client(tmp_path, entry, key="claude-code"):
    client = Client(key, tmp_path / key / "mcp.json")
    client.path.parent.mkdir(parents=True, exist_ok=True)
    document = {"theme": "dark", "mcpServers": {"my-notes": entry, "other": {"command": "other"}}}
    client.path.write_text(json.dumps(document), encoding="utf-8")
    return client, document


def inquirer(checked=(), confirmed=True):
    inq = Mock()
    inq.checkbox.return_value.execute.return_value = list(checked)
    inq.confirm.return_value.execute.return_value = confirmed
    return inq


@pytest.mark.parametrize("key", ["claude-code", "cursor", "claude-desktop"])
def test_json_disable_and_restore_preserve_exact_registration(tmp_path, config, entry, key):
    client, original = json_client(tmp_path, entry, key)
    state = management.inspect_registration(client, config)
    saved = management.set_enabled(state, False, config)
    assert json.loads(saved.read_text()) == original
    assert json.loads(client.path.read_text()) == {
        "theme": "dark", "mcpServers": {"other": {"command": "other"}}}
    archive = management.archive_client(client, config).path
    assert json.loads(archive.read_text())["entry"] == entry
    if os.name != "nt":
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
        assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700
    state = management.inspect_registration(client, config)
    assert state.status == "disabled" and state.name == "my-notes"
    management.set_enabled(state, True, config)
    assert json.loads(client.path.read_text()) == original
    assert management.inspect_registration(client, config).status == "enabled"


def test_codex_toggle_preserves_comments_and_options(tmp_path, config):
    client = Client("codex", tmp_path / "config.toml")
    client.path.write_text('# user comment\nmodel = "custom"\n[mcp_servers.notes]\n'
                           'command = "uvx" # runner comment\nargs = ["videonote==0.2.4"]\n'
                           'startup_timeout_sec = 60\n[mcp_servers.notes.env]\nTOKEN = "secret"\n'
                           '[mcp_servers.other]\ncommand = "other"\n', encoding="utf-8")
    original = tomllib.loads(client.path.read_text())
    for enabled in (False, True):
        state = management.inspect_registration(client, config)
        management.set_enabled(state, enabled, config)
        text = client.path.read_text()
        document = tomllib.loads(text)
        assert "# user comment" in text and "# runner comment" in text
        assert document["mcp_servers"]["notes"].pop("enabled") is enabled
        assert document == original
    assert not (config / "client_registrations").exists()


def test_checkbox_can_disable_claude_and_keep_codex(tmp_path, config, entry, capsys):
    claude, _ = json_client(tmp_path, entry)
    codex = Client("codex", tmp_path / "config.toml")
    codex.path.write_text('[mcp_servers.videonote]\ncommand="uvx"\nargs=["videonote@latest"]\n')
    codex_raw = codex.path.read_bytes()
    items = [management.inspect_registration(client, config) for client in (claude, codex)]
    inq = inquirer(["codex"])
    management.manage_enabled(items, config, inq)
    assert management.inspect_registration(claude, config).status == "disabled"
    assert codex.path.read_bytes() == codex_raw
    assert all(choice["enabled"] for choice in inq.checkbox.call_args.kwargs["choices"])
    assert "secret" not in capsys.readouterr().out


@pytest.mark.parametrize("confirmed", [False, True])
def test_empty_selection_requires_confirmation(tmp_path, config, entry, confirmed):
    client, _ = json_client(tmp_path, entry)
    raw = client.path.read_bytes()
    management.manage_enabled([management.inspect_registration(client, config)], config,
                              inquirer([], confirmed))
    assert (client.path.read_bytes() != raw) is confirmed
    if not confirmed:
        assert not config.exists()


def test_cancel_checkbox_never_writes(tmp_path, config, entry):
    client, _ = json_client(tmp_path, entry)
    raw = client.path.read_bytes()
    inq = inquirer()
    inq.checkbox.return_value.execute.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        management.manage_enabled([management.inspect_registration(client, config)], config, inq)
    assert client.path.read_bytes() == raw and not config.exists()


def test_idempotent_toggle_does_not_create_backups(tmp_path, config, entry):
    client, _ = json_client(tmp_path, entry)
    state = management.inspect_registration(client, config)
    assert management.set_enabled(state, True, config) is None
    assert not list(client.path.parent.glob("*.bak")) and not config.exists()


def test_current_registration_takes_priority_over_old_archive(tmp_path, config, entry):
    client, _ = json_client(tmp_path, entry)
    management.set_enabled(management.inspect_registration(client, config), False, config)
    updated = {**entry, "args": ["videonote==0.2.5"], "timeout": 99}
    client.path.write_text(json.dumps({"mcpServers": {"new-alias": updated}}))
    state = management.inspect_registration(client, config)
    assert state.status == "enabled" and state.entry == updated
    management.set_enabled(state, False, config)
    management.set_enabled(management.inspect_registration(client, config), True, config)
    assert json.loads(client.path.read_text())["mcpServers"] == {"new-alias": updated}


@pytest.mark.parametrize("problem", ["wrong-path", "occupied-alias", "bad-entry", "malformed"])
def test_restore_rejects_invalid_archive_or_occupied_alias(tmp_path, config, entry, problem):
    client, _ = json_client(tmp_path, entry)
    management.set_enabled(management.inspect_registration(client, config), False, config)
    archive_path = management.archive_client(client, config).path
    archive = json.loads(archive_path.read_text())
    if problem == "wrong-path":
        archive["path"] = str(tmp_path / "other-profile.json")
    elif problem == "bad-entry":
        archive["entry"] = {"command": "other"}
    elif problem == "occupied-alias":
        document = json.loads(client.path.read_text())
        document["mcpServers"]["my-notes"] = {"command": "other"}
        client.path.write_text(json.dumps(document))
    archive_path.write_text("broken" if problem == "malformed" else json.dumps(archive))
    raw = client.path.read_bytes()
    with pytest.raises(ConfigError):
        state = management.inspect_registration(client, config)
        management.set_enabled(state, True, config)
    assert client.path.read_bytes() == raw


@pytest.mark.parametrize("problem", ["malformed", "duplicate", "conflict", "plugin"])
def test_unmanaged_registration_is_never_overwritten(tmp_path, config, entry, problem):
    client, document = json_client(tmp_path, entry)
    if problem == "duplicate":
        document["mcpServers"]["another-copy"] = entry
    elif problem == "conflict":
        document["mcpServers"]["videonote"] = {"command": "other"}
    elif problem == "plugin":
        settings = tmp_path / "claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text('{"enabledPlugins":{"videonote@test":true}}')
    client.path.write_text("{bad" if problem == "malformed" else json.dumps(document))
    raw = client.path.read_bytes()
    with pytest.raises(ConfigError):
        state = management.inspect_registration(client, config)
        management.set_enabled(state, False, config)
    assert client.path.read_bytes() == raw and not config.exists()


def test_concurrent_client_edit_stops_before_archiving(tmp_path, config, entry):
    client, _ = json_client(tmp_path, entry)
    state = management.inspect_registration(client, config)
    client.path.write_text('{"newSetting":true}')
    with pytest.raises(ConfigError, match="发生变化"):
        management.set_enabled(state, False, config)
    assert client.path.read_text() == '{"newSetting":true}' and not config.exists()


def test_concurrent_archive_edit_stops_restore(tmp_path, config, entry):
    client, _ = json_client(tmp_path, entry)
    management.set_enabled(management.inspect_registration(client, config), False, config)
    state = management.inspect_registration(client, config)
    archive = management.archive_client(client, config).path
    archive.write_text(archive.read_text() + "\n")
    raw = client.path.read_bytes()
    with pytest.raises(ConfigError):
        management.set_enabled(state, True, config)
    assert client.path.read_bytes() == raw


def test_archive_write_failure_keeps_active_registration(tmp_path, config, entry, monkeypatch):
    client, _ = json_client(tmp_path, entry)
    state = management.inspect_registration(client, config)
    raw = client.path.read_bytes()
    monkeypatch.setattr(management, "save_document", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError):
        management.set_enabled(state, False, config)
    assert client.path.read_bytes() == raw


def test_failed_client_save_keeps_registration_and_recoverable_archive(tmp_path, config, entry, monkeypatch):
    client, _ = json_client(tmp_path, entry)
    state = management.inspect_registration(client, config)
    raw = client.path.read_bytes()
    replace = mcp_clients.os.replace

    def fail_client(source, target):
        if target == client.path:
            raise OSError("write failed")
        return replace(source, target)

    monkeypatch.setattr(mcp_clients.os, "replace", fail_client)
    with pytest.raises(OSError):
        management.set_enabled(state, False, config)
    assert client.path.read_bytes() == raw
    assert json.loads(management.archive_client(client, config).path.read_text())["entry"] == entry


def test_management_left_returns_without_writes(tmp_path, config, entry, monkeypatch):
    client, _ = json_client(tmp_path, entry)
    monkeypatch.setattr(management, "clients", lambda *_: [client])
    inq = inquirer()
    inq.select.return_value.execute.side_effect = KeyboardInterrupt
    management.wizard(config, inq)
    assert not config.exists()


def test_setup_install_reuses_installer_with_explicit_targets(tmp_path, config, monkeypatch):
    candidates = [Client("cursor", tmp_path / "cursor.json"), Client("codex", tmp_path / "config.toml")]
    monkeypatch.setattr(management, "clients", lambda *_: candidates)
    install = Mock(return_value=0)
    monkeypatch.setattr(installer, "main", install)
    inq = inquirer(["cursor", "codex"])
    inq.select.return_value.execute.side_effect = ["install", "back", "back"]
    management.wizard(config, inq)
    install.assert_called_once_with(["--yes", "--replace", "--asr", "keep", "--no-download",
                                    "--client", "cursor", "--client", "codex"])


def test_installer_can_select_multiple_detected_and_manual_targets(tmp_path, monkeypatch):
    candidates = [Client("codex", tmp_path / "config.toml", detected=True),
                  Client("cursor", tmp_path / "cursor.json", detected=False)]
    inq = inquirer(["codex", "cursor"])
    monkeypatch.setattr(terminal_ui, "prompts", lambda: inq)
    assert installer.choose_clients(candidates) == candidates
    choices = inq.checkbox.call_args.kwargs["choices"]
    assert "已检测到" in choices[0]["name"] and "未检测到" in choices[1]["name"]
    assert not any(choice["enabled"] for choice in choices)


@pytest.mark.parametrize("supported,answer,expected", [(True, "auto", "mlx-whisper"), (False, "cpu", "fast-whisper")])
def test_asr_ui_recommends_supported_backend_and_selects_model(monkeypatch, supported, answer, expected):
    inq = inquirer()
    inq.select.return_value.execute.side_effect = [answer, "medium"]
    monkeypatch.setattr(terminal_ui, "prompts", lambda: inq)
    hardware = Hardware("darwin" if supported else "linux", "arm64", supported, False, False, "test hardware")
    options = SimpleNamespace(asr=None, model=None)
    assert installer.choose_asr(options, hardware, {}, {}, True) == (expected, "medium", "cpu", False)
    choices = inq.select.call_args_list[0].kwargs["choices"]
    assert any(choice["value"] == "mlx" for choice in choices) is supported
    assert "自动推荐" in choices[0]["name"]


def test_installer_cancel_stops_before_runtime_or_registration(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(installer, "clients", lambda *_: [Client("cursor", tmp_path / "cursor.json")])
    inq = inquirer()
    inq.checkbox.return_value.execute.side_effect = KeyboardInterrupt
    monkeypatch.setattr(terminal_ui, "prompts", lambda: inq)
    action = Mock()
    monkeypatch.setattr(installer, "runtime_action", action)
    assert installer.main([]) == 130
    action.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_text_fallback_checkbox_handles_multiple_and_empty_selection(monkeypatch):
    monkeypatch.setattr(terminal_ui, "prompts", lambda: None)
    choices = [{"name": "Claude", "value": "claude", "enabled": True},
               {"name": "Codex", "value": "codex", "enabled": False}]
    answers = iter(["3", "1,2", "0", ""])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))
    assert terminal_ui.checkbox("targets", choices) == ["claude", "codex"]
    assert terminal_ui.checkbox("targets", choices) == []
    assert terminal_ui.checkbox("targets", choices) == ["claude"]


def test_real_checkbox_space_arrows_and_enter_select_multiple():
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(" \x1b[B \r")
        assert terminal_ui.checkbox("选择平台", [
            {"name": "Claude Code", "value": "claude-code", "enabled": False},
            {"name": "Codex", "value": "codex", "enabled": False},
        ]) == ["claude-code", "codex"]


@pytest.mark.parametrize("key", ["\x1b[D", "\x03"])
def test_real_checkbox_left_and_control_c_cancel(key):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(key)
        with pytest.raises(KeyboardInterrupt):
            terminal_ui.checkbox("选择平台", [{"name": "Codex", "value": "codex", "enabled": True}])
