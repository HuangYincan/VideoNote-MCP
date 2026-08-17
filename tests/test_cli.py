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
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import videonote_mcp.cli as cli
from app.db.provider_dao import delete_provider
from app.services.provider import ProviderService
from videonote_mcp.config import get_app_config


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

    def test_set_invalid_size_rejected(self, capsys):
        # set --size 是自由串（download 才有 choices）：非法尺寸持久化后运行时才炸，
        # 与 MCP set_transcriber 同口径入口拒绝（#108）
        with pytest.raises(SystemExit) as ei:
            cli._transcriber_cli(["set", "fast-whisper", "--size", "bogus-size"])
        assert ei.value.code == 1
        assert "未知 whisper 模型尺寸" in capsys.readouterr().err

    def test_set_repo_id_size_accepted(self, capsys):
        # 含 "/" 的 HF repo_id 是合法输入（resolve 直通），不得误伤
        cli._transcriber_cli(["set", "fast-whisper", "--size", "Systran/faster-whisper-small"])
        assert "fast-whisper / Systran/faster-whisper-small" in _cli_out(capsys)

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

    def test_export_unknown_format_rejected(self, capsys):
        # 未知格式此前被 exporter 静默丢弃后仍打印「✓ 已导出 N 个格式」→
        # 用户以为导出齐了实际缺文件；现在入口拒绝并退出（#120 C4）
        with pytest.raises(SystemExit) as ei:
            cli._export_cli(["export", str(uuid.uuid4()), "--format", "srt,bogus"])
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "未知导出格式" in err and "bogus" in err
        assert "已导出" not in err

    def test_export_invalid_task_id_rejected(self, capsys):
        # task_id 进路径拼接前校验格式（与 MCP _validate_task_id 同源正则，防 ../ 逃逸）
        with pytest.raises(SystemExit) as ei:
            cli._export_cli(["export", "../escape/../../etc", "--format", "srt"])
        assert ei.value.code == 1
        assert "非法 task_id" in capsys.readouterr().err

    def test_export_corrupt_cache_falls_back_to_result(self, capsys):
        task_id = str(uuid.uuid4())
        note_out = Path(os.environ["NOTE_OUTPUT_DIR"])
        task_dir = note_out / task_id
        (task_dir / "gen").mkdir(parents=True, exist_ok=True)
        # 规范来源 gen/transcript.json 损坏 → 警告 + 回退 result.json（同源，docs/05 #16）
        (task_dir / "gen" / "transcript.json").write_text("{broken json", encoding="utf-8")
        (task_dir / "result.json").write_text(
            json.dumps(
                {
                    "transcript": {
                        "language": "zh",
                        "full_text": "回退内容",
                        "segments": [{"start": 0.0, "end": 1.0, "text": "回退内容"}],
                    }
                }
            ),
            encoding="utf-8",
        )
        cli._export_cli(["export", task_id, "--format", "srt"])
        out = _cli_out(capsys)
        assert "转写缓存损坏" in out
        assert "✓ 已导出 1 个格式" in out
        srt_file = task_dir / "gen" / "transcript.srt"
        assert srt_file.is_file() and "回退内容" in srt_file.read_text(encoding="utf-8")

    def test_export_corrupt_result_reports_damage(self, capsys):
        task_id = str(uuid.uuid4())
        note_out = Path(os.environ["NOTE_OUTPUT_DIR"])
        task_dir = note_out / task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        # 无 gen/transcript.json，result.json 也损坏 → 报「结果文件损坏」而不是裸 traceback
        (task_dir / "result.json").write_text("not-json-at-all", encoding="utf-8")
        with pytest.raises(SystemExit) as ei:
            cli._export_cli(["export", task_id, "--format", "srt"])
        assert ei.value.code == 1
        err = capsys.readouterr().err
        assert "结果文件损坏" in err


class _InqResult:
    """InquirerPy prompt 的 .execute() 返回体。"""

    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _FakeInq:
    """InquirerPy 替身：按脚本序列逐项回答（select/text/secret 都走 .execute()）。"""

    def __init__(self, script):
        self.script = list(script)

    def _next(self, kind):
        got_kind, value = self.script.pop(0)
        assert got_kind == kind, f"期望 {got_kind!r}，但调用的是 {kind!r}"
        return value

    def select(self, message, choices=None, keybindings=None):
        return _InqResult(self._next("select"))

    def confirm(self, message, default=False, keybindings=None):
        return _InqResult(self._next("confirm"))

    def text(self, message, keybindings=None, default=""):
        return _InqResult(self._next("text"))

    def secret(self, message, keybindings=None):
        return _InqResult(self._next("secret"))


