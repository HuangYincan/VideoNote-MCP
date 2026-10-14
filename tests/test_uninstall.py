"""Multi-target uninstall removes registrations, preserves unrelated data."""
import os
import stat
from pathlib import Path
from unittest.mock import Mock

import pytest

from videonote_mcp import client_management as management
from videonote_mcp import mcp_clients
from videonote_mcp.mcp_clients import CLIENT_NAMES, Client, ConfigError


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    return tmp_path / "video-config"


def installed(tmp_path, key="cursor"):
    client = Client(key, tmp_path / key / ("config.toml" if mcp_clients.CLIENT_SPECS[key].toml else "mcp.json"))
    desired = {"command": "uvx", "args": ["videonote==0.2.4"], "env": {"TOKEN": "fake-uninstall-token-123"}, "type": "stdio"}
    state = mcp_clients.State(None, {}, "custom-notes", "new")
    mcp_clients.write_config(client, state, desired)
    raw, document = mcp_clients.read_config(client)
    document = client.editable_document(raw, document)
    document["theme"] = "keep"
    client.servers(document)["other"] = {"command": "other"}
    mcp_clients.save_document(client, raw, document)
    return client


def remove(client, config):
    return management.uninstall(management.prepare_uninstall(management.inspect_registration(client, config), config), config)


@pytest.mark.parametrize("key", tuple(CLIENT_NAMES))
@pytest.mark.parametrize("disabled", [False, True])
def test_uninstall_enabled_or_disabled_target_preserves_everything_else(tmp_path, config, key, disabled):
    client = installed(tmp_path, key)
    note = tmp_path / "notes/my-note.md"
    note.parent.mkdir()
    note.write_text("keep notes")
    model = tmp_path / "models/weights.bin"
    model.parent.mkdir()
    model.write_bytes(b"keep model")
    if disabled:
        management.set_enabled(management.inspect_registration(client, config), False, config)
    saved, archive_saved = remove(client, config)
    assert management.inspect_registration(client, config).status == "new"
    _, document = mcp_clients.read_config(client)
    assert document["theme"] == "keep" and client.servers(document) == {"other": {"command": "other"}}
    assert not management.archive_client(client, config).path.exists()
    assert note.read_text() == "keep notes" and model.read_bytes() == b"keep model"
    if saved:
        assert saved.is_file()
    if archive_saved:
        assert archive_saved.is_file()
        if os.name != "nt":
            assert stat.S_IMODE(archive_saved.stat().st_mode) == 0o600
    if key == "gemini-cli":
        _, enablement = client.read_enablement()
        assert "custom-notes" not in enablement


def test_gemini_native_toggle_and_uninstall_preserve_other_enablement(tmp_path, config):
    client = installed(tmp_path, "gemini-cli")
    target = client.enablement_client()
    mcp_clients.save_document(target, None, {"other": {"enabled": False}})
    management.set_enabled(management.inspect_registration(client, config), False, config)
    _, enablement = client.read_enablement()
    assert enablement == {"other": {"enabled": False}, "custom-notes": {"enabled": False}}
    assert "custom-notes" in client.servers(mcp_clients.read_config(client)[1])
    management.set_enabled(management.inspect_registration(client, config), True, config)
    assert client.read_enablement()[1] == {"other": {"enabled": False}}
    management.set_enabled(management.inspect_registration(client, config), False, config)
    remove(client, config)
    assert client.read_enablement()[1] == {"other": {"enabled": False}}


def test_uninstall_grok_preserves_comments_and_unrelated_disabled_servers(tmp_path, config):
    client = installed(tmp_path, "grok-build")
    raw, document = mcp_clients.read_config(client)
    document = client.editable_document(raw, document)
    document["disabled_mcp_servers"] = ["other", "custom-notes"]
    mcp_clients.save_document(client, raw, document)
    client.path.write_text("# keep comment\n" + client.path.read_text())
    remove(client, config)
    assert "# keep comment" in client.path.read_text()
    assert mcp_clients.read_config(client)[1]["disabled_mcp_servers"] == ["other"]


