"""CLI（videonote_mcp.cli）非交互子命令的契约测试。

覆盖 `providers` / `transcriber` / `export` 三个子命令：
- providers add/set/list：加密落盘、掩码不泄明文、交互取 key、错误路径（SystemExit）；
- transcriber set/list/preprocess/diarization：配置读写闭环；
- export list/export：格式清单、找不到任务、真实任务渲染出文件。

不碰真实网络 / 转写 / LLM。数据目录与 DB 由根 conftest 按 pid 隔离；
`main()` 的未知子命令错误路径也一并覆盖。

运行（仓库根目录）：
    .venv/bin/python tests/test_cli.py
"""
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import videonote_mcp.cli as cli
from app.db.provider_dao import delete_provider
from app.services.provider import ProviderService


@pytest.fixture(autouse=True)
def clean_providers():
    """每个测试前清空供应商表，保证起点确定（同进程其他测试文件可能 seed 内置供应商）。"""
    for p in ProviderService.get_all_providers():
        delete_provider(p["id"])
    yield


def _cli_out(capsys):
    # cli.py 模块级 `builtins.print = _print_to_stderr`（MCP 场景防 stdio 污染）：
    # 默认 print 被重定向到 stderr，但显式 `file=sys.stdout` 的调用绕过重定向。
    # 断言取两流并集，不依赖具体落点。
    out, err = capsys.readouterr()
    return out + err


class TestProvidersCli:
    def test_add_and_list_masks_key(self, capsys):
        cli._providers_cli(
            ["add", "--name", "中转站A", "--base-url", "https://example.com/v1", "--api-key", "sk-test-secret-123456"]
        )
        out = _cli_out(capsys)
        assert "sk-test-secret-123456" not in out  # 明文绝不回显
        assert "已新增 中转站A" in out
        # 加密落盘（6g）：DB 里带 enc: 前缀，decrypt 后一致
        rows = ProviderService.get_all_providers()
        assert len(rows) == 1
        assert rows[0]["name"] == "中转站A"
        # safe 视图掩码：只露首尾
        safe = ProviderService.get_all_providers_safe()[0]
        masked = safe["api_key"]
        assert "sk-test-secret-123456" not in masked
        assert masked.startswith("sk-t") and masked.endswith("3456")

    def test_list_prints_masked_rows(self, capsys):
        cli._providers_cli(["add", "--name", "P1", "--base-url", "https://e.com/v1", "--api-key", "abcdefgh12345678"])
        capsys.readouterr()
        cli._providers_cli(["list"])
        out = _cli_out(capsys)
        assert "P1" in out
        assert "key=已填" in out
        assert "abcdefgh12345678" not in out  # 完整 key 不出现

    def test_add_interactive_key(self, capsys, monkeypatch):
        monkeypatch.setattr(cli, "_ask_secret", lambda prompt: "sk-interactive-key")
        cli._providers_cli(["add", "--name", "P2", "--base-url", "https://e.com/v1"])
        rows = ProviderService.get_all_providers()
        assert rows[0]["api_key"] == "sk-interactive-key"
        assert "已新增 P2" in _cli_out(capsys)

    def test_add_requires_name_and_base_url(self):
        with pytest.raises(SystemExit):
            cli._providers_cli(["add", "--name", "P3"])  # 缺 --base-url
        with pytest.raises(SystemExit):
            cli._providers_cli(["add", "--base-url", "https://e.com/v1"])  # 缺 --name

    def test_set_api_key_updates_and_masks(self, capsys):
        cli._providers_cli(["add", "--name", "P4", "--base-url", "https://e.com/v1", "--api-key", "old-key-1234"])
        capsys.readouterr()
        pid = ProviderService.get_all_providers()[0]["id"]
        cli._providers_cli(["set", pid, "--api-key", "new-key-5678"])
        assert ProviderService.get_provider_by_id(pid)["api_key"] == "new-key-5678"
        out = _cli_out(capsys)
        assert "new-key-5678" not in out

    def test_set_requires_at_least_one_field(self):
        with pytest.raises(SystemExit) as ei:
            cli._providers_cli(["set", "some-id"])
        assert ei.value.code == 2  # argparse 用法错误

    def test_set_missing_provider(self, capsys):
        with pytest.raises(SystemExit) as ei:
            cli._providers_cli(["set", "nonexistent-id", "--name", "X"])
        assert ei.value.code == 1
        assert "不存在" in capsys.readouterr().err