class TestWizardProviderCli:
    """setup 向导内供应商管理：#120 的错误就地消化（重名不崩 / edit 失败如实报）。"""

    def test_wizard_add_duplicate_name_does_not_crash(self, capsys):
        # 已有一个同名供应商 → add 抛 ValueError；向导应消化并提示「未新增」，向导继续
        ProviderService.add_provider(
            name="中转站A", api_key="sk-dup", base_url="https://a.com/v1",
            logo="custom", type_="custom",
        )
        fake = _FakeInq([
            ("select", ("add", None)),
            ("text", "中转站A"),  # 重名
            ("text", "https://relay.example.com/v1"),
            ("secret", "sk-key-2"),
            ("select", ("back", None)),  # 回到主菜单 → 退出
        ])
        cli._wizard_llm(fake)
        out = _cli_out(capsys)
        assert "未新增" in out
        assert "已新增" not in out
        # 已填的 key/base_url 不落库（add_provider 抛错前未 insert）
        rows = ProviderService.get_all_providers()
        assert len(rows) == 1 and rows[0]["name"] == "中转站A"
        assert rows[0]["api_key"] == "sk-dup"

    def test_wizard_add_success_path(self, capsys):
        fake = _FakeInq([
            ("select", ("add", None)),
            ("text", "新供应商"),
            ("text", "https://relay.example.com/v1"),
            ("secret", "sk-new"),
            ("select", ("back", None)),
        ])
        cli._wizard_llm(fake)
        assert "✓ 已新增 新供应商" in _cli_out(capsys)

    def test_edit_provider_failure_reported_not_faked(self, capsys):
        pid = ProviderService.add_provider(
            name="P9", api_key="sk-old", base_url="https://a.com/v1",
            logo="custom", type_="custom",
        )
        with mock.patch.object(ProviderService, "update_provider", return_value=None):
            fake = _FakeInq([("secret", "sk-new"), ("text", "")])  # 换 key，base_url 留空
            cli._edit_provider(fake, pid)
        out = _cli_out(capsys)
        # 失败必须如实报（此前失败也打印「已更新」，用户以为 key 已换，#120）
        assert "更新" in out and "失败" in out
        assert "✓ 已更新" not in out

    def test_edit_provider_success(self, capsys):
        pid = ProviderService.add_provider(
            name="P10", api_key="sk-old", base_url="https://a.com/v1",
            logo="custom", type_="custom",
        )
        fake = _FakeInq([("secret", "sk-new"), ("text", "")])
        cli._edit_provider(fake, pid)
        assert "✓ 已更新" in _cli_out(capsys)
        assert ProviderService.get_provider_by_id(pid)["api_key"] == "sk-new"