@pytest.mark.parametrize("confirmed", [False, True])
def test_multiselect_removes_only_chosen_targets_after_confirmation(tmp_path, config, confirmed, capsys):
    clients = [installed(tmp_path, key) for key in ("codex", "cursor", "opencode")]
    cursor_raw = clients[1].path.read_bytes()
    inq = Mock()
    inq.checkbox.return_value.execute.return_value = ["codex", "opencode"]
    inq.confirm.return_value.execute.return_value = confirmed
    items = [management.inspect_registration(client, config) for client in clients]
    management.manage_uninstall(items, config, inq)
    assert clients[1].path.read_bytes() == cursor_raw
    for client in (clients[0], clients[2]):
        assert management.inspect_registration(client, config).status == ("new" if confirmed else "enabled")
    assert not any(choice["enabled"] for choice in inq.checkbox.call_args.kwargs["choices"])
    assert "fake-uninstall-token-123" not in capsys.readouterr().out


@pytest.mark.parametrize("cancel", ["checkbox", "confirm", "empty"])
def test_uninstall_cancellation_never_mutates(tmp_path, config, cancel):
    client = installed(tmp_path)
    raw = client.path.read_bytes()
    inq = Mock()
    inq.checkbox.return_value.execute.return_value = [] if cancel == "empty" else ["cursor"]
    inq.confirm.return_value.execute.return_value = True
    if cancel != "empty":
        getattr(inq, cancel).return_value.execute.side_effect = KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            management.manage_uninstall([management.inspect_registration(client, config)], config, inq)
    else:
        management.manage_uninstall([management.inspect_registration(client, config)], config, inq)
        inq.confirm.assert_not_called()
    assert client.path.read_bytes() == raw and not config.exists()


def test_uninstall_new_target_does_not_create_config(tmp_path, config):
    client = Client("cursor", tmp_path / "missing/mcp.json")
    assert remove(client, config) == (None, None)
    assert not client.path.exists() and not config.exists()


@pytest.mark.parametrize("problem", ["foreign-archive", "changed-config", "changed-archive", "plugin"])
def test_uninstall_snapshot_and_ownership_guards(tmp_path, config, problem):
    client = installed(tmp_path, "codex")
    plan = management.prepare_uninstall(management.inspect_registration(client, config), config)
    if problem in ("foreign-archive", "changed-archive"):
        archive = management.archive_client(client, config)
        mcp_clients.save_document(archive, None, {"path": str(client.path) if problem == "changed-archive" else "/other/profile",
                                                 "name": "custom-notes", "entry": {"command": "uvx", "args": ["videonote"]}})
    elif problem == "changed-config":
        client.path.write_text(client.path.read_text() + "\n# concurrent change\n")
    else:
        client.path.write_text(client.path.read_text() + '\n[plugins."videonote@test"]\nenabled=true\n')
    raw = client.path.read_bytes()
    with pytest.raises(ConfigError):
        management.uninstall(plan, config)
    assert client.path.read_bytes() == raw


def test_uninstall_rejects_changed_gemini_enablement_before_removing_registration(tmp_path, config):
    client = installed(tmp_path, "gemini-cli")
    plan = management.prepare_uninstall(management.inspect_registration(client, config), config)
    mcp_clients.save_document(client.enablement_client(), None, {"custom-notes": {"enabled": False}})
    raw = client.path.read_bytes()
    with pytest.raises(ConfigError):
        management.uninstall(plan, config)
    assert client.path.read_bytes() == raw