class TestTranscriberCli:
    def test_set_fast_whisper_and_list(self, capsys):
        cli._transcriber_cli(["set", "fast-whisper", "--size", "small"])
        out = _cli_out(capsys)
        assert "fast-whisper / small" in out
        capsys.readouterr()
        cli._transcriber_cli(["list"])
        out = _cli_out(capsys)
        assert "当前引擎: fast-whisper / small" in out
        assert "可选引擎" in out

    def test_set_local_engine_defaults_size(self, capsys):
        cli._transcriber_cli(["set", "mlx-whisper"])
        out = _cli_out(capsys)
        assert "mlx-whisper / small" in out  # 本地引擎未给 --size 时默认 small

    def test_set_cloud_engine_no_size(self, capsys):
        cli._transcriber_cli(["set", "groq"])
        # 云端引擎无 size 概念：配置文件保留旧值，不提示本地下载
        out = _cli_out(capsys)
        assert "已切换: groq /" in out
        assert "本地模型还需下载" not in out

    def test_set_invalid_engine(self):
        with pytest.raises(SystemExit) as ei:
            cli._transcriber_cli(["set", "not-an-engine"])
        assert ei.value.code == 2

    def test_preprocess_toggle(self, capsys):
        cli._transcriber_cli(["preprocess", "on"])
        assert "音频预处理: 开" in _cli_out(capsys)
        capsys.readouterr()
        cli._transcriber_cli(["preprocess", "off"])
        assert "音频预处理: 关" in _cli_out(capsys)

    def test_diarization_toggle(self, capsys):
        cli._transcriber_cli(["diarization", "on"])
        assert "说话人分离: 开" in _cli_out(capsys)
        capsys.readouterr()
        cli._transcriber_cli(["diarization", "off"])
        assert "说话人分离: 关" in _cli_out(capsys)


class TestExportCli:
    def test_export_list_formats(self, capsys):
        cli._export_cli(["list"])
        out = _cli_out(capsys)
        assert "srt" in out and "vtt" in out and "json" in out

    def test_export_missing_task(self, capsys):
        with pytest.raises(SystemExit) as ei:
            cli._export_cli(["export", "00000000-0000-0000-0000-000000000000"])
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "找不到任务" in err

    def test_export_renders_real_task(self, capsys):
        task_id = str(uuid.uuid4())
        note_out = Path(os.environ["NOTE_OUTPUT_DIR"])
        task_dir = note_out / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "result.json").write_text(
            json.dumps(
                {
                    "transcript": {
                        "language": "zh",
                        "full_text": "你好 世界",
                        "segments": [{"start": 0.0, "end": 1.5, "text": "你好"}, {"start": 1.5, "end": 3.0, "text": "世界"}],
                    }
                }
            ),
            encoding="utf-8",
        )
        cli._export_cli(["export", task_id, "--format", "srt"])
        out = _cli_out(capsys)
        assert "✓ 已导出 1 个格式" in out
        srt_file = task_dir / "gen" / "transcript.srt"
        assert srt_file.is_file()
        content = srt_file.read_text(encoding="utf-8")
        assert "你好" in content and "00:00:01,500" in content


class TestMainUnknownCommand:
    def test_unknown_subcommand_subprocess(self):
        # main() 无参数会启动 MCP stdio server（会卡住），故用 subprocess 验证错误路径
        proc = subprocess.run(
            [sys.executable, "-m", "videonote_mcp.cli", "no-such-cmd"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 2
        assert "未知子命令" in proc.stderr

    def test_providers_list_seeds_builtin_subprocess(self):
        """全新 DB：CLI import 时 seed 内置供应商，list 显示 key=空（掩码）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ)
            env["DATABASE_URL"] = f"sqlite:///{tmp}/empty.db"
            env["VIDEONOTE_DATA_DIR"] = f"{tmp}/data"
            env["VIDEONOTE_CONFIG_DIR"] = f"{tmp}/config"
            proc = subprocess.run(
                [sys.executable, "-m", "videonote_mcp.cli", "providers", "list"],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert proc.returncode == 0
            out = proc.stdout + proc.stderr
            assert "openai" in out and "key=空" in out  # 内置供应商已 seed
            assert "暂无供应商" not in out