class TestWizardCleanupGuard:
    """#122 A6：CLI 清理向导的运行中任务守卫（与 MCP cleanup_note/cleanup_all 对齐）。

    旧实现：向导无守卫，直接清理会把下载器/转写器正在写的目录删掉。
    CLI 看不到 MCP 内存状态，以磁盘 status.json 的终态判定。
    """

    def _make_task(self, status):
        from app.utils.task_manifest import get_note_dir

        tid = f"wizguard{uuid.uuid4().hex[:8]}"
        tdir = get_note_dir() / tid
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "status.json").write_text(json.dumps({"status": status}), encoding="utf-8")
        return tid

    def test_cleanup_all_refuses_running_without_confirm(self, capsys):
        self._make_task("TRANSCRIBING")
        fake = _FakeInq([("confirm", False)])  # 守卫确认：拒绝强清
        with mock.patch("app.utils.task_manifest.cleanup_all_files") as m_clean:
            cli._wizard_data_cleanup_all(fake)
        m_clean.assert_not_called()
        out = _cli_out(capsys)
        assert "未终态" in out and "仍在运行" in out
        assert "✓ 全局清理完成" not in out

    def test_cleanup_all_proceeds_when_user_confirms(self, capsys):
        self._make_task("TRANSCRIBING")
        fake = _FakeInq([
            ("confirm", True),    # 守卫确认：仍要强清
            ("confirm", False),   # include_config
            ("confirm", False),   # include_models
            ("confirm", True),    # 最终确认
        ])
        with mock.patch("app.utils.task_manifest.cleanup_all_files") as m_clean:
            cli._wizard_data_cleanup_all(fake)
        m_clean.assert_called_once()

    def test_cleanup_one_refuses_running_without_confirm(self, capsys):
        tid = self._make_task("PENDING")
        fake = _FakeInq([
            ("select", tid),
            ("confirm", False),  # 运行中守卫：拒绝
        ])
        with mock.patch("app.db.video_task_dao.list_tasks",
                        return_value=[{"task_id": tid, "title": "x", "status": "PENDING"}]), \
             mock.patch("app.utils.task_manifest.cleanup_task_files") as m_clean:
            cli._wizard_data_cleanup_one(fake)
        m_clean.assert_not_called()
        out = _cli_out(capsys)
        assert "仍在运行" in out

    def test_cleanup_one_terminal_task_skips_guard(self, capsys):
        tid = self._make_task("SUCCESS")
        fake = _FakeInq([
            ("select", tid),
            ("confirm", False),  # include_note（终态 → 无守卫确认，直接到 include_note）
            ("confirm", True),   # 最终确认
        ])
        with mock.patch("app.db.video_task_dao.list_tasks",
                        return_value=[{"task_id": tid, "title": "x", "status": "SUCCESS"}]), \
             mock.patch("app.utils.task_manifest.cleanup_task_files") as m_clean:
            cli._wizard_data_cleanup_one(fake)
        m_clean.assert_called_once()


class TestFallbackDefaultModelCli:
    """纯文本兜底向导的默认模型选择：回车=保持现状（此前被当「清除」，#120 S4）。"""
    def _seed(self):
        pid = ProviderService.add_provider(
            name="Fallback1", api_key="sk-fb", base_url="https://a.com/v1",
            logo="custom", type_="custom",
        )
        return pid

    def test_enter_keeps_existing_default(self, capsys, monkeypatch):
        pid = self._seed()
        cli._set_default_model(pid, "existing-model")
        capsys.readouterr()
        monkeypatch.setattr(cli, "probe_models", lambda *a, **k: {"ok": True, "models": ["m1", "m2"]})
        monkeypatch.setattr(cli, "_ask", lambda *a, **k: "")  # 回车=跳过
        cli._fallback_test_and_default(pid)
        assert get_app_config().get(f"default_model:{pid}") == "existing-model"
        assert "未设置" not in _cli_out(capsys)

    def test_explicit_clear_removes_default(self, capsys, monkeypatch):
        pid = self._seed()
        cli._set_default_model(pid, "existing-model")
        capsys.readouterr()
        monkeypatch.setattr(cli, "probe_models", lambda *a, **k: {"ok": True, "models": ["m1", "m2"]})
        monkeypatch.setattr(cli, "_ask", lambda *a, **k: "clear")
        cli._fallback_test_and_default(pid)
        assert get_app_config().get(f"default_model:{pid}") is None

    def test_number_picks_model(self, capsys, monkeypatch):
        pid = self._seed()
        capsys.readouterr()
        monkeypatch.setattr(cli, "probe_models", lambda *a, **k: {"ok": True, "models": ["m1", "m2"]})
        monkeypatch.setattr(cli, "_ask", lambda *a, **k: "2")
        cli._fallback_test_and_default(pid)
        assert get_app_config().get(f"default_model:{pid}") == "m2"


class TestLoginCli:
    """login 失败路径：#120 后向导内调用（exit_on_fail=False）只返回不杀进程。"""

    # _login_cli 函数内 `import requests`：patch 字符串路径替换 sys.modules 里
    # 同一模块对象的 get，函数内 import 绑定到同一对象，同样生效
    def test_failure_returns_without_exit_in_wizard_mode(self, capsys):
        with mock.patch("requests.get", side_effect=RuntimeError("网络失败")):
            cli._login_cli([], exit_on_fail=False)  # 不应 raise SystemExit
        assert "生成二维码失败" in _cli_out(capsys)

    def test_failure_still_exits_in_standalone_mode(self, capsys):
        with mock.patch("requests.get", side_effect=RuntimeError("网络失败")):
            with pytest.raises(SystemExit) as ei:
                cli._login_cli([], exit_on_fail=True)
        assert ei.value.code == 1


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
