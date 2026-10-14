"""Installer contracts: isolated homes, no real clients/network/model downloads."""
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from videonote_mcp import (
    __version__,
    hardware,
    installer,
    installer_runtime,
    mcp_clients,
)
from videonote_mcp.hardware import Hardware
from videonote_mcp.mcp_clients import Client, ConfigError


@pytest.fixture
def home(tmp_path, monkeypatch):
    target = tmp_path / "home"
    target.mkdir()
    monkeypatch.setattr(Path, "home", lambda: target)
    for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "TRANSCRIBER_TYPE", "WHISPER_MODEL_SIZE",
                "VIDEONOTE_TRANSCRIBER_DEVICE", "XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIDEONOTE_CONFIG_DIR", str(tmp_path / "data" / "config"))
    return target


@pytest.fixture
def entry(tmp_path):
    return {"type": "stdio", "command": str(tmp_path / "runner with spaces"),
            "args": ["--python", "3.12", "videonote==" + __version__],
            "env": {"VIDEONOTE_DATA_DIR": str(tmp_path / "data"), "VIDEONOTE_TRANSCRIBER_DEVICE": "cpu"}}


@pytest.mark.parametrize("platform,machine,version,expected", [
    ("darwin", "arm64", "14.0", True), ("darwin", "arm64", "13.6", False),
    ("darwin", "x86_64", "15.0", False), ("win32", "arm64", "14.0", False),
    ("linux", "aarch64", "", False), ("darwin", "arm64", "", False),
])
def test_mlx_recommendation_requires_native_apple_silicon(platform, machine, version, expected):
    assert hardware.mlx_supported(platform, machine, version) is expected


@pytest.mark.parametrize("cuda,nvidia,recommended", [(True, True, "cuda"), (False, True, "cpu"), (False, False, "cpu")])
def test_cuda_requires_backend_and_libraries(monkeypatch, cuda, nvidia, recommended):
    monkeypatch.setattr(hardware.sys, "platform", "linux")
    result = SimpleNamespace(returncode=0, stdout=json.dumps({"cuda": cuda, "nvidia": nvidia}))
    with mock.patch.object(hardware.subprocess, "run", side_effect=[result, OSError("no nvidia-smi")]) as run:
        detected = hardware.inspect_hardware()
    assert detected.recommended == recommended
    assert "torch" not in run.call_args_list[0].args[0][-1]
    assert run.call_args_list[0].kwargs["timeout"] == 15


def test_hardware_timeout_keeps_gpu_presence_separate_from_readiness(monkeypatch):
    monkeypatch.setattr(hardware.sys, "platform", "linux")
    with mock.patch.object(hardware.subprocess, "run", side_effect=[
        subprocess.TimeoutExpired("probe", 15), SimpleNamespace(returncode=0, stdout="NVIDIA GPU\n")
    ]):
        detected = hardware.inspect_hardware()
    assert detected.nvidia_present and not detected.cuda_available
    assert detected.recommended == "cpu"


@pytest.mark.parametrize("system,suffix", [
    ("darwin", "Library/Application Support/Claude/claude_desktop_config.json"),
    ("linux", ".config/Claude/claude_desktop_config.json"),
    ("win32", "AppData/Roaming/Claude/claude_desktop_config.json"),
])
def test_platform_paths(home, system, suffix):
    candidates = mcp_clients.clients(system, home, {}, which=lambda _: None)
    desktop = next(item for item in candidates if item.key == "claude-desktop")
    assert desktop.path == home / suffix


def test_custom_homes_and_windows_store_installation(home):
    redirected = home / "local" / "Packages" / "Claude_example" / "LocalCache" / "Roaming" / "Claude"
    redirected.mkdir(parents=True)
    env = {"LOCALAPPDATA": str(home / "local"), "APPDATA": str(home / "roaming"),
           "CODEX_HOME": str(home / "custom codex"), "CLAUDE_CONFIG_DIR": str(home / "custom claude")}
    candidates = {item.key: item for item in mcp_clients.clients("win32", home, env, which=lambda _: None)}
    assert candidates["claude-desktop"].path == redirected / "claude_desktop_config.json"
    assert candidates["codex"].path == home / "custom codex" / "config.toml"
    assert candidates["claude-code"].path == home / "custom claude" / ".claude.json"


def test_detection_does_not_create_missing_directories(home):
    candidates = mcp_clients.clients("linux", home, {}, which=lambda _: None)
    assert not any(item.detected for item in candidates)
    assert list(home.iterdir()) == []


