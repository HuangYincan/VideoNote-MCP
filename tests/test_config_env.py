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


def test_env_int():
    os.environ["VN_TEST"] = "6"
    assert env_int("VN_TEST", 0) == 6
    os.environ["VN_TEST"] = "not-a-number"
    assert env_int("VN_TEST", 20) == 20
    os.environ.pop("VN_TEST", None)
    assert env_int("VN_TEST", 20) == 20


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