def test_archive_cleanup_failure_reports_partial_state_and_can_retry(tmp_path, config, monkeypatch):
    client = installed(tmp_path)
    management.set_enabled(management.inspect_registration(client, config), False, config)
    management.set_enabled(management.inspect_registration(client, config), True, config)
    archive = management.archive_client(client, config).path
    original_unlink = Path.unlink

    def fail_archive(path, *args, **kwargs):
        if path == archive:
            raise OSError("cannot delete archive")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_archive)
    with pytest.raises(ConfigError, match="存档删除失败"):
        remove(client, config)
    assert archive.is_file() and "custom-notes" not in client.servers(mcp_clients.read_config(client)[1])
    monkeypatch.setattr(Path, "unlink", original_unlink)
    remove(client, config)
    assert management.inspect_registration(client, config).status == "new"


def test_uninstall_menu_dispatch(tmp_path, config, monkeypatch):
    client = installed(tmp_path)
    monkeypatch.setattr(management, "clients", lambda *_: [client])
    inq = Mock()
    inq.select.return_value.execute.side_effect = ["uninstall", "back", "back"]
    inq.checkbox.return_value.execute.return_value = ["cursor"]
    inq.confirm.return_value.execute.return_value = True
    management.wizard(config, inq)
    assert management.inspect_registration(client, config).status == "new"


def test_failed_client_write_preserves_registration_and_archive(tmp_path, config, monkeypatch):
    client = installed(tmp_path)
    management.set_enabled(management.inspect_registration(client, config), False, config)
    management.set_enabled(management.inspect_registration(client, config), True, config)
    raw = client.path.read_bytes()
    archive = management.archive_client(client, config).path
    archive_raw = archive.read_bytes()
    replace = mcp_clients.os.replace

    def fail_client(source, target):
        if target == client.path:
            raise OSError("cannot write client config")
        return replace(source, target)

    monkeypatch.setattr(mcp_clients.os, "replace", fail_client)
    with pytest.raises(OSError):
        remove(client, config)
    assert client.path.read_bytes() == raw and archive.read_bytes() == archive_raw


def test_gemini_policy_blocks_enable_but_allows_unregister(tmp_path, config):
    client = installed(tmp_path, "gemini-cli")
    raw, doc = mcp_clients.read_config(client)
    doc["mcp"] = {"excluded": ["custom-notes"]}
    mcp_clients.save_document(client, raw, doc)
    state = management.inspect_registration(client, config)
    assert state.status == "disabled"
    with pytest.raises(ConfigError, match="allowed / excluded"):
        management.set_enabled(state, True, config)
    remove(client, config)
    assert mcp_clients.read_config(client)[1]["mcp"] == {"excluded": ["custom-notes"]}


def test_jsonc_uninstall_preserves_unrelated_server_comments(tmp_path, config):
    client = Client("opencode", tmp_path / "opencode.jsonc")
    client.path.write_text('// keep header\n{ "mcp": {\n'
                           '  "notes": {"type":"local","command":["uvx","videonote"]},\n'
                           '  // keep other server comment\n'
                           '  "other": {"type":"local","command":["other"],},\n},}\n')
    remove(client, config)
    assert "// keep header" in client.path.read_text()
    assert "// keep other server comment" in client.path.read_text()
    assert mcp_clients.read_config(client)[1] == {"mcp": {"other": {"type": "local", "command": ["other"]}}}


def test_multiselect_preflight_prevents_first_removal_when_second_snapshot_changed(tmp_path, config):
    clients = [installed(tmp_path, key) for key in ("codex", "cursor")]
    raw = clients[0].path.read_bytes()
    items = [management.inspect_registration(client, config) for client in clients]
    inq = Mock()
    inq.checkbox.return_value.execute.return_value = ["codex", "cursor"]

    def confirm():
        clients[1].path.write_text(clients[1].path.read_text() + "\n")
        return True

    inq.confirm.return_value.execute.side_effect = confirm
    with pytest.raises(ConfigError):
        management.manage_uninstall(items, config, inq)
    assert clients[0].path.read_bytes() == raw
