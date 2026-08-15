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
