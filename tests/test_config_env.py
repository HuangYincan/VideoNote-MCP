"""videonote_mcp.config 的 env 助手与占位符剔除测试。

背景：Claude Code 插件 userConfig 对「用户跳过未填的项」会透传字面 `${user_config.x}`。
这些值绝不能当真实配置；`_purge_placeholder_env()` 统一剔除，让下游走默认值。
`env_or` / `env_bool` / `env_int` / `env_json_list` 是「配置文件优先、env 兜底」
读取点用的解析助手（userConfig 注入的 env 值都是字符串）。
"""
import os

from videonote_mcp.config import (
    _purge_placeholder_env,
    env_bool,
    env_int,
    env_json_list,
    env_or,
    setup_environment,
)


def test_purge_removes_literal_placeholders():
    os.environ["TRANSCRIBER_TYPE"] = "${user_config.transcriber_type}"
    os.environ["VIDEONOTE_DEFAULT_STYLE"] = "${user_config.default_style}"
    os.environ["WHISPER_MODEL_SIZE"] = "${user_config.whisper_model_size}"
    _purge_placeholder_env()
    assert "TRANSCRIBER_TYPE" not in os.environ
    assert "VIDEONOTE_DEFAULT_STYLE" not in os.environ
    assert "WHISPER_MODEL_SIZE" not in os.environ


def test_purge_keeps_real_values():
    os.environ["TRANSCRIBER_TYPE"] = "groq"
    os.environ["VIDEONOTE_DEFAULT_STYLE"] = "minimal"
    _purge_placeholder_env()
    assert os.environ["TRANSCRIBER_TYPE"] == "groq"
    assert os.environ["VIDEONOTE_DEFAULT_STYLE"] == "minimal"
    del os.environ["TRANSCRIBER_TYPE"]
    del os.environ["VIDEONOTE_DEFAULT_STYLE"]


def test_purge_removes_empty_values():
    # 部分版本对未填 userConfig 传空串而非占位符，同样应剔除走默认
    os.environ["TRANSCRIBER_TYPE"] = ""
    os.environ["WHISPER_MODEL_SIZE"] = ""
    _purge_placeholder_env()
    assert "TRANSCRIBER_TYPE" not in os.environ
    assert "WHISPER_MODEL_SIZE" not in os.environ


def test_env_or():
    os.environ["VN_TEST"] = "abc"
    assert env_or("VN_TEST") == "abc"
    os.environ["VN_TEST"] = "  "
    assert env_or("VN_TEST") is None
    os.environ.pop("VN_TEST", None)
    assert env_or("VN_TEST") is None


def test_env_bool():
    os.environ["VN_TEST"] = "true"
    assert env_bool("VN_TEST") is True
    os.environ["VN_TEST"] = "True"
    assert env_bool("VN_TEST") is True
    os.environ["VN_TEST"] = "0"
    assert env_bool("VN_TEST") is False
    os.environ.pop("VN_TEST", None)
    assert env_bool("VN_TEST", default=True) is True


def test_env_bool_garbage_falls_back_to_default():
    """垃圾值（非已知布尔词）回退 default，不静默翻反 default=True 的开关（#124 A5）。

    旧实现把垃圾值一律当 False——将来某调用点用 default=True 时，环境里一个手滑的
    "maybe" 会把开关静默翻成 False；与 env_int 的「解析失败回退」语义对齐后，
    "y"/"t" 这类缩写也不再被当成 False。
    """
    os.environ["VN_TEST"] = "maybe"
    assert env_bool("VN_TEST", default=True) is True
    os.environ["VN_TEST"] = "y"
    assert env_bool("VN_TEST", default=True) is True
    os.environ["VN_TEST"] = "off"
    assert env_bool("VN_TEST", default=True) is False
    os.environ["VN_TEST"] = "ON"
    assert env_bool("VN_TEST", default=False) is True
    os.environ.pop("VN_TEST", None)


def test_env_int():
    os.environ["VN_TEST"] = "6"
    assert env_int("VN_TEST", 0) == 6
    os.environ["VN_TEST"] = "not-a-number"
    assert env_int("VN_TEST", 20) == 20
    os.environ.pop("VN_TEST", None)
    assert env_int("VN_TEST", 20) == 20