def test_json_merge_backup_and_repeat_install(home, entry):
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    original = b'{"theme":"dark","mcpServers":{"other":{"command":"other","env":{"KEY":"secret"}}}}'
    path.write_bytes(original)
    client = Client("cursor", path)
    state = mcp_clients.inspect(client, entry)
    saved = mcp_clients.register(client, state, entry)
    assert saved.read_bytes() == original
    result = json.loads(path.read_text())
    assert result["theme"] == "dark"
    assert result["mcpServers"]["other"]["env"] == {"KEY": "secret"}
    assert result["mcpServers"]["videonote"]["command"] == entry["command"]
    repeated = mcp_clients.inspect(client, entry)
    assert repeated.status == "same"
    assert mcp_clients.register(client, repeated, entry) is None
    assert len(list(path.parent.glob("*.bak"))) == 1


def test_toml_update_preserves_comments_other_servers_and_timeout(home, entry):
    path = home / "config.toml"
    original = ('# keep my comment\nmodel = "example"\n'
                '[mcp_servers.other]\ncommand = "other"\n'
                '[mcp_servers.videonote]\ncommand = "uvx" # keep inline\n'
                'args = ["videonote@latest"]\ntool_timeout_sec = 600\nenabled = false\n'
                '[mcp_servers.videonote.env]\nCUSTOM = "keep"\n')
    path.write_text(original)
    client = Client("codex", path)
    state = mcp_clients.inspect(client, entry)
    assert state.status == "update"
    saved = mcp_clients.register(client, state, entry)
    output = path.read_text()
    parsed = tomllib.loads(output)
    assert "# keep my comment" in output and "# keep inline" in output
    assert parsed["model"] == "example"
    assert parsed["mcp_servers"]["other"]["command"] == "other"
    current = parsed["mcp_servers"]["videonote"]
    assert current["tool_timeout_sec"] == 600 and current["enabled"] is False
    assert current["env"]["CUSTOM"] == "keep"
    assert saved.read_text() == original
    assert mcp_clients.inspect(client, entry).status == "same"


@pytest.mark.parametrize("key,original", [("cursor", b'{"mcpServers":'), ("codex", b'[mcp_servers.')])
def test_malformed_configuration_is_never_replaced(home, entry, key, original):
    path = home / "broken-config"
    path.write_bytes(original)
    with pytest.raises(ConfigError, match="保留原文件"):
        mcp_clients.inspect(Client(key, path), entry)
    assert path.read_bytes() == original and not list(home.glob("*.bak"))


def test_name_conflict_and_duplicate_aliases_are_not_overwritten(home, entry):
    path = home / "config.json"
    original = '{"mcpServers":{"videonote":{"command":"other"}}}'
    path.write_text(original)
    client = Client("cursor", path)
    state = mcp_clients.inspect(client, entry)
    assert state.status == "conflict"
    with pytest.raises(ConfigError):
        mcp_clients.register(client, state, entry)
    assert path.read_text() == original
    path.write_text(json.dumps({"mcpServers": {"one": entry, "two": entry}}))
    with pytest.raises(ConfigError, match="多份"):
        mcp_clients.inspect(client, entry)


def test_existing_alias_is_updated_without_creating_second_entry(home, entry):
    path = home / "config.json"
    path.write_text('{"mcpServers":{"old-note":{"command":"uvx","args":["videonote"]}}}')
    client = Client("cursor", path)
    state = mcp_clients.inspect(client, entry)
    assert state.name == "old-note"
    mcp_clients.register(client, state, entry)
    assert list(json.loads(path.read_text())["mcpServers"]) == ["old-note"]


def test_enabled_plugin_blocks_duplicate_registration(home, entry):
    path = home / "config.toml"
    path.write_text('[plugins."videonote@videonote"]\nenabled = true\n')
    assert mcp_clients.inspect(Client("codex", path), entry).status == "plugin"
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text('{"enabledPlugins":{"videonote@videonote":true}}')
    assert mcp_clients.inspect(Client("claude-code", home / ".claude.json", "claude"), entry).status == "plugin"


def test_concurrent_config_edit_is_not_lost(home, entry):
    path = home / "config.json"
    client = Client("cursor", path)
    state = mcp_clients.inspect(client, entry)
    changed = '{"mcpServers":{"new-service":{"command":"example"}}}'
    path.write_text(changed)
    with pytest.raises(ConfigError, match="发生变化"):
        mcp_clients.register(client, state, entry)
    assert path.read_text() == changed


@pytest.mark.parametrize("key", ["codex", "claude-code"])
def test_new_cli_registration_uses_argument_list_and_user_scope(home, entry, key):
    path = home / ("config.toml" if key == "codex" else ".claude.json")
    client = Client(key, path, "/cli with spaces/" + key)
    state = mcp_clients.inspect(client, entry)

    def execute(command, **kwargs):
        assert command[0] == client.cli and "--" in command
        assert entry["command"] in command
        assert kwargs["timeout"] == 30 and "shell" not in kwargs
        assert ("--scope" in command) is (key == "claude-code")
        mcp_clients.write_config(client, state, entry)
        return SimpleNamespace(returncode=0)

    with mock.patch.object(mcp_clients.subprocess, "run", side_effect=execute):
        mcp_clients.register(client, state, entry)
    assert mcp_clients.inspect(client, entry).status == "same"


