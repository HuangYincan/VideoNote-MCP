"""Existing ASR summaries and bounded legacy profile discovery; isolated homes."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from videonote_mcp import __version__, installer, installer_runtime, terminal_ui
from videonote_mcp import installer_profiles as profiles
from videonote_mcp.hardware import Hardware
from videonote_mcp.mcp_clients import Client, ConfigError

CPU = Hardware("linux", "x86_64", False, False, False, "CPU")


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for name in (*profiles.ASR_ENV, "VIDEONOTE_DATA_DIR", "VIDEONOTE_CONFIG_DIR", "XDG_DATA_HOME", "APPDATA"):
        monkeypatch.delenv(name, raising=False)


def config_at(data, settings, installation=None):
    config = data / "config"
    config.mkdir(parents=True)
    (config / "transcriber.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2))
    if installation is not None:
        (config / "installation.json").write_text(json.dumps(installation))
    return config


def registered(home, key, environment, command="uvx", args=None):
    client = Client(key, home / key / "mcp.json")
    client.path.parent.mkdir()
    client.path.write_text(json.dumps({"mcpServers": {"old-notes": {
        "command": command, "args": args or ["videonote@latest"], "env": environment}}}))
    return client


def choose(monkeypatch, existing, installation=None, answer="keep", hardware=CPU, model=None):
    inq = Mock()
    inq.select.return_value.execute.return_value = answer
    monkeypatch.setattr(terminal_ui, "prompts", lambda: inq)
    result = installer.choose_asr(SimpleNamespace(asr=None, model=model), hardware, existing, installation or {}, True)
    return result, inq


def test_keep_shows_settings_and_sources_and_does_not_reprompt_for_model(monkeypatch, capsys):
    monkeypatch.setenv("TRANSCRIBER_TYPE", "groq")
    monkeypatch.setenv("WHISPER_MODEL_SIZE", "tiny")
    existing = {"transcriber_type": "fast-whisper", "whisper_model_size": "saved/custom-whisper",
                "enable_preprocess": "false", "diarization": "true", "diarization_speakers": 2,
                "api_key": "dummy-secret-must-not-appear"}
    result, inq = choose(monkeypatch, existing, {"env": {"VIDEONOTE_TRANSCRIBER_DEVICE": "cpu",
                                                       "PRIVATE_TOKEN": "dummy-secret-must-not-appear"}})
    assert result == ("fast-whisper", "saved/custom-whisper", "cpu", False)
    assert inq.select.call_count == 1
    out = capsys.readouterr().out
    for expected in ("引擎：fast-whisper（配置文件）", "模型尺寸：saved/custom-whisper（配置文件）",
                     "设备设置：CPU", "音频预处理：关（配置文件）", "说话人分离：开（配置文件）", "说话人数：2"):
        assert expected in out
    assert "dummy-secret-must-not-appear" not in out
    assert "saved/custom-whisper" in next(c["name"] for c in inq.select.call_args.kwargs["choices"] if c["value"] == "keep")


def test_installation_record_is_visible_fallback_when_config_missing(monkeypatch, capsys):
    result, _ = choose(monkeypatch, {}, {"engine": "fast-whisper", "size": "medium", "env": {"VIDEONOTE_TRANSCRIBER_DEVICE": "cuda"}})
    assert result == ("fast-whisper", "medium", "cuda", False)
    assert "模型尺寸：medium（上次安装记录）" in capsys.readouterr().out


def test_explicit_model_override_is_shown_and_applied(monkeypatch, capsys):
    result, inq = choose(monkeypatch, {"transcriber_type": "fast-whisper", "whisper_model_size": "medium"}, model="base")
    assert result[1] == "base" and inq.select.call_count == 1
    out = capsys.readouterr().out
    assert "模型尺寸：medium" in out and "--model 指定 base" in out


def test_cloud_summary_uses_actual_remote_model_not_whisper_backup(monkeypatch, capsys):
    monkeypatch.setenv("GROQ_TRANSCRIBER_MODEL", "whisper-large-v3-turbo")
    result, _ = choose(monkeypatch, {"transcriber_type": "groq", "whisper_model_size": "tiny"})
    assert result[0] == "groq"
    out = capsys.readouterr().out
    assert "当前模型：whisper-large-v3-turbo" in out
    assert "Whisper 备用尺寸：tiny" in out and "不使用本地计算设备" in out


def test_mlx_summary_shows_apple_gpu_even_with_cpu_marker(monkeypatch, capsys):
    apple = Hardware("darwin", "arm64", True, False, False, "Apple Silicon")
    result, _ = choose(monkeypatch, {"transcriber_type": "mlx-whisper", "whisper_model_size": "medium"},
                       {"env": {"VIDEONOTE_TRANSCRIBER_DEVICE": "cpu"}}, hardware=apple)
    assert result[:2] == ("mlx-whisper", "medium")
    assert "设备设置：Apple GPU / MLX" in capsys.readouterr().out


def test_first_install_does_not_claim_saved_settings(monkeypatch, capsys):
    result, inq = choose(monkeypatch, {})
    assert result[:2] == ("fast-whisper", "small")
    choice = next(c for c in inq.select.call_args.kwargs["choices"] if c["value"] == "keep")
    assert "尚无保存的设置" in choice["name"]
    out = capsys.readouterr().out
    assert "程序默认" in out and "未明确设置" in out


def test_agent_env_legacy_settings_and_paths_are_reused(tmp_path):
    data = tmp_path / "old-data"
    config = config_at(data, {"enable_preprocess": "false", "diarization_speakers": 3})
    client = registered(tmp_path, "cursor", {"VIDEONOTE_DATA_DIR": str(data), "VIDEONOTE_CONFIG_DIR": str(config),
                                           "TRANSCRIBER_TYPE": "groq", "WHISPER_MODEL_SIZE": "medium",
                                           "VIDEONOTE_DIARIZATION": "true", "GROQ_TRANSCRIBER_MODEL": "custom-model",
                                           "PRIVATE_TOKEN": "dummy-secret-must-not-appear"})
    before = (config / "transcriber.json").read_bytes()
    result = profiles.pick_profile(None, tmp_path / "new-data", tmp_path / "new-data/config", [client], {}, tmp_path, False)
    assert result.data == data and result.config == config
    assert profiles.asr_values(result.settings, result.installation, result.environment)["engine"][0] == "groq"
    assert result.environment["VIDEONOTE_DIARIZATION"] == "true" and "PRIVATE_TOKEN" not in result.environment
    assert (config / "transcriber.json").read_bytes() == before and not (tmp_path / "new-data").exists()


def test_packaged_profile_and_model_cache_are_discovered_from_source_install(tmp_path):
    data = tmp_path / ("videonote-mcp" if os.name == "nt" else ".local/share/videonote-mcp")
    config = config_at(data, {"transcriber_type": "fast-whisper", "whisper_model_size": "medium"})
    (data / "models").mkdir()
    root = tmp_path / "new-source"
    result = profiles.pick_profile(root, root / "data", root / "data/config", [], {}, tmp_path, False)
    assert result.config == config and result.data == data
    assert result.environment["VIDEONOTE_MODEL_DIR"] == str(data / "models")


def test_legacy_source_config_directory_reused_without_copying(tmp_path):
    root = tmp_path / "repo"
    legacy = config_at(root, {"transcriber_type": "groq", "whisper_model_size": "small"})
    result = profiles.pick_profile(root, root / "data", root / "data/config", [], {}, tmp_path, False)
    assert result.config == legacy and result.data == root / "data"
    assert not (root / "data").exists()


def test_legacy_packaged_record_retains_data_and_expands_saved_model_path(tmp_path):
    packaged = tmp_path / ("videonote-mcp" if os.name == "nt" else ".local/share/videonote-mcp")
    data = tmp_path / "old-database"
    config = config_at(packaged, {"transcriber_type": "groq"}, {"env": {
        "VIDEONOTE_DATA_DIR": str(data), "VIDEONOTE_MODEL_DIR": "~/old-models"}})
    result = profiles.pick_profile(tmp_path / "source", tmp_path / "new", tmp_path / "new/config", [], {}, tmp_path, False)
    assert result.data == data and result.config == config
    assert result.environment["VIDEONOTE_MODEL_DIR"] == str(tmp_path / "old-models")
    assert not data.exists() and not (tmp_path / "old-models").exists()


def test_relative_paths_require_known_launcher_directory(tmp_path):
    root = tmp_path / "old-repo"
    config = config_at(root / "state", {"transcriber_type": "groq"})
    (root / "weights").mkdir()
    environment = {"VIDEONOTE_DATA_DIR": "state", "VIDEONOTE_CONFIG_DIR": "state/config", "VIDEONOTE_MODEL_DIR": "weights"}
    client = registered(tmp_path, "cursor", environment, command="uv", args=["--directory", str(root), "run", "videonote"])
    result = profiles.pick_profile(None, tmp_path / "new", tmp_path / "new/config", [client], {}, tmp_path, False)
    assert result.config == config and result.environment["VIDEONOTE_MODEL_DIR"] == str(root / "weights")
    document = json.loads(client.path.read_text())
    document["mcpServers"]["old-notes"]["args"] = ["run", "videonote"]
    client.path.write_text(json.dumps(document))
    with pytest.raises(ConfigError, match="相对路径"):
        profiles.pick_profile(None, tmp_path / "new", tmp_path / "new/config", [client], {}, tmp_path, False)


def test_venv_package_is_not_assumed_to_be_a_source_checkout(tmp_path):
    data = tmp_path / ("videonote-mcp" if os.name == "nt" else ".local/share/videonote-mcp")
    config = config_at(data, {"transcriber_type": "groq"})
    client = registered(tmp_path, "cursor", {}, command=str(tmp_path / "other-project/.venv/bin/videonote"))
    result = profiles.pick_profile(None, tmp_path / "new", tmp_path / "new/config", [client], {}, tmp_path, False)
    assert result.data == data and result.config == config


def test_explicit_directories_and_environment_take_precedence(tmp_path):
    data = tmp_path / "specified"
    config = config_at(data, {"transcriber_type": "groq", "whisper_model_size": "small"})
    old = tmp_path / "old"
    config_at(old, {"transcriber_type": "funasr", "whisper_model_size": "medium"})
    client = registered(tmp_path, "cursor", {"VIDEONOTE_DATA_DIR": str(old), "TRANSCRIBER_TYPE": "fast-whisper", "WHISPER_MODEL_SIZE": "tiny"})
    env = {"VIDEONOTE_DATA_DIR": str(data), "VIDEONOTE_CONFIG_DIR": str(config), "WHISPER_MODEL_SIZE": "base"}
    result = profiles.pick_profile(None, data, config, [client], env, tmp_path, False)
    assert result.config == config and profiles.asr_values(result.settings, result.installation, result.environment)["size"][0] == "small"


def test_old_record_data_dir_is_retained_when_only_config_dir_explicit(tmp_path):
    data = tmp_path / "retained-data"
    config = config_at(tmp_path / "shared", {}, {"engine": "groq", "size": "medium", "command": "uvx",
                                               "env": {"VIDEONOTE_DATA_DIR": str(data)}})
    result = profiles.pick_profile(None, tmp_path / "new", config, [], {"VIDEONOTE_CONFIG_DIR": str(config)}, tmp_path, False)
    assert result.data == data and result.config == config and result.installation["engine"] == "groq"


def test_conflicting_profiles_require_selection_not_guessing(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    config_at(first, {"transcriber_type": "groq"})
    config = config_at(second, {"transcriber_type": "funasr"})
    clients = [registered(tmp_path, key, {"VIDEONOTE_DATA_DIR": str(data)}) for key, data in (("cursor", first), ("claude-desktop", second))]
    with pytest.raises(ConfigError, match="多套历史"):
        profiles.pick_profile(None, tmp_path / "new", tmp_path / "new/config", clients, {}, tmp_path, False)
    selector = Mock(return_value=1)
    monkeypatch.setattr(profiles, "select", selector)
    result = profiles.pick_profile(None, tmp_path / "new", tmp_path / "new/config", clients, {}, tmp_path, True)
    assert result.config == config
    names = [choice["name"] for choice in selector.call_args.args[1]]
    assert "groq" in names[0] and "funasr" in names[1]


def test_same_file_overrides_different_client_defaults_without_false_conflict(tmp_path):
    data = tmp_path / "shared"
    config = config_at(data, {"transcriber_type": "groq", "whisper_model_size": "medium"})
    clients = [registered(tmp_path, key, {"VIDEONOTE_DATA_DIR": str(data), "TRANSCRIBER_TYPE": engine,
                                        "WHISPER_MODEL_SIZE": "tiny"}) for key, engine in (("cursor", "fast-whisper"), ("claude-desktop", "funasr"))]
    result = profiles.pick_profile(None, tmp_path / "new", tmp_path / "new/config", clients, {}, tmp_path, False)
    assert result.config == config


@pytest.mark.parametrize("text", ['{broken', '{"transcriber_type":"groq","transcriber_type":"funasr"}',
                                 '{"whisper_model_size":{}}', '\ufeff{"transcriber_type":"groq"}'])
def test_damaged_legacy_profile_is_never_reset(tmp_path, text):
    config = tmp_path / "config"
    config.mkdir()
    path = config / "transcriber.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError):
        profiles.discover_profiles(None, tmp_path, config, [], {}, tmp_path)
    assert path.read_text(encoding="utf-8") == text
    assert not path.with_suffix(".json.corrupt").exists()


def test_placeholder_env_and_credentials_are_not_reported_as_settings():
    assert profiles.safe_env({"TRANSCRIBER_TYPE": "${user_config.transcriber_type}", "WHISPER_MODEL_SIZE": "",
                              "PRIVATE_TOKEN": "dummy-secret-must-not-appear", "VIDEONOTE_ENABLE_PREPROCESS": "false"}) == {
                                  "VIDEONOTE_ENABLE_PREPROCESS": "false"}


def test_runtime_propagates_retained_transcription_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "find_runner", lambda _: "/fake/uvx")
    env = {"TRANSCRIBER_TYPE": "groq", "GROQ_TRANSCRIBER_MODEL": "custom-groq", "VIDEONOTE_DIARIZATION": "true",
           "PRIVATE_TOKEN": "dummy-secret-must-not-appear"}
    runtime = installer.runtime_for(None, "groq", "cpu", "3.12", data_config=(tmp_path, tmp_path / "config"), settings_env=env)
    assert runtime.env["GROQ_TRANSCRIBER_MODEL"] == "custom-groq" and runtime.env["VIDEONOTE_DIARIZATION"] == "true"
    assert runtime.env["VIDEONOTE_CONFIG_DIR"] == str(tmp_path / "config") and "PRIVATE_TOKEN" not in runtime.env


def test_prepare_preserves_file_bytes_and_unknown_options_when_keeping(tmp_path, monkeypatch):
    settings = {"transcriber_type": "groq", "whisper_model_size": "medium", "enable_preprocess": "false",
                "diarization": "true", "diarization_speakers": 2, "legacy_option": {"keep": True}}
    config = config_at(tmp_path, settings)
    monkeypatch.setenv("VIDEONOTE_CONFIG_DIR", str(config))
    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(tmp_path))
    raw = (config / "transcriber.json").read_bytes()
    installer_runtime.prepare("groq", "medium", "cpu", False, False, profiles.profile_digest(settings))
    assert (config / "transcriber.json").read_bytes() == raw


def test_prepare_detects_profile_changed_since_selection(tmp_path, monkeypatch):
    config = config_at(tmp_path, {"transcriber_type": "groq", "whisper_model_size": "medium"})
    monkeypatch.setenv("VIDEONOTE_CONFIG_DIR", str(config))
    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(tmp_path))
    expected = profiles.profile_digest(profiles.read_profile(config)[0])
    path = config / "transcriber.json"
    path.write_text('{"transcriber_type":"funasr","whisper_model_size":"small"}')
    raw = path.read_bytes()
    with pytest.raises(RuntimeError, match="changed"):
        installer_runtime.prepare("groq", "medium", "cpu", False, False, expected)
    assert path.read_bytes() == raw


@pytest.mark.parametrize("raw", [b'{broken', b'[]', b'\xef\xbb\xbf{"transcriber_type":"groq"}',
                                 b'{"transcriber_type":"groq","transcriber_type":"funasr"}'])
def test_prepare_never_renames_damaged_concurrent_profile(tmp_path, monkeypatch, raw):
    settings = {"transcriber_type": "groq", "whisper_model_size": "medium"}
    config = config_at(tmp_path, settings)
    monkeypatch.setenv("VIDEONOTE_CONFIG_DIR", str(config))
    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(tmp_path))
    path = config / "transcriber.json"
    path.write_bytes(raw)
    with pytest.raises(ConfigError):
        installer_runtime.prepare("groq", "medium", "cpu", False, False, profiles.profile_digest(settings))
    assert path.read_bytes() == raw and not path.with_suffix(".json.corrupt").exists()


def test_prepare_guard_reads_only_transcription_file_and_preserves_other_fields(tmp_path, monkeypatch):
    settings = {"transcriber_type": "groq", "whisper_model_size": "medium", "legacy_option": {"keep": True}}
    config = config_at(tmp_path, settings)
    monkeypatch.setenv("VIDEONOTE_CONFIG_DIR", str(config))
    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(tmp_path))
    path = config / "installation.json"
    path.write_bytes(b'{broken')
    installer_runtime.prepare("bcut", "small", "cpu", False, False, profiles.profile_digest(settings))
    assert profiles.read_settings(config) == {**settings, "transcriber_type": "bcut", "whisper_model_size": "small"}
    assert path.read_bytes() == b'{broken'


def test_install_reuses_old_data_profile_and_preserves_flags_and_login_files(tmp_path, monkeypatch, capsys):
    data = tmp_path / "existing-data"
    settings = {"transcriber_type": "groq", "whisper_model_size": "medium", "enable_preprocess": "false",
                "diarization": "true", "diarization_speakers": 3, "legacy_option": "retain"}
    config = config_at(data, settings, {"engine": "groq", "size": "medium", "legacy_record_option": "retain", "env": {}})
    cookie = config / "downloader.json"
    cookie.write_text('{"cookie":"dummy-secret-must-not-appear"}')
    client = registered(tmp_path, "cursor", {"VIDEONOTE_DATA_DIR": str(data), "VIDEONOTE_CONFIG_DIR": str(config),
                                           "GROQ_TRANSCRIBER_MODEL": "custom-cloud-model", "PRIVATE_TOKEN": "dummy-secret-must-not-appear"})
    raw = (config / "transcriber.json").read_bytes()
    monkeypatch.setattr(installer, "clients", lambda *_: [client])
    monkeypatch.setattr(installer, "source_root", lambda: None)
    monkeypatch.setattr(installer, "find_runner", lambda _: "/fake/uvx")
    monkeypatch.setattr(installer, "inspect_hardware", lambda: CPU)
    runtimes = []

    def action(runtime, operation, args=None):
        runtimes.append(runtime)
        if operation == "probe":
            return {"version": __version__, "cuda": False, "mlx": False}
        with patch.dict("os.environ", runtime.env):
            expected = args[args.index("--expected-profile-hash") + 1]
            return installer_runtime.prepare("groq", "medium", "cpu", False, False, expected)

    monkeypatch.setattr(installer, "runtime_action", action)
    assert installer.main(["--yes", "--client", "cursor", "--replace", "--no-download", "--no-verify"]) == 0
    assert (config / "transcriber.json").read_bytes() == raw and "dummy-secret" in cookie.read_text()
    assert all(runtime.env["VIDEONOTE_DATA_DIR"] == str(data) for runtime in runtimes)
    assert json.loads((config / "installation.json").read_text())["legacy_record_option"] == "retain"
    registered_env = json.loads(client.path.read_text())["mcpServers"]["old-notes"]["env"]
    assert registered_env["GROQ_TRANSCRIBER_MODEL"] == "custom-cloud-model"
    captured = capsys.readouterr()
    assert "dummy-secret-must-not-appear" not in captured.out + captured.err