def test_resolve_int_config_priority_and_garbage():
    """resolve_int_config：app_config 优先于 env，垃圾值 warning 回退，0 不被 falsy 吞（#120 C3）。

    MCP 与 CLI 向导共用同一实现（此前 CLI 各自裸 int()，垃圾值每次进向导循环就崩）。
    """
    from unittest import mock

    from videonote_mcp.config import resolve_int_config

    os.environ.pop("VN_RESOLVE", None)
    with mock.patch("videonote_mcp.config.get_app_config", return_value={}):
        assert resolve_int_config("video_interval", "VN_RESOLVE", 6) == 6  # 全缺 → 默认
    os.environ["VN_RESOLVE"] = "99"
    with mock.patch("videonote_mcp.config.get_app_config", return_value={"video_interval": "12"}):
        assert resolve_int_config("video_interval", "VN_RESOLVE", 6) == 12  # app_config 优先
    with mock.patch("videonote_mcp.config.get_app_config", return_value={"video_interval": "abc"}), \
         mock.patch("videonote_mcp.config.logger") as m_logger:
        assert resolve_int_config("video_interval", "VN_RESOLVE", 6) == 99  # 垃圾值回退 env
        assert any("非整数" in str(c) for c in m_logger.warning.call_args_list)
    with mock.patch("videonote_mcp.config.get_app_config", return_value={"video_interval": 0}):
        assert resolve_int_config("video_interval", "VN_RESOLVE", 6) == 0  # 0 显式关闭不被吞
    os.environ.pop("VN_RESOLVE", None)


def test_env_json_list():
    os.environ["VN_TEST"] = '["srt", "vtt"]'
    assert env_json_list("VN_TEST", []) == ["srt", "vtt"]
    os.environ["VN_TEST"] = "not-json"
    assert env_json_list("VN_TEST", []) == []
    os.environ.pop("VN_TEST", None)
    assert env_json_list("VN_TEST", []) == []


def test_setup_environment_purges_then_fills_defaults():
    os.environ["TRANSCRIBER_TYPE"] = "${user_config.transcriber_type}"
    setup_environment()
    # 占位符被剔除后，setdefault 重新填上默认值
    assert os.environ["TRANSCRIBER_TYPE"] == "fast-whisper"
    del os.environ["TRANSCRIBER_TYPE"]


def test_setup_environment_creates_data_dir_0700(monkeypatch, tmp_path):
    """#140 复扫 A3：数据目录创建即 0700（默认 umask 022 是 0755，同机可列 key/cookie 与笔记）。"""
    data_dir = tmp_path / "data"
    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(data_dir))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")  # 防裸脚本副产物
    setup_environment()
    assert data_dir.stat().st_mode & 0o777 == 0o700


def test_plugin_json_env_keys_match_mapping():
    """plugin.json 的 mcpServers env 键与 _USER_CONFIG_MAPPED_ENV 精确一致，
    且占位符指向存在的 userConfig 键（docs 审计 P1-3：漂移会把
    `${user_config.x}` 字面量透传成真实配置）。"""
    import json
    from pathlib import Path

    from videonote_mcp.config import _USER_CONFIG_MAPPED_ENV

    plugin = json.loads(
        (Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json").read_text()
    )
    env = plugin["mcpServers"]["videonote"]["env"]
    assert set(env) == set(_USER_CONFIG_MAPPED_ENV), (
        f"env 键漂移: plugin={sorted(env)} config={sorted(_USER_CONFIG_MAPPED_ENV)}"
    )
    for k, v in env.items():
        key_name = v.removeprefix("${user_config.").removesuffix("}")
        assert key_name in plugin["userConfig"], f"{k} 的占位符指向不存在的 userConfig: {key_name}"
    suffix = {k.removeprefix("VIDEONOTE_").lower() for k in env}
    assert suffix == set(plugin["userConfig"]), (
        f"userConfig 与 env 不对应: env={sorted(suffix)} plugin={sorted(plugin['userConfig'])}"
    )

def test_corrupt_app_config_warns_and_backs_up(tmp_path, monkeypatch):
    """app_config.json 损坏：warning + .corrupt 备份 + 空配置（#125 C1）。

    旧实现静默返回 {}——set_app_config 读到 {} 写回会把 default_model/notes_dir
    等其余配置全部抹掉（#106 修过的同类模式在 app_config 上的残留）。
    """
    from videonote_mcp.config import get_app_config

    monkeypatch.setenv("VIDEONOTE_CONFIG_DIR", str(tmp_path))
    cfg_file = tmp_path / "app_config.json"
    cfg_file.write_text("{broken json", encoding="utf-8")

    assert get_app_config() == {}
    assert (tmp_path / "app_config.json.corrupt").exists()
    assert not cfg_file.exists()  # 损坏文件被移走保留


def test_corrupt_app_config_set_preserves_backup(tmp_path, monkeypatch):
    """损坏后 set_app_config 以空为基写入：损坏文件已备份，不再静默覆盖。"""
    from videonote_mcp.config import get_app_config, set_app_config

    monkeypatch.setenv("VIDEONOTE_CONFIG_DIR", str(tmp_path))
    cfg_file = tmp_path / "app_config.json"
    cfg_file.write_text("{broken json", encoding="utf-8")

    set_app_config("default_model", "x")
    assert get_app_config() == {"default_model": "x"}
    # 损坏原文件被保留（数据可恢复，不再被静默抹掉）
    assert (tmp_path / "app_config.json.corrupt").exists()