def test_cli_failure_does_not_expose_output_secrets(home, entry):
    client = Client("codex", home / "config.toml", "codex")
    with mock.patch.object(mcp_clients.subprocess, "run", return_value=SimpleNamespace(
        returncode=1, stdout="SECRET_TOKEN", stderr="SECRET_TOKEN")):
        with pytest.raises(ConfigError) as error:
            mcp_clients.register(client, mcp_clients.inspect(client, entry), entry)
    assert "SECRET_TOKEN" not in str(error.value)


def fake_install(monkeypatch, home):
    monkeypatch.setattr(installer, "source_root", lambda: None)
    monkeypatch.setattr(installer, "find_runner", lambda _: str(home / "uvx"))
    monkeypatch.setattr(installer, "clients", lambda *_: [Client("cursor", home / ".cursor" / "mcp.json", detected=True)])
    monkeypatch.setattr(installer, "inspect_hardware", lambda: Hardware("linux", "x86_64", False, False, False, "CPU"))
    actions = []

    def action(runtime, operation, arguments=None):
        actions.append((runtime, operation, arguments))
        return ({"version": __version__, "cuda": False, "mlx": False} if operation == "probe"
                else {"model_ready": False, "asr_verified": False})

    monkeypatch.setattr(installer, "runtime_action", action)
    return actions


def test_preview_has_no_installation_or_configuration_side_effects(home, monkeypatch):
    actions = fake_install(monkeypatch, home)
    assert installer.main(["--client", "cursor", "--dry-run", "--asr", "cpu"]) == 0
    assert actions == []
    assert not (home / ".cursor").exists()
    assert not Path(os.environ["VIDEONOTE_DATA_DIR"]).exists()


def test_noninteractive_requires_explicit_scope(home, monkeypatch):
    actions = fake_install(monkeypatch, home)
    monkeypatch.setattr(installer.sys.stdin, "isatty", lambda: False)
    assert installer.main(["--yes"]) == 1
    assert installer.main(["--client", "cursor"]) == 1
    assert actions == []


def test_runtime_and_client_share_dependencies_directories_and_device(home, monkeypatch):
    actions = fake_install(monkeypatch, home)
    assert installer.main(["--yes", "--client", "cursor", "--asr", "cpu", "--no-verify", "--download"]) == 0
    registered = json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["videonote"]
    assert [operation for _, operation, _ in actions] == ["probe", "prepare"]
    assert all(runtime.entry == registered for runtime, _, _ in actions)
    assert "--download" in actions[1][2]
    profile = json.loads((Path(os.environ["VIDEONOTE_CONFIG_DIR"]) / "installation.json").read_text())
    assert profile["env"] == registered["env"]
    assert profile["args"][-1] == "videonote==" + __version__


def test_mlx_optional_dependency_is_part_of_all_runtime_commands(home, monkeypatch):
    actions = fake_install(monkeypatch, home)
    monkeypatch.setattr(installer, "inspect_hardware", lambda: Hardware("darwin", "arm64", True, False, False, "Apple"))

    def action(runtime, operation, arguments=None):
        actions.append((runtime, operation, arguments))
        return {"version": __version__, "cuda": False, "mlx": True, "model_ready": False, "asr_verified": False}

    monkeypatch.setattr(installer, "runtime_action", action)
    assert installer.main(["--yes", "--client", "cursor", "--asr", "auto", "--no-verify"]) == 0
    assert all(runtime.args[-1] == "videonote[mlx]==" + __version__ for runtime, _, _ in actions)


def test_custom_model_directory_is_shared_with_gui_runtime(home, monkeypatch):
    actions = fake_install(monkeypatch, home)
    models = home / "models with spaces"
    monkeypatch.setenv("VIDEONOTE_MODEL_DIR", str(models))
    assert installer.main(["--yes", "--client", "cursor", "--asr", "cpu", "--no-verify"]) == 0
    registered = json.loads((home / ".cursor" / "mcp.json").read_text())["mcpServers"]["videonote"]
    assert registered["env"]["VIDEONOTE_MODEL_DIR"] == str(models)
    assert all(runtime.env["VIDEONOTE_MODEL_DIR"] == str(models) for runtime, _, _ in actions)


def test_cuda_not_ready_never_registers_client(home, monkeypatch):
    fake_install(monkeypatch, home)
    assert installer.main(["--yes", "--client", "cursor", "--asr", "cuda", "--no-verify"]) == 1
    assert not (home / ".cursor").exists()


