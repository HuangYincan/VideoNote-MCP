"""Color accessibility and profile summaries without changing selections/settings."""
import io
from pathlib import Path
from unittest.mock import Mock

import pytest

from videonote_mcp import installer, installer_profiles, terminal_ui
from videonote_mcp.hardware import Hardware
from videonote_mcp.mcp_clients import Client


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def terminal(monkeypatch):
    output = Terminal()
    monkeypatch.setattr(terminal_ui, "_windows_colors", lambda _: True)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    return output


@pytest.mark.parametrize("plain", ["redirect", "no_color", "dumb"])
def test_plain_output_has_no_color_or_clear_sequences(terminal, monkeypatch, plain):
    monkeypatch.setattr(terminal_ui.sys, "stdout", terminal)
    if plain == "redirect":
        monkeypatch.setattr(terminal, "isatty", lambda: False)
    elif plain == "no_color":
        monkeypatch.setenv("NO_COLOR", "1")
    else:
        monkeypatch.setenv("TERM", "dumb")
    terminal_ui.header("安装向导", "② 语音转写引擎")
    terminal_ui.field("引擎", "mlx-whisper", "（配置文件）")
    assert "\x1b" not in terminal.getvalue()
    assert "引擎：mlx-whisper（配置文件）" in terminal.getvalue()
    style = terminal_ui.prompt_style().dict
    assert not any(value for key, value in style.items() if key != "bottom-toolbar")


def test_colors_distinguish_labels_values_notes_and_errors(terminal, monkeypatch):
    monkeypatch.setattr(terminal_ui.sys, "stdout", terminal)
    terminal_ui.header("安装向导", "② 语音转写引擎", clear=False)
    terminal_ui.field("引擎", "mlx-whisper", "（配置文件）")
    terminal_ui.message("注册失败", "error")
    text = terminal.getvalue()
    assert "\x1b[36m引擎：" in text
    assert "\x1b[1;32mmlx-whisper" in text
    assert "\x1b[2m（配置文件）" in text
    assert "\x1b[1;31m注册失败" in text
    style = terminal_ui.prompt_style().dict
    assert len({style[key] for key in ("question", "pointer", "checkbox", "instruction")}) == 4


@pytest.mark.parametrize("kind", ["select", "checkbox", "confirm", "secret", "text"])
def test_shared_theme_keeps_values_bindings_and_secret_prompts(terminal, monkeypatch, kind):
    monkeypatch.setattr(terminal_ui.sys, "stdout", terminal)
    from InquirerPy import inquirer

    factory = Mock()
    original = getattr(inquirer, kind)
    setattr(inquirer, kind, factory)
    try:
        choices = [{"name": "Codex — 注册已启用", "value": "codex", "enabled": True}]
        kwargs = {"message": "选择", "keybindings": terminal_ui.BACK_KEYS}
        if kind in ("select", "checkbox"):
            kwargs["choices"] = choices
        getattr(terminal_ui.prompts(), kind)(**kwargs)
        options = factory.call_args.kwargs
        assert options["style"].dict["pointer"] == "bg:ansiyellow ansiblack bold"
        assert options["keybindings"] is terminal_ui.BACK_KEYS
        if "choices" in kwargs:
            assert options["choices"] is choices and "\x1b" not in choices[0]["name"]
    finally:
        setattr(inquirer, kind, original)


def test_profile_paths_are_shown_once_and_mlx_device_is_not_cpu(tmp_path, monkeypatch, capsys):
    profiles = [installer_profiles.Profile(
        tmp_path / key, tmp_path / key / "config",
        {"transcriber_type": "mlx-whisper", "whisper_model_size": "large-v3-turbo"}, {},
        {"VIDEONOTE_TRANSCRIBER_DEVICE": "cpu", "VIDEONOTE_MODEL_DIR": str(tmp_path / key / "models")}, source)
        for key, source in (("source", "当前目录 / 环境变量"), ("package", "已安装包的用户配置"))]
    monkeypatch.setattr(installer_profiles, "discover_profiles", lambda *_: profiles)
    names = []

    def choose(_message, choices, _default):
        names.extend(item["name"] for item in choices)
        for name in names:
            print(name)
        return 1

    monkeypatch.setattr(installer_profiles, "select", choose)
    result = installer_profiles.pick_profile(None, tmp_path, tmp_path, [], {}, tmp_path, True)
    text = capsys.readouterr().out
    assert result is profiles[1]
    for profile in profiles:
        assert text.count(str(profile.config)) == text.count(profile.environment["VIDEONOTE_MODEL_DIR"]) == 1
    assert all(str(tmp_path) not in name and "\x1b" not in name for name in names)
    assert text.count("设备：Apple GPU / MLX") == 2
    assert "设备标记" not in text and "cpu" not in text.lower()


def test_install_summary_uses_mlx_device_meaning_without_rewriting_marker(tmp_path, monkeypatch, capsys):
    import json

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    data = tmp_path / "data"
    config = data / "config"
    config.mkdir(parents=True)
    settings = json.dumps({"transcriber_type": "mlx-whisper", "whisper_model_size": "large-v3-turbo"}).encode()
    path = config / "transcriber.json"
    path.write_bytes(settings)
    for key in installer_profiles.ASR_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(data))
    monkeypatch.setenv("VIDEONOTE_CONFIG_DIR", str(config))
    monkeypatch.setenv("VIDEONOTE_TRANSCRIBER_DEVICE", "cpu")
    monkeypatch.setattr(installer, "clients", lambda *_: [Client("cursor", tmp_path / "cursor.json")])
    monkeypatch.setattr(installer, "source_root", lambda: None)
    monkeypatch.setattr(installer, "find_runner", lambda _: str(tmp_path / "uvx"))
    monkeypatch.setattr(installer, "inspect_hardware", lambda: Hardware("darwin", "arm64", True, False, False, "Apple Silicon"))
    assert installer.main(["--client", "cursor", "--dry-run", "--asr", "keep"]) == 0
    assert "转写：mlx-whisper / large-v3-turbo / Apple GPU / MLX" in capsys.readouterr().out
    assert path.read_bytes() == settings
    assert not (config / "installation.json").exists()