def test_mcp_handshake_failure_never_registers_client(home, monkeypatch):
    fake_install(monkeypatch, home)

    async def fail(_):
        raise TimeoutError("private error")

    monkeypatch.setattr(installer, "verify_mcp", fail)
    assert installer.main(["--yes", "--client", "cursor", "--asr", "cpu"]) == 1
    assert not (home / ".cursor").exists()


def test_existing_cloud_engine_and_size_are_preserved(home, monkeypatch):
    actions = fake_install(monkeypatch, home)
    config = Path(os.environ["VIDEONOTE_CONFIG_DIR"])
    config.mkdir(parents=True)
    (config / "transcriber.json").write_text('{"transcriber_type":"groq","whisper_model_size":"medium"}')
    (config / "installation.json").write_text('{"env":{"VIDEONOTE_TRANSCRIBER_DEVICE":"cuda"}}')
    assert installer.main(["--yes", "--client", "cursor", "--no-verify"]) == 0
    assert "groq" in actions[1][2] and "medium" in actions[1][2]
    assert "--download" not in actions[1][2]


def test_configuration_persists_terminal_defaults_for_gui_clients(home, monkeypatch):
    monkeypatch.setenv("TRANSCRIBER_TYPE", "groq")
    monkeypatch.setenv("WHISPER_MODEL_SIZE", "medium")
    result = installer_runtime.prepare("groq", "medium", "cpu", False, False)
    config = Path(os.environ["VIDEONOTE_CONFIG_DIR"]) / "transcriber.json"
    saved = json.loads(config.read_text())
    assert saved["transcriber_type"] == "groq" and saved["whisper_model_size"] == "medium"
    assert result["model_ready"] is None


def test_changed_registration_requires_explicit_replace(home, monkeypatch):
    actions = fake_install(monkeypatch, home)
    path = home / ".cursor" / "mcp.json"
    path.parent.mkdir()
    old = '{"mcpServers":{"videonote":{"command":"uvx","args":["videonote@latest"]}}}'
    path.write_text(old)
    assert installer.main(["--yes", "--client", "cursor", "--asr", "cpu", "--no-verify"]) == 1
    assert actions == [] and path.read_text() == old
    assert installer.main(["--yes", "--client", "cursor", "--asr", "cpu", "--no-verify", "--replace"]) == 0
    assert path.read_text() != old


def test_installer_help_imports_without_third_party_packages(home):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-S", "-m", "videonote_mcp", "install", "--help"],
                            cwd=root, env={**os.environ, "HOME": str(home)}, capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0 and "--dry-run" in result.stdout
    assert not Path(os.environ["VIDEONOTE_DATA_DIR"]).exists()


def test_installer_preview_in_isolated_process_does_not_initialize_db(home, tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, "-S", "-m", "videonote_mcp", "install", "--client", "cursor",
                             "--dry-run", "--asr", "cpu"], cwd=root,
                            env={**os.environ, "HOME": str(home), "DATABASE_URL": "sqlite:///" + str(tmp_path / "never.db")},
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert not (tmp_path / "never.db").exists()
    assert not Path(os.environ["VIDEONOTE_DATA_DIR"]).exists()
    assert not (home / ".cursor").exists()


def test_asr_verification_consumes_lazy_inference_with_vad_disabled(monkeypatch, tmp_path):
    consumed = []

    def transcribe(path, **kwargs):
        assert Path(path).is_file() and kwargs["vad_filter"] is False

        def segments():
            consumed.append(True)
            yield "segment"

        return segments(), None

    transcriber = SimpleNamespace(device="cuda", model=SimpleNamespace(transcribe=transcribe))
    with mock.patch("app.transcriber.whisper.WhisperTranscriber", return_value=transcriber):
        installer_runtime.verify_asr("fast-whisper", "small", "cuda")
    assert consumed == [True]


def test_device_setting_is_used_by_runtime_and_explicit_argument_wins(monkeypatch):
    from app.transcriber import transcriber_provider

    monkeypatch.setenv("VIDEONOTE_TRANSCRIBER_DEVICE", "cpu")
    with mock.patch.object(transcriber_provider, "get_whisper_transcriber") as get:
        transcriber_provider.get_transcriber("fast-whisper", "small")
        assert get.call_args.kwargs["device"] == "cpu"
        transcriber_provider.get_transcriber("fast-whisper", "small", device="cuda")
        assert get.call_args.kwargs["device"] == "cuda"


def test_unsupported_device_is_rejected(monkeypatch):
    from app.transcriber import transcriber_provider

    monkeypatch.setenv("VIDEONOTE_TRANSCRIBER_DEVICE", "invalid")
    with pytest.raises(ValueError, match="必须为 cpu 或 cuda"):
        transcriber_provider.get_transcriber()
