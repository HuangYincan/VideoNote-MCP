"""Wave A 契约：file://、未知 task、步骤 SUCCESS、默认 provider、密钥拒绝。"""
import json
import shutil
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import Future
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import videonote_mcp.config as config_mod
import videonote_mcp.server as server
from videonote_mcp import __version__ as pkg_version


class CoerceLocalPathTest(unittest.TestCase):
    def test_plain_path_and_home(self):
        p = server._coerce_local_path("/tmp/foo.mp4")
        self.assertEqual(p, Path("/tmp/foo.mp4"))

    def test_file_uri_unquotes_space(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "foo bar.mp4"
            f.write_bytes(b"x")
            uri = f.as_uri()
            self.assertIn("%20", uri)
            coerced = server._coerce_local_path(uri)
            self.assertTrue(coerced.exists())
            self.assertEqual(coerced, f)
            self.assertTrue(server._local_video_exists(uri))

    def test_windows_drive_strip_is_conditional(self):
        # POSIX 上 /C:/x 不会被剥（os.name != nt）；只保证 unquote
        p = server._coerce_local_path("file:///tmp/hello%20world.mp4")
        self.assertEqual(p, Path("/tmp/hello world.mp4"))

    def test_local_video_exists_rejects_empty_and_dir(self):
        # docs 审计 H 组：空串 → Path(".") 不应误判存在；目录也不该算视频
        self.assertFalse(server._local_video_exists(""))
        with tempfile.TemporaryDirectory() as td:
            self.assertFalse(server._local_video_exists(td))

    def test_local_video_exists_accepts_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "clip.mp4"
            f.write_bytes(b"x")
            self.assertTrue(server._local_video_exists(str(f)))
            self.assertTrue(server._local_video_exists(f.as_uri()))


class AbsolutizeImagesTest(unittest.TestCase):
    def setUp(self):
        self._old = server.DATA_DIR
        server.DATA_DIR = Path(tempfile.mkdtemp(prefix="vn_absolutize_"))

    def tearDown(self):
        shutil.rmtree(server.DATA_DIR, ignore_errors=True)
        server.DATA_DIR = self._old

    def test_normal_screenshot_absolutized(self):
        shot = server.DATA_DIR / "static" / "screenshots"
        shot.mkdir(parents=True)
        md = "![x](static/screenshots/a.png)"
        out = server._absolutize_images(md)
        self.assertIn("file://", out)
        self.assertIn("screenshots/a.png", out)

    def test_traversal_kept_verbatim(self):
        # docs 审计 H 组：../ 逃逸解析到数据目录外 → 原样保留，不生成 file:// 泄露路径
        # rel 前缀 static/screenshots/ 会先消耗两级 ..；要逃出 DATA_DIR/static/screenshots
        # 需 4 级 ..：static/screenshots/..→static→DATA_DIR，再 ..→再上一级
        md = "![x](static/screenshots/../../../../etc/passwd)"
        self.assertEqual(server._absolutize_images(md), md)

    def test_assets_relative_absolutized_with_base_dir(self):
        # 便携笔记模式：markdown 里 Assets/ 相对引用（相对 note_dir=gen/）→ file://
        gen = server.DATA_DIR / "t1" / "gen"
        gen.mkdir(parents=True)
        md = "![帧](Assets/frame_0001.jpg)"
        out = server._absolutize_images(md, base_dir=str(gen))
        self.assertIn("file://", out)
        self.assertIn("Assets/frame_0001.jpg", out)

    def test_assets_without_base_dir_untouched(self):
        # 无 base_dir（旧路径兼容）时 Assets/ 不处理
        md = "![帧](Assets/frame_0001.jpg)"
        self.assertEqual(server._absolutize_images(md), md)

    def test_assets_traversal_kept_verbatim(self):
        gen = server.DATA_DIR / "t2" / "gen"
        gen.mkdir(parents=True)
        md = "![x](Assets/../../secret.png)"
        out = server._absolutize_images(md, base_dir=str(gen))
        # Assets/.. 逃出 gen/ → 原样保留
        self.assertEqual(out, md)


class TaskStatusNotFoundTest(unittest.TestCase):
    def test_unknown_task_is_not_found(self):
        resp = json.loads(server.get_task_status("deadbeef0001"))
        self.assertEqual(resp["status"], "NOT_FOUND")
        self.assertEqual(resp["stage"], "不存在")
        self.assertIsNone(resp["elapsed_secs"])
        self.assertIsNone(resp["result"])


class DefaultProviderTest(unittest.TestCase):
    def test_prefers_default_model_key(self):
        rows = [
            {"id": "a", "api_key": "sk-aaaa"},
            {"id": "b", "api_key": "sk-bbbb"},
        ]
        with mock.patch.object(server.ProviderService, "get_all_providers", return_value=rows):
            with mock.patch.object(server, "get_app_config", return_value={"default_model:b": "gpt-4o"}):
                self.assertEqual(server._resolve_default_provider_id(), "b")

    def test_unique_keyed_provider(self):
        rows = [
            {"id": "a", "api_key": ""},
            {"id": "b", "api_key": "sk-bbbb"},
        ]
        with mock.patch.object(server.ProviderService, "get_all_providers", return_value=rows):
            with mock.patch.object(server, "get_app_config", return_value={}):
                self.assertEqual(server._resolve_default_provider_id(), "b")

    def test_ambiguous_returns_none(self):
        rows = [
            {"id": "a", "api_key": "sk-aaaa"},
            {"id": "b", "api_key": "sk-bbbb"},
        ]
        with mock.patch.object(server.ProviderService, "get_all_providers", return_value=rows):
            with mock.patch.object(server, "get_app_config", return_value={}):
                self.assertIsNone(server._resolve_default_provider_id())


class SecretsRefusedTest(unittest.TestCase):
    def test_diarize_media_rejects_hf_token(self):
        with self.assertRaises(ValueError) as ctx:
            server.diarize_media("/tmp/x.wav", hf_token="hf_xxx")
        self.assertIn("不能经 MCP", str(ctx.exception))


class HealthCheckVersionTest(unittest.TestCase):
    def test_reports_server_version(self):
        data = json.loads(server.health_check())
        self.assertEqual(data["server_version"], pkg_version)
        self.assertIn("queue_length", data)
        self.assertIn("keyed_providers", data)
        self.assertIn("skill_refresh", data)
        self.assertEqual(data["max_workers"], server._MAX_WORKERS)


class NoteDirContractTest(unittest.TestCase):
    """docs 审计 G2：result.note_dir 指向 note.md 所在目录（gen/），
    便携副本经 manifest 定位为 portable_note_dir；video_tasks 列与之一致。"""

    def _fake_result(self, **over):
        from app.models.audio_model import AudioDownloadResult
        from app.models.notes_model import NoteResult
        from app.models.transcriber_model import TranscriptResult

        return NoteResult(
            markdown="# 测试标题\n正文",
            transcript=TranscriptResult(language="zh", full_text="hi", segments=[]),
            audio_meta=AudioDownloadResult(
                file_path="/tmp/x.mp3", title="视频标题", duration=10.0,
                cover_url=None, platform="local", video_id="local-x", raw_info={},
            ),
            **over,
        )

    def tearDown(self):
        from app.db.video_task_dao import delete_task

        for tid in ("nodedir000001", "nodedir000002"):
            try:
                delete_task(tid)
            except Exception:
                pass
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def test_note_dir_points_to_gen(self):
        # 默认模式：note_dir = {task_id}/gen（note.md 真实所在）
        tid = "nodedir000001"
        (server.NOTE_OUTPUT_DIR / tid / "gen").mkdir(parents=True)  # 模拟 note.py 写盘
        (server.NOTE_OUTPUT_DIR / tid / "gen" / "note.md").write_text("# 测试", encoding="utf-8")
        with mock.patch.object(server, "NoteGenerator") as m_gen:
            m_gen.return_value.generate.return_value = self._fake_result()
            server._run_note_task(tid)
        resp = json.loads(server.get_task_status(tid))
        self.assertEqual(
            resp["result"]["note_dir"],
            str(server.NOTE_OUTPUT_DIR / tid / "gen"),
        )
        self.assertNotIn("portable_note_dir", resp["result"])

    def test_portable_note_dir_from_manifest(self):
        # 指定 notes_dir：便携副本经 manifest 定位，补 portable_note_dir
        # （真实流程 note.py 在 generate() 内写便携副本并 record，_run_note_task
        #  在 generate 返回后读 manifest 计算——测试预置 manifest 模拟这一过程）
        tid = "nodedir000002"
        portable = server.NOTE_OUTPUT_DIR.parent / "portable" / "标题"
        try:
            portable.mkdir(parents=True, exist_ok=True)
            (portable / "note.md").write_text("# 便携", encoding="utf-8")
            server.record_task_paths(tid, [portable, portable / "note.md"])
            (server.NOTE_OUTPUT_DIR / tid / "gen").mkdir(parents=True)
            (server.NOTE_OUTPUT_DIR / tid / "gen" / "note.md").write_text("# 测试", encoding="utf-8")
            with mock.patch.object(server, "NoteGenerator") as m_gen:
                m_gen.return_value.generate.return_value = self._fake_result()
                server._run_note_task(tid)
            resp = json.loads(server.get_task_status(tid))
            self.assertEqual(resp["result"]["portable_note_dir"], str(portable))
            self.assertEqual(
                resp["result"]["note_dir"],
                str(server.NOTE_OUTPUT_DIR / tid / "gen"),
            )
        finally:
            shutil.rmtree(portable, ignore_errors=True)


class StyleFormatValidationTest(unittest.TestCase):
    """style/format 白名单（schema enum 只约束客户端，服务端入口显式校验兜底）。"""

    def test_bogus_style_rejected_in_generate_note(self):
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", style="bogus-style")
        self.assertIn("style 必须是", str(cm.exception))

    def test_bogus_format_rejected_in_generate_note(self):
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", format=["bogus"])
        self.assertIn("format 只支持", str(cm.exception))

    def test_valid_style_passes_validation(self):
        # 合法 style 应越过白名单校验，走到后续 provider 解析（报 provider 错误而非 style 错误）
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", style="detailed")
        self.assertNotIn("style 必须是", str(cm.exception))

    def test_valid_format_passes_validation(self):
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", format=["toc", "summary"])
        self.assertNotIn("format 只支持", str(cm.exception))

    def test_none_style_skips_validation(self):
        # 默认路径（None → setup 配置）不被白名单拦截，继续走 provider 解析
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", style=None)
        self.assertNotIn("style 必须是", str(cm.exception))

    def test_string_format_rejected_explicitly(self):
        # 字符串 "toc" 曾穿透到 set() 被拆成字符集报「['c','o','t']」——把合法格式说成非法
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", format="toc")
        self.assertIn("format 必须是字符串列表", str(cm.exception))

    def test_mixed_type_format_no_sort_crash(self):
        # int/str 混排曾让 sorted() 裸 TypeError；元素字符串化后明确报出
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", format=[1, "toc"])
        self.assertIn("收到: ['1']", str(cm.exception))

class ConcurrencyGuardTest(unittest.TestCase):
    def test_guard_raises_when_full(self):
        # 门禁只统计「正在执行」（future.running()）——排队不占名额（docs 审计 F7）
        fake = mock.Mock()
        fake.running.return_value = True
        old = dict(server._task_futures)
        try:
            with server._tasks_lock:
                server._task_futures.clear()
                for i in range(server._MAX_WORKERS):
                    server._task_futures[f"busy{i}"] = fake
            with self.assertRaises(ValueError) as ctx:
                server._guard_concurrency()
            self.assertIn("同时执行", str(ctx.exception))
        finally:
            with server._tasks_lock:
                server._task_futures.clear()
                server._task_futures.update(old)

    def test_guard_allows_queued_not_yet_running(self):
        # 排队中的 future（未 running）不占名额：batch_generate_notes 的批量排队语义
        fake = mock.Mock()
        fake.running.return_value = False
        old = dict(server._task_futures)
        try:
            with server._tasks_lock:
                server._task_futures.clear()
                for i in range(server._MAX_WORKERS * 2):
                    server._task_futures[f"queued{i}"] = fake
            server._guard_concurrency()  # 不抛异常即通过
        finally:
            with server._tasks_lock:
                server._task_futures.clear()
                server._task_futures.update(old)


class ExportEmptyFormatsTest(unittest.TestCase):
    """export_transcript(formats=[])：显式空列表 = 零导出假成功，入口报错（#124 A8）。"""

    def tearDown(self):
        shutil.rmtree(server.NOTE_OUTPUT_DIR / "exportempty001", ignore_errors=True)

    def test_empty_formats_rejected(self):
        tid = "exportempty001"
        server._atomic_write_json(
            server.NOTE_OUTPUT_DIR / tid / "result.json",
            {
                "transcript": {
                    "language": "zh",
                    "full_text": "hi",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "hi"}],
                }
            },
        )
        with self.assertRaises(ValueError) as cm:
            server.export_transcript(tid, formats=[])
        self.assertIn("空列表", str(cm.exception))

    def test_omitted_formats_still_defaults(self):
        """省略 formats 走缺省链（默认 ["srt"]），不因空列表报错拒绝。"""
        tid = "exportempty002"
        server._atomic_write_json(
            server.NOTE_OUTPUT_DIR / tid / "result.json",
            {
                "transcript": {
                    "language": "zh",
                    "full_text": "hi",
                    "segments": [{"start": 0.0, "end": 1.0, "text": "hi"}],
                }
            },
        )
        # C1 门禁（#126）：非 SUCCESS 任务拒绝导出——测试任务补 SUCCESS 状态
        server._atomic_write_json(
            server.NOTE_OUTPUT_DIR / tid / "status.json", {"status": "SUCCESS"}
        )
        try:
            resp = json.loads(server.export_transcript(tid))
            self.assertTrue(resp["ok"])
            self.assertIn("srt", resp["formats"])
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)


class ResultReadErrorTest(unittest.TestCase):
    """get_task_status：SUCCESS 但 result.json 损坏 → result_error 显式提示（#124 A10）。

    status.json 有 #118 内存快照回退，result.json 是唯一不可重建的文件——此前
    result:null 让 Agent 向用户报「笔记已生成」而内容不可读。
    """

    def tearDown(self):
        shutil.rmtree(server.NOTE_OUTPUT_DIR / "resultbad0001", ignore_errors=True)

    def test_corrupt_result_reports_result_error(self):
        tid = "resultbad0001"
        task_dir = server.NOTE_OUTPUT_DIR / tid
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "result.json").write_text("{not-json", encoding="utf-8")
        server._write_status(tid, "SUCCESS", message="完成")
        resp = json.loads(server.get_task_status(tid))
        self.assertEqual(resp["status"], "SUCCESS")
        self.assertIsNone(resp["result"])
        self.assertIn("result_error", resp)
        self.assertIn("读取失败", resp["result_error"])

    def test_healthy_result_has_no_result_error(self):
        tid = "resultbad0002"
        server._atomic_write_json(
            server.NOTE_OUTPUT_DIR / tid / "result.json",
            {"kind": "transcript", "transcript": {"full_text": "ok", "segments": []}},
        )
        try:
            server._write_status(tid, "SUCCESS", message="完成")
            resp = json.loads(server.get_task_status(tid))
            self.assertNotIn("result_error", resp)
            self.assertEqual(resp["result"]["kind"], "transcript")
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)


class PreflightNeedProviderTest(unittest.TestCase):
    """preflight need_provider：material-only 流程跳过供应商检查（#124 A12）。

    prepare_note_material 不调 LLM——默认开着的 provider 检查会给它报
    「无已填 key 的供应商」误导结论。
    """

    def _mock_base_checks(self):
        return mock.patch.object(server.shutil, "which", return_value="/usr/bin/ffmpeg"), mock.patch.object(
            server.shutil, "disk_usage", return_value=mock.Mock(free=5 * 1024**3)
        ), mock.patch.object(
            server.TranscriberConfigManager,
            "is_model_ready",
            return_value={
                "ready": True, "transcriber_type": "fast-whisper", "model_size": "small",
                "downloading": False, "reason": "",
            },
        )

    def test_default_includes_provider_check(self):
        with mock.patch.object(server, "_preflight_provider", return_value=(False, "无已填 key 的供应商")) as m_p:
            with self._mock_base_checks()[0], self._mock_base_checks()[1], self._mock_base_checks()[2]:
                data = json.loads(server.preflight())
        m_p.assert_called_once()
        names = {c["name"] for c in data["checks"]}
        self.assertIn("provider", names)
        self.assertFalse(data["ok"])

    def test_need_provider_false_skips_provider_check(self):
        with mock.patch.object(server, "_preflight_provider") as m_p:
            with self._mock_base_checks()[0], self._mock_base_checks()[1], self._mock_base_checks()[2]:
                data = json.loads(server.preflight(need_provider=False))
        m_p.assert_not_called()
        names = {c["name"] for c in data["checks"]}
        self.assertNotIn("provider", names)
        self.assertTrue(data["ok"])  # 无 provider 依赖时其余检查全过


class PreflightTest(unittest.TestCase):
    """Phase 2 审计#27：提交前预检（ffmpeg/磁盘/转写器/供应商/时长）。"""

    def _ready(self, **over):
        return {
            "ready": True,
            "transcriber_type": "fast-whisper",
            "model_size": "small",
            "downloading": False,
            "reason": "",
            **over,
        }

    def test_env_ok_passes(self):
        with mock.patch.object(server.shutil, "which", return_value="/usr/bin/ffmpeg"):
            with mock.patch.object(
                server.shutil, "disk_usage", return_value=mock.Mock(free=5 * 1024**3)
            ):
                with mock.patch.object(
                    server.TranscriberConfigManager, "is_model_ready", return_value=self._ready()
                ):
                    with mock.patch.object(server, "_preflight_provider", return_value=(True, "openai（key 已填，默认模型 gpt-4o）")):
                        data = json.loads(server.preflight())
        self.assertTrue(data["ok"])
        by_name = {c["name"]: c for c in data["checks"]}
        self.assertTrue(by_name["ffmpeg"]["ok"])
        self.assertTrue(by_name["disk"]["ok"])
        self.assertTrue(by_name["transcriber"]["ok"])
        self.assertTrue(by_name["provider"]["ok"])
        self.assertIn("0/3", by_name["queue"]["detail"])
        self.assertIsNone(data["duration_secs"])

    def test_ffmpeg_missing_and_low_disk_fail(self):
        with mock.patch.object(server.shutil, "which", return_value=None):
            with mock.patch.object(
                server.shutil, "disk_usage", return_value=mock.Mock(free=int(0.5 * 1024**3))
            ):
                with mock.patch.object(
                    server.TranscriberConfigManager, "is_model_ready", return_value=self._ready()
                ):
                    with mock.patch.object(server, "_preflight_provider", return_value=(True, "ok")):
                        data = json.loads(server.preflight())
        self.assertFalse(data["ok"])
        by_name = {c["name"]: c for c in data["checks"]}
        self.assertFalse(by_name["ffmpeg"]["ok"])
        self.assertFalse(by_name["disk"]["ok"])

    def test_transcriber_not_ready_fails(self):
        with mock.patch.object(server.shutil, "which", return_value="/usr/bin/ffmpeg"):
            with mock.patch.object(
                server.shutil, "disk_usage", return_value=mock.Mock(free=10 * 1024**3)
            ):
                with mock.patch.object(
                    server.TranscriberConfigManager,
                    "is_model_ready",
                    return_value=self._ready(ready=False, reason="whisper-small 未下载"),
                ):
                    with mock.patch.object(server, "_preflight_provider", return_value=(True, "ok")):
                        data = json.loads(server.preflight())
        self.assertFalse(data["ok"])
        detail = {c["name"]: c for c in data["checks"]}["transcriber"]["detail"]
        self.assertIn("未下载", detail)

    def test_provider_check_mirrors_generate_resolution(self):
        # 无默认供应商 → 失败
        with mock.patch.object(server, "_resolve_default_provider_id", return_value=None):
            ok, detail = server._preflight_provider(None)
        self.assertFalse(ok)
        self.assertIn("providers set", detail)
        # 供应商不存在 → 失败
        with mock.patch.object(server, "_resolve_default_provider_id", return_value="nosuch"):
            with mock.patch.object(server.ProviderService, "get_provider_by_id", return_value=None):
                ok, detail = server._preflight_provider("nosuch")
        self.assertFalse(ok)
        # key 为空 → 失败
        with mock.patch.object(
            server.ProviderService, "get_provider_by_id", return_value={"id": "p1", "api_key": ""}
        ):
            ok, detail = server._preflight_provider("p1")
        self.assertFalse(ok)
        self.assertIn("providers set p1", detail)
        # key 已填 + 默认模型 → 通过
        with mock.patch.object(
            server.ProviderService, "get_provider_by_id", return_value={"id": "p1", "api_key": "sk-abc"}
        ):
            with mock.patch.object(server, "get_app_config", return_value={"default_model:p1": "gpt-4o"}):
                ok, detail = server._preflight_provider("p1")
        self.assertTrue(ok)
        self.assertIn("gpt-4o", detail)
        # key 已填但无模型 → 失败
        with mock.patch.object(
            server.ProviderService, "get_provider_by_id", return_value={"id": "p1", "api_key": "sk-abc"}
        ):
            with mock.patch.object(server, "get_app_config", return_value={}):
                with mock.patch.object(server, "get_models_by_provider", return_value=[]):
                    ok, detail = server._preflight_provider("p1")
        self.assertFalse(ok)
        self.assertIn("list_models", detail)

    def test_duration_best_effort(self):
        # 解析失败不拦（info ok=false → duration 检查仍 ok）
        with mock.patch.object(server.shutil, "which", return_value="/usr/bin/ffmpeg"):
            with mock.patch.object(
                server.shutil, "disk_usage", return_value=mock.Mock(free=10 * 1024**3)
            ):
                with mock.patch.object(
                    server.TranscriberConfigManager, "is_model_ready", return_value=self._ready()
                ):
                    with mock.patch.object(server, "_preflight_provider", return_value=(True, "ok")):
                        with mock.patch(
                            "app.services.inspect.inspect_video", return_value={"ok": False, "error": "需要登录"}
                        ):
                            data = json.loads(server.preflight(url="https://www.bilibili.com/video/BV1xx411c7mD"))
        self.assertTrue(data["ok"])
        detail = {c["name"]: c for c in data["checks"]}["duration"]["detail"]
        self.assertIn("无法预解析", detail)
        # 解析成功 → duration_secs
        with mock.patch.object(server.shutil, "which", return_value="/usr/bin/ffmpeg"):
            with mock.patch.object(
                server.shutil, "disk_usage", return_value=mock.Mock(free=10 * 1024**3)
            ):
                with mock.patch.object(
                    server.TranscriberConfigManager, "is_model_ready", return_value=self._ready()
                ):
                    with mock.patch.object(server, "_preflight_provider", return_value=(True, "ok")):
                        with mock.patch(
                            "app.services.inspect.inspect_video",
                            return_value={"ok": True, "kind": "single", "entries": [{"duration": 754}]},
                        ):
                            data = json.loads(server.preflight(url="https://x"))
        self.assertEqual(data["duration_secs"], 754)
        detail = {c["name"]: c for c in data["checks"]}["duration"]["detail"]
        self.assertEqual(detail, "12:34")

    def test_fmt_duration(self):
        self.assertEqual(server._fmt_duration(754), "12:34")

    def test_queue_ok_with_queued_not_running(self):
        # 排队任务不占名额（与 _guard_concurrency 同源）：3 个排队 + 0 运行 → 不报已满（#115）
        fake = mock.Mock()
        fake.running.return_value = False
        old = dict(server._task_futures)
        try:
            with server._tasks_lock:
                server._task_futures.clear()
                for i in range(3):
                    server._task_futures[f"q{i}"] = fake
            with mock.patch.object(server.shutil, "which", return_value="/usr/bin/ffmpeg"):
                with mock.patch.object(
                    server.shutil, "disk_usage", return_value=mock.Mock(free=10 * 1024**3)
                ):
                    with mock.patch.object(
                        server.TranscriberConfigManager, "is_model_ready", return_value=self._ready()
                    ):
                        with mock.patch.object(server, "_preflight_provider", return_value=(True, "ok")):
                            data = json.loads(server.preflight())
            by_name = {c["name"]: c for c in data["checks"]}
            self.assertTrue(by_name["queue"]["ok"])
            self.assertIn("3 排队", by_name["queue"]["detail"])
        finally:
            with server._tasks_lock:
                server._task_futures.clear()
                server._task_futures.update(old)

    def test_queue_full_only_when_running_full(self):
        # 运行中占满 _MAX_WORKERS 才报已满
        fake = mock.Mock()
        fake.running.return_value = True
        old = dict(server._task_futures)
        try:
            with server._tasks_lock:
                server._task_futures.clear()
                for i in range(server._MAX_WORKERS):
                    server._task_futures[f"busy{i}"] = fake
            with mock.patch.object(server.shutil, "which", return_value="/usr/bin/ffmpeg"):
                with mock.patch.object(
                    server.shutil, "disk_usage", return_value=mock.Mock(free=10 * 1024**3)
                ):
                    with mock.patch.object(
                        server.TranscriberConfigManager, "is_model_ready", return_value=self._ready()
                    ):
                        with mock.patch.object(server, "_preflight_provider", return_value=(True, "ok")):
                            data = json.loads(server.preflight())
            by_name = {c["name"]: c for c in data["checks"]}
            self.assertFalse(by_name["queue"]["ok"])
            self.assertIn("已满", by_name["queue"]["detail"])
        finally:
            with server._tasks_lock:
                server._task_futures.clear()
                server._task_futures.update(old)
        self.assertEqual(server._fmt_duration(3661), "1:01:01")
        self.assertEqual(server._fmt_duration(None), "未知")
        self.assertEqual(server._fmt_duration(0), "未知")


class NotesDirWarningTest(unittest.TestCase):
    """notes_dir 写数据目录外只提示不拦截（与 export/merge 同口径，docs/05 #45 收口）。"""

    @staticmethod
    def _submit_generate(notes_dir):
        """stub provider/模型/线程池，让 generate_note 干净走到提交点。"""
        done = Future()
        done.set_result(None)
        with mock.patch(
            "videonote_mcp.server._resolve_default_provider_id", return_value="t-provider"
        ), mock.patch(
            "videonote_mcp.server.get_models_by_provider", return_value=[{"model_name": "t-model"}]
        ), mock.patch("videonote_mcp.server._pool.submit", return_value=done):
            return server.generate_note("https://example.com/v", notes_dir=notes_dir)

    def test_outside_data_dir_warns(self):
        out_dir = tempfile.mkdtemp(prefix="vn_notes_out_")
        try:
            with self.assertLogs("videonote_mcp.server", level="WARNING") as logs:
                resp = self._submit_generate(out_dir)
            self.assertTrue(any("数据目录外" in m for m in logs.output))
            self.assertIn('"status": "PENDING"', resp)  # 不拦截，任务照常提交
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def test_inside_data_dir_silent(self):
        with mock.patch.object(server.logger, "warning") as w:
            resp = self._submit_generate(str(server.DATA_DIR))
        self.assertFalse(
            any("数据目录外" in str(c) for c in w.call_args_list)
        )
        self.assertIn('"status": "PENDING"', resp)

    def test_env_fallback_warns_once(self):
        # 缺省链 notes_dir → app_config → VIDEONOTE_NOTES_DIR 解析后仍校验
        out_dir = tempfile.mkdtemp(prefix="vn_notes_env_")
        try:
            with mock.patch.object(server, "get_app_config", return_value={}), mock.patch.dict(
                "os.environ", {"VIDEONOTE_NOTES_DIR": out_dir}, clear=False
            ):
                with self.assertLogs("videonote_mcp.server", level="WARNING") as logs:
                    resp = self._submit_generate(None)
            self.assertTrue(any("数据目录外" in m for m in logs.output))
            self.assertIn('"status": "PENDING"', resp)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


class GridSizeValidationTest(unittest.TestCase):
    """grid_size 非法值（[0,0]/[1]/[1,2,3]）在 VideoReader 深处才炸成泛化错误——
    与 style/format 同口径，入口显式校验（#100）。"""

    def test_bogus_grid_rejected_in_generate_note(self):
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", grid_size=[0, 0])
        self.assertIn("grid_size 必须是两个正整数", str(cm.exception))

    def test_short_grid_rejected_in_generate_note(self):
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", grid_size=[3])
        self.assertIn("grid_size 必须是两个正整数", str(cm.exception))

    def test_long_grid_rejected_in_generate_note(self):
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", grid_size=[2, 2, 2])
        self.assertIn("grid_size 必须是两个正整数", str(cm.exception))

    def test_bogus_grid_rejected_in_prepare_material(self):
        with self.assertRaises(ValueError) as cm:
            server.prepare_note_material("https://example.com/v", grid_size=[0, 3])
        self.assertIn("grid_size 必须是两个正整数", str(cm.exception))

    def test_valid_grid_passes_to_provider_resolution(self):
        # 合法 grid_size 应越过校验，走到后续 provider 解析（报 provider 错误而非 grid 错误）
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", grid_size=[3, 3])
        self.assertNotIn("grid_size 必须是", str(cm.exception))

    def test_none_grid_skips_validation(self):
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", grid_size=None)
        self.assertNotIn("grid_size 必须是", str(cm.exception))

class ListTasksPaginationTest(unittest.TestCase):
    """list_tasks 的 limit/offset 分页（#102）：缺省全量向后兼容；limit 钳制 ≥1。"""

    def setUp(self):
        from app.db.video_task_dao import insert_video_task

        self.tids = []
        for i in range(3):
            tid = f"paging_{uuid.uuid4().hex}"
            # video_id 必须唯一：shared DB 里 get_task_by_video 取「最新」，
            # 与 test_task_index 的 BV1/BV2 撞名会抢到对方断言（created_at 秒级并列按 rowid 序）
            insert_video_task(f"LTP_{i}_{uuid.uuid4().hex}", "bilibili", tid, title=f"任务{i}")
            self.tids.append(tid)

    def test_all_when_limit_none(self):
        tasks = json.loads(server.list_tasks())
        by_id = {t["task_id"] for t in tasks}
        self.assertTrue({self.tids[0], self.tids[1], self.tids[2]} <= by_id)

    def test_limit_truncates(self):
        tasks = json.loads(server.list_tasks(limit=2))
        # 共享 DB 里其它测试也有行，created_at 秒级并列使「最新在前」断言不可控——
        # 只验证切片机制：条数=2，且全量列表必然多于 2（证明截断生效）
        self.assertEqual(len(tasks), 2)
        full = json.loads(server.list_tasks())
        self.assertGreater(len(full), 2)
        self.assertTrue({t["task_id"] for t in tasks} <= {t["task_id"] for t in full})

    def test_offset_skips(self):
        all_tasks = json.loads(server.list_tasks())
        paged = json.loads(server.list_tasks(offset=2))
        self.assertEqual(paged, all_tasks[2:])

    def test_zero_limit_returns_empty(self):
        # limit=0 显式「取 0 条」→ 空列表（不再被钳成 1、误导「没有任务」判断，#127 A6）
        tasks = json.loads(server.list_tasks(limit=0))
        self.assertEqual(tasks, [])


class ExportFormatsWhitelistTest(unittest.TestCase):
    """export_transcript 未知格式曾只写 stderr 警告后静默丢弃——Agent 以为导出成功
    实际缺文件；入口显式报错（#104）。"""

    @staticmethod
    def _task_with_transcript():
        tid = f"exp_{uuid.uuid4().hex}"
        task_dir = server.NOTE_OUTPUT_DIR / tid
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "result.json").write_text(
            json.dumps(
                {
                    "transcript": {
                        "language": "zh",
                        "segments": [{"start": 0, "end": 1, "text": "hi"}],
                        "full_text": "hi",
                    }
                }
            ),
            encoding="utf-8",
        )
        # C1 门禁（#126）：非 SUCCESS 任务拒绝导出——测试任务补 SUCCESS 状态
        (task_dir / "status.json").write_text(
            json.dumps({"status": "SUCCESS"}, ensure_ascii=False), encoding="utf-8"
        )
        return tid

    def test_unknown_format_rejected(self):
        tid = self._task_with_transcript()
        try:
            with self.assertRaises(ValueError) as cm:
                server.export_transcript(tid, formats=["pdf"])
            self.assertIn("srt", str(cm.exception))
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def test_mixed_unknown_rejected(self):
        # 请求里混入未知格式 → 整单拒绝（不静默导出半份）
        tid = self._task_with_transcript()
        try:
            with self.assertRaises(ValueError) as cm:
                server.export_transcript(tid, formats=["srt", "pdf"])
            self.assertIn("pdf", str(cm.exception))
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def test_non_list_formats_rejected(self):
        tid = self._task_with_transcript()
        try:
            with self.assertRaises(ValueError) as cm:
                server.export_transcript(tid, formats="srt")
            self.assertIn("formats 必须是字符串列表", str(cm.exception))
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def test_valid_formats_export(self):
        tid = self._task_with_transcript()
        try:
            resp = json.loads(server.export_transcript(tid, formats=["srt"]))
            self.assertTrue(resp["ok"])
            self.assertIn("srt", resp["formats"])
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def test_default_formats_still_export(self):
        # 缺省（None）走 config/env 默认链，白名单校验只作用于显式传入
        tid = self._task_with_transcript()
        try:
            with mock.patch.object(server, "get_app_config", return_value={}), mock.patch.dict(
                "os.environ", {"VIDEONOTE_DEFAULT_EXPORT_FORMATS": '["srt"]'}, clear=False
            ):
                resp = json.loads(server.export_transcript(tid))
            self.assertTrue(resp["ok"])
            self.assertIn("srt", resp["formats"])
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)


class MergeAudioFileUriTest(unittest.TestCase):
    """merge_audio 的 file:// 规整：app 层只认普通路径，直接传 URI 曾误报「文件不存在」（#105）。"""

    def test_file_uri_paths_coerced_before_merge(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a b.mp3"  # 空格 → URI 编码 %20，必须 unquote
            b = Path(td) / "b.mp3"
            a.write_bytes(b"x")
            b.write_bytes(b"x")
            with mock.patch(
                "app.services.merge.merge_audio", return_value=str(Path(td) / "merged.wav")
            ) as m:
                resp = json.loads(server.merge_audio([a.as_uri(), b.as_uri()], out_dir=td))
            self.assertTrue(resp["ok"])
            self.assertEqual(m.call_args.args[0], [str(a), str(b)])

    def test_plain_paths_passthrough_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a.mp3"
            b = Path(td) / "b.mp3"
            a.write_bytes(b"x")
            b.write_bytes(b"x")
            with mock.patch(
                "app.services.merge.merge_audio", return_value=str(Path(td) / "merged.wav")
            ) as m:
                resp = json.loads(server.merge_audio([str(a), str(b)], out_dir=td))
            self.assertTrue(resp["ok"])
            self.assertEqual(m.call_args.args[0], [str(a), str(b)])


class MergeAudioInputValidationTest(unittest.TestCase):
    """merge_audio 的目录/不存在输入：merge.py 用 os.path.exists（目录为 True）穿透
    到 ffmpeg 深处才炸「转换失败」泛化错误——入口 is_file 与 diarize_media 同口径（#109）。"""

    def test_directory_input_rejected_clearly(self):
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "b.mp3"
            b.write_bytes(b"x")
            resp = json.loads(server.merge_audio([td, str(b)]))
        self.assertFalse(resp["ok"])
        self.assertIn("不是文件", resp["error"])
        self.assertNotIn("ffmpeg", resp["error"])

    def test_missing_input_rejected_clearly(self):
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "b.mp3"
            b.write_bytes(b"x")
            resp = json.loads(server.merge_audio([str(Path(td) / "nope.mp3"), str(b)]))
        self.assertFalse(resp["ok"])
        self.assertIn("不存在", resp["error"])

    def test_file_uri_directory_rejected(self):
        # 目录经 file:// 规整后仍应按 is_file 拦截（#107 规整 + #109 is_file 叠加）
        with tempfile.TemporaryDirectory() as td:
            b = Path(td) / "b.mp3"
            b.write_bytes(b"x")
            resp = json.loads(server.merge_audio([Path(td).as_uri(), str(b)]))
        self.assertFalse(resp["ok"])
        self.assertIn("不是文件", resp["error"])


class BatchSingleEntryErrorTest(unittest.TestCase):
    """batch_generate_notes 单集退化路径：_submit raise 曾裸传中断调用（与多条目
    「收集继续」契约不符）——收进 errors 返回同形状（#109）。"""

    def test_single_entry_submit_failure_collected(self):
        with mock.patch(
            "app.services.inspect.inspect_video",
            return_value={"ok": True, "kind": "single", "title": "t", "platform": "bilibili"},
        ), mock.patch.object(
            server, "generate_note", side_effect=ValueError("供应商还没有可用模型")
        ):
            resp = json.loads(server.batch_generate_notes("https://example.com/v"))
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["submitted"], 0)
        self.assertEqual(len(resp["errors"]), 1)
        self.assertIn("还没有可用模型", resp["errors"][0]["error"])

    def test_single_entry_success_shape(self):
        with mock.patch(
            "app.services.inspect.inspect_video",
            return_value={"ok": True, "kind": "single", "title": "t", "platform": "bilibili"},
        ), mock.patch.object(
            server, "generate_note", return_value='{"task_id": "x", "status": "PENDING"}'
        ):
            resp = json.loads(server.batch_generate_notes("https://example.com/v"))
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["submitted"], 1)
        self.assertEqual(resp["tasks"][0]["task_id"], "x")


class DiarizeMediaIsFileTest(unittest.TestCase):
    """diarize_media 目录输入：.exists() 对目录为 True，曾穿透到 ffmpeg 深处炸泛化错误；
    与 transcribe_media/extract_frames 同口径改 is_file（#105）。"""

    def test_directory_input_rejected_as_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            resp = json.loads(server.diarize_media(td))
        self.assertFalse(resp["ok"])
        self.assertIn("本地文件不存在", resp["error"])

    def test_missing_file_rejected(self):
        resp = json.loads(server.diarize_media("/nonexistent/audio.mp3"))
        self.assertFalse(resp["ok"])
        self.assertIn("本地文件不存在", resp["error"])


class OutputDirFileUriTest(unittest.TestCase):
    """输出目录类参数（notes_dir / out_dir）的 file:// 规整：URI 直传曾按字面
    `file:` 相对目录创建垃圾目录，且「数据目录外」检查基于未规整值误报（#107）。"""

    @staticmethod
    def _submit_generate_capture(notes_dir):
        """stub provider/模型/线程池，捕获 _pool.submit 的 kwargs（校验 notes_dir 规整结果）。"""
        done = Future()
        done.set_result(None)
        with mock.patch(
            "videonote_mcp.server._resolve_default_provider_id", return_value="t-provider"
        ), mock.patch(
            "videonote_mcp.server.get_models_by_provider", return_value=[{"model_name": "t-model"}]
        ), mock.patch("videonote_mcp.server._pool.submit", return_value=done) as m:
            server.generate_note("https://example.com/v", notes_dir=notes_dir)
        return m.call_args.kwargs

    def test_generate_note_file_uri_notes_dir_coerced(self):
        with tempfile.TemporaryDirectory() as td:
            out_dir = Path(td) / "my notes"  # 空格 → URI 编码 %20，必须 unquote
            kwargs = self._submit_generate_capture(out_dir.as_uri())
        self.assertEqual(kwargs["notes_dir"], str(out_dir))
        self.assertFalse(Path("file:").exists(), "字面 file: 目录不应存在（URI 未规整的回归标志）")

    def test_generate_note_plain_notes_dir_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            kwargs = self._submit_generate_capture(td)
        self.assertEqual(kwargs["notes_dir"], td)

    def test_merge_audio_file_uri_out_dir_coerced(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a.mp3"
            b = Path(td) / "b.mp3"
            a.write_bytes(b"x")
            b.write_bytes(b"x")
            out_dir = Path(td) / "out dir"
            with mock.patch(
                "app.services.merge.merge_audio", return_value=str(Path(td) / "merged.wav")
            ) as m:
                resp = json.loads(server.merge_audio([str(a), str(b)], out_dir=out_dir.as_uri()))
            self.assertTrue(resp["ok"])
            self.assertEqual(m.call_args.kwargs["out_dir"], str(out_dir))

    def test_export_transcript_file_uri_out_dir_coerced(self):
        tid = ExportFormatsWhitelistTest._task_with_transcript()
        td = tempfile.mkdtemp(prefix="vn_out_uri_")
        try:
            out_dir = Path(td) / "sub dir"
            resp = json.loads(server.export_transcript(tid, formats=["srt"], out_dir=out_dir.as_uri()))
            self.assertTrue(resp["ok"])
            # 输出落在规整后的目录（export 文件名是 transcript.srt），而不是 CWD 下字面 file: 目录
            self.assertTrue((out_dir / "transcript.srt").exists())
            self.assertFalse(Path("file:").exists(), "字面 file: 目录不应存在（URI 未规整的回归标志）")
        finally:
            shutil.rmtree(td, ignore_errors=True)
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)


class DefaultExportFormatsJunkTest(unittest.TestCase):
    """app_config.default_export_formats 非列表垃圾值曾遮蔽 env 回退链（truthy 短路 `or`）
    或在工具入口炸 ValueError——打 warning 后回退，与 #104 缺省链口径一致（#107；
    守卫在 #124 A4 迁到 videonote_mcp.config.resolve_default_export_formats，
    CLI/MCP/自动导出三处同源，故 mock 目标从 server 改为 config_mod）。"""

    def test_tool_junk_config_falls_back_to_env(self):
        tid = ExportFormatsWhitelistTest._task_with_transcript()
        try:
            with mock.patch.object(
                config_mod, "get_app_config", return_value={"default_export_formats": "srt,vtt"}
            ), mock.patch.dict(
                "os.environ", {"VIDEONOTE_DEFAULT_EXPORT_FORMATS": '["vtt"]'}, clear=False
            ), mock.patch.object(config_mod.logger, "warning") as w:
                resp = json.loads(server.export_transcript(tid))
            self.assertTrue(resp["ok"])
            self.assertIn("vtt", resp["formats"])
            self.assertTrue(any("不是列表" in str(c) for c in w.call_args_list))
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def test_tool_junk_config_no_env_uses_default_srt(self):
        # 无 env 兜底时收敛到 ["srt"] 默认，而不是把垃圾值当格式炸掉
        tid = ExportFormatsWhitelistTest._task_with_transcript()
        try:
            with mock.patch.object(
                config_mod, "get_app_config", return_value={"default_export_formats": {"a": "b"}}
            ), mock.patch.dict("os.environ", {"VIDEONOTE_DEFAULT_EXPORT_FORMATS": ""}, clear=False):
                resp = json.loads(server.export_transcript(tid))
            self.assertTrue(resp["ok"])
            self.assertIn("srt", resp["formats"])
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def test_auto_export_junk_config_falls_back_to_env(self):
        with mock.patch.object(
            config_mod, "get_app_config", return_value={"default_export_formats": "srt"}
        ), mock.patch.dict(
            "os.environ", {"VIDEONOTE_DEFAULT_EXPORT_FORMATS": '["vtt"]'}, clear=False
        ), mock.patch("videonote_mcp.export.export_transcript") as m, mock.patch.object(
            config_mod.logger, "warning"
        ) as w:
            server._auto_export_transcript("junk-test", {"language": "zh"})
        self.assertEqual(m.call_args.kwargs["formats"], ["vtt"])
        self.assertTrue(any("不是列表" in str(c) for c in w.call_args_list))


class IntConfigFallbackTest(unittest.TestCase):
    """app_config 整数配置（video_interval/comments_limit）垃圾值遮蔽回退链 + falsy 0 优先级倒挂（#116）。

    `get(...) or env_int(...)` 的 truthy 短路：垃圾值（"abc"）让 int() 裸 ValueError；
    0（显式关闭）被当 falsy 吞掉、app_config 优先级倒挂给 env。is not None + 防御性
    int() 两处一起修，与 #107 default_export_formats 同族。
    """

    def _resolve(self, cfg, env_value):
        """env 用值或空串显式覆盖，防本机环境残留（env_or 空串→None→回退默认）。

        #120 后实现上移到 videonote_mcp.config.resolve_int_config，mock 目标随之切换
        （server._resolve_int_config 现在是薄包装）。
        """
        with mock.patch.object(config_mod, "get_app_config", return_value=cfg), mock.patch.dict(
            "os.environ", {"VIDEONOTE_VIDEO_INTERVAL": env_value}, clear=False
        ), mock.patch.object(config_mod.logger, "warning") as w:
            value = server._resolve_int_config("video_interval", "VIDEONOTE_VIDEO_INTERVAL", 0)
        return value, w

    def test_junk_config_falls_back_to_env(self):
        value, w = self._resolve({"video_interval": "abc"}, "6")
        self.assertEqual(value, 6)
        self.assertTrue(any("非整数" in str(c) for c in w.call_args_list))

    def test_junk_config_no_env_uses_default(self):
        value, _ = self._resolve({"video_interval": {"a": 1}}, "")
        self.assertEqual(value, 0)

    def test_falsy_zero_not_shadowed_by_env(self):
        # app_config 显式配 0（关闭视频理解）不能被 env 覆盖——旧 `or` 短路回归标志
        value, w = self._resolve({"video_interval": 0}, "6")
        self.assertEqual(value, 0)
        self.assertEqual(w.call_count, 0)

    def test_normal_int_and_str_passthrough(self):
        for cfg, expected in ((6, 6), ("8", 8)):
            with self.subTest(cfg=cfg):
                value, _ = self._resolve({"video_interval": cfg}, "")
                self.assertEqual(value, expected)

    def test_generate_note_junk_comments_limit_falls_back_to_env(self):
        # 契约级：垃圾配置不能遮蔽 env 回退（缺省链 参数 → app_config → env → 默认）
        done = Future()
        done.set_result(None)
        with mock.patch.object(
            server, "get_app_config", return_value={"comments_limit": "abc"}
        ), mock.patch.dict(
            "os.environ", {"VIDEONOTE_COMMENTS_LIMIT": "5"}, clear=False
        ), mock.patch(
            "videonote_mcp.server._resolve_default_provider_id", return_value="t-provider"
        ), mock.patch(
            "videonote_mcp.server.get_models_by_provider", return_value=[{"model_name": "t-model"}]
        ), mock.patch("videonote_mcp.server._pool.submit", return_value=done) as m:
            server.generate_note("https://example.com/v")
        self.assertEqual(m.call_args.kwargs["comments_limit"], 5)

    def test_generate_note_zero_interval_not_shadowed_by_env(self):
        # 契约级：app_config video_interval=0（显式关视频理解）不能被 env=6 覆盖
        done = Future()
        done.set_result(None)
        with mock.patch.object(
            config_mod, "get_app_config", return_value={"video_interval": 0}
        ), mock.patch.dict(
            "os.environ", {"VIDEONOTE_VIDEO_INTERVAL": "6"}, clear=False
        ), mock.patch(
            "videonote_mcp.server._resolve_default_provider_id", return_value="t-provider"
        ), mock.patch(
            "videonote_mcp.server.get_models_by_provider", return_value=[{"model_name": "t-model"}]
        ), mock.patch("videonote_mcp.server._pool.submit", return_value=done) as m:
            server.generate_note("https://example.com/v")
        self.assertEqual(m.call_args.kwargs["video_interval"], 0)


class WriteStatusStartedAtTest(unittest.TestCase):
    """_write_status 的 started_at 保留 + 写盘失败不裸抛 + get_task_status 内存快照回退（#118）。"""

    def setUp(self):
        import uuid as _uuid

        self.tid = f"ws-{_uuid.uuid4().hex[:8]}"
        self.task_dir = server.NOTE_OUTPUT_DIR / self.tid

    def tearDown(self):
        shutil.rmtree(self.task_dir, ignore_errors=True)
        with server._tasks_lock:
            server._status_memory.pop(self.tid, None)

    def test_started_at_preserved_across_writes(self):
        # 每次重打时间戳曾让成功任务终态 elapsed≈0（PENDING→INITIALIZING→…→SUCCESS 全由本函数写）
        server._write_status(self.tid, "PENDING", message="任务排队中")
        first = json.loads((self.task_dir / "status.json").read_text(encoding="utf-8"))["started_at"]
        server._write_status(self.tid, "SUCCESS", message="完成")
        second = json.loads((self.task_dir / "status.json").read_text(encoding="utf-8"))["started_at"]
        self.assertEqual(first, second)

    def test_write_failure_does_not_raise_and_keeps_memory_snapshot(self):
        # 磁盘满/只读时裸抛会进后台线程被吞、FAILED 重写循环同样失败——不抛 + 快照可查
        with mock.patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            server._write_status(self.tid, "PENDING", message="x")  # 不应抛
        with server._tasks_lock:
            self.assertEqual(server._status_memory[self.tid]["status"], "PENDING")

    def test_memory_snapshot_capped_under_disk_failure(self):
        # 持续写盘失败时快照只增不删会无界积累——上限淘汰最旧（#123 A9）
        caps = [f"cap-{i:04d}" for i in range(server._STATUS_MEMORY_MAX + 20)]
        with mock.patch("pathlib.Path.write_text", side_effect=OSError("disk full")), \
             mock.patch("app.db.video_task_dao.update_task_status"):
            for tid in caps:
                server._write_status(tid, "PENDING", message="x")
        with server._tasks_lock:
            n = len(server._status_memory)
        self.assertLessEqual(n, server._STATUS_MEMORY_MAX)
        with server._tasks_lock:  # 清理本轮快照，避免污染后续测试
            for tid in caps:
                server._status_memory.pop(tid, None)

    def test_terminal_status_pops_memory_snapshot(self):
        # 终态落盘成功后弹内存快照：防长生命周期 server 无界增长
        # （写盘失败保留快照的路径由 test_write_failure_... 覆盖，#121 C9）
        server._write_status(self.tid, "SUCCESS", message="完成")
        with server._tasks_lock:
            self.assertNotIn(self.tid, server._status_memory)
        # 非终态不弹：快照供运行中读盘损坏时的 get_task_status 回退
        server._write_status(self.tid, "TRANSCRIBING", message="转写中")
        with server._tasks_lock:
            self.assertIn(self.tid, server._status_memory)

    def test_get_task_status_falls_back_to_memory_snapshot(self):
        # 状态文件损坏（写一半）曾误报「状态文件读取失败」PENDING——回退最近一次写盘快照
        server._write_status(self.tid, "PENDING", message="任务排队中")
        (self.task_dir / "status.json").write_text("{", encoding="utf-8")
        resp = json.loads(server.get_task_status(self.tid))
        self.assertEqual(resp["status"], "PENDING")
        self.assertEqual(resp["message"], "任务排队中")  # 内存快照而非误导文案


class CleanupAllDryRunTest(unittest.TestCase):
    """#137：cleanup_all dry_run 预览——破坏性最强的工具，执行前先预览。"""

    def test_dry_run_deletes_nothing(self):
        # 造任务产物 + 假 running 任务：dry_run 全部列出但不拒绝、不删
        old = dict(server._task_futures)
        with server._tasks_lock:
            server._task_futures.clear()
            from concurrent.futures import Future

            f = Future()  # 未 set_result → 进行中
            server._task_futures["live-1"] = f
        try:
            resp = json.loads(server.cleanup_all(dry_run=True))
        finally:
            with server._tasks_lock:
                server._task_futures.clear()
                server._task_futures.update(old)
        self.assertTrue(resp["ok"])
        self.assertTrue(resp["dry_run"])
        self.assertEqual(resp["running"], 1)
        self.assertEqual(resp["running_task_ids"], ["live-1"])
        # 默认保留 config/models，清理不含它们
        self.assertIn("note_results/", resp["would_clean"])
        self.assertNotIn("config/", resp["would_clean"])
        self.assertIn("config/", resp["would_keep"])
        self.assertIn("models/", resp["would_keep"])
        self.assertTrue(any(k.startswith("logs/") for k in resp["would_keep"]))

    def test_dry_run_respects_include_flags(self):
        resp = json.loads(server.cleanup_all(include_config=True, include_models=True, dry_run=True))
        self.assertIn("config/（LLM key / cookie / 转写设置）", resp["would_clean"])
        self.assertIn("models/（已下载模型）", resp["would_clean"])
        self.assertNotIn("config/", resp["would_keep"])
        self.assertNotIn("models/", resp["would_keep"])

    def test_dry_run_without_include_keeps_kept_lists(self):
        resp = json.loads(server.cleanup_all(dry_run=True))
        # would_clean 恒含任务产物，would_keep 恒含 logs（#121 C3）
        self.assertIn("video_tasks 全局索引", resp["would_clean"])
        self.assertIn("logs/（运行日志，刻意不清）", resp["would_keep"])


class CleanupRunningTaskGuardTest(unittest.TestCase):
    """cleanup_note / cleanup_all 对运行中（或排队中）任务拒绝清理——直接删会破坏
    下载器/转写器正在写的目录，任务中途失败或产生残留状态（#111）。"""

    def _fresh_future(self):
        from concurrent.futures import Future

        return Future()  # 未 set_result → 与进行中/排队等价（not done）

    def _done_future(self):
        from concurrent.futures import Future

        f = Future()
        f.set_result(None)
        return f

    def _inject(self, futures):
        old = dict(server._task_futures)
        with server._tasks_lock:
            server._task_futures.clear()
            server._task_futures.update(futures)
        return old

    def _restore(self, old):
        with server._tasks_lock:
            server._task_futures.clear()
            server._task_futures.update(old)

    def test_cleanup_note_refuses_running_task(self):
        old = self._inject({"live-1": self._fresh_future()})
        try:
            with mock.patch.object(server, "cleanup_task_files") as m:
                resp = json.loads(server.cleanup_note("live-1"))
            self.assertFalse(resp["ok"])
            self.assertIn("cancel_note", resp["error"])
            m.assert_not_called()
        finally:
            self._restore(old)

    def test_cleanup_note_allows_terminal_task(self):
        old = self._inject({"done-1": self._done_future()})
        try:
            with mock.patch.object(
                server, "cleanup_task_files",
                return_value={"task_id": "done-1", "include_note": False,
                              "note_kept": True, "deleted": [], "missing": [], "errors": []},
            ) as m:
                resp = json.loads(server.cleanup_note("done-1"))
            self.assertTrue(resp["note_kept"])  # 正常清理形状，未被拦截
            m.assert_called_once_with("done-1", include_note=False)
        finally:
            self._restore(old)

    def test_cleanup_all_refuses_while_tasks_running(self):
        old = self._inject({"live-1": self._fresh_future(), "live-2": self._fresh_future()})
        try:
            with mock.patch.object(server, "cleanup_all_files") as m:
                resp = json.loads(server.cleanup_all())
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["running"], 2)
            self.assertIn("cancel_note", resp["error"])
            m.assert_not_called()
        finally:
            self._restore(old)

    def test_cleanup_all_allows_when_idle(self):
        old = self._inject({})
        try:
            with mock.patch.object(server, "cleanup_all_files",
                                   return_value={"cleaned": [], "kept": []}) as m:
                resp = json.loads(server.cleanup_all())
            self.assertTrue(resp["ok"])  # 成功路径带 ok:true，与拒绝路径 {ok:false} 对称（#126 C2）
            m.assert_called_once_with(include_config=False, include_models=False)
        finally:
            self._restore(old)

    def test_cleanup_all_refuses_while_model_downloading(self):
        """include_models=True 且仍有模型后台下载 → 拒绝，避免删 models/ 打断下载（#123 A1）。"""
        old = self._inject({})
        try:
            with mock.patch.object(server, "cleanup_all_files") as m, \
                 mock.patch.object(server.dl_state, "downloading_keys", return_value=["small", "mlx-tiny"]):
                resp = json.loads(server.cleanup_all(include_models=True))
            self.assertFalse(resp["ok"])
            self.assertEqual(resp["downloading_models"], ["small", "mlx-tiny"])
            self.assertIn("正在后台下载", resp["error"])
            m.assert_not_called()
        finally:
            self._restore(old)

    def test_cleanup_all_models_allowed_when_download_idle(self):
        """无模型下载中时 include_models=True 正常放行。"""
        old = self._inject({})
        try:
            with mock.patch.object(server, "cleanup_all_files",
                                   return_value={"cleaned": [], "kept": []}) as m, \
                 mock.patch.object(server.dl_state, "downloading_keys", return_value=[]):
                resp = json.loads(server.cleanup_all(include_models=True))
            self.assertTrue(resp["ok"])  # 成功路径带 ok:true（#126 C2）
            m.assert_called_once_with(include_config=False, include_models=True)
        finally:
            self._restore(old)


class TranscriptUnavailableReasonTest(unittest.TestCase):
    """「无转写」文案按 status.json 区分原因：运行中→建议等终态；失败/取消→如实报；
    不存在→状态不可读。此前一律「可能未成功」——运行中的任务被误报成失败（#114）。"""

    def _status(self, tid, status=None):
        d = server.NOTE_OUTPUT_DIR / tid
        d.mkdir(parents=True, exist_ok=True)
        if status:
            (d / "status.json").write_text(json.dumps({"status": status}), encoding="utf-8")
        return d

    def tearDown(self):
        for d in server.NOTE_OUTPUT_DIR.iterdir():
            if d.name.startswith("tx"):
                shutil.rmtree(d, ignore_errors=True)

    def test_export_running_task_advises_waiting(self):
        self._status("tx0001", "DOWNLOADING")
        resp = json.loads(server.export_transcript("tx0001"))
        self.assertIn("仍在运行", resp["error"])
        self.assertIn("DOWNLOADING", resp["error"])
        self.assertIn("get_task_status", resp["error"])

    def test_export_failed_task_reports_failure(self):
        self._status("tx0002", "FAILED")
        resp = json.loads(server.export_transcript("tx0002"))
        self.assertFalse(resp["ok"])  # 失败形状与成功路径对齐（#121 C4）
        self.assertIn("未成功（FAILED）", resp["error"])

    def test_export_missing_task_reports_unreadable(self):
        resp = json.loads(server.export_transcript("tx0003"))
        self.assertIn("不可读", resp["error"])

    def test_resource_running_task_advises_waiting(self):
        self._status("tx0004", "TRANSCRIBING")
        out = server.transcript_resource("tx0004")
        self.assertIn("仍在运行", out)
        self.assertIn("TRANSCRIBING", out)

    def test_get_task_transcript_running_task(self):
        self._status("tx0005", "SUMMARIZING")
        resp = json.loads(server.get_task_transcript("tx0005"))
        self.assertFalse(resp["ok"])
        self.assertIn("仍在运行", resp["message"])

    def test_segment_range_invalid_reports_real_status_success(self):
        """有转写但 segment_range 非法：status 读真实状态（SUCCESS），不硬编码 UNKNOWN（#123 A8）。"""
        d = self._status("tx0006", "SUCCESS")
        (d / "gen").mkdir(parents=True, exist_ok=True)
        (d / "gen" / "transcript.json").write_text(
            json.dumps({"language": "zh", "full_text": "x",
                        "segments": [{"start": 0, "end": 1, "text": "x"}]}),
            encoding="utf-8",
        )
        resp = json.loads(server.get_task_transcript("tx0006", segment_range="bogus"))
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["status"], "SUCCESS")
        self.assertIn("segment_range 非法", resp["message"])

    def test_no_transcript_reports_real_status(self):
        """无转写时 status 字段也用真实状态而非 UNKNOWN。"""
        self._status("tx0007", "FAILED")
        resp = json.loads(server.get_task_transcript("tx0007"))
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["status"], "FAILED")


class ScreenshotLinkFormatMergeTest(unittest.TestCase):
    """screenshot/link 布尔开关并入 _format 列表（#120 G1）。

    布尔不开 → format 原样；布尔开 → 对应项追加；已有同项 → 不重复。
    否则 prompt 不注入标记指令 → LLM 不输出标记 → 视频白下载但笔记无图。
    双向闭合的另一半（format→布尔归一化）在 note.py，由 test_material_mode 覆盖。
    """

    def setUp(self):
        self.captured = None
        self._pool_submit = mock.patch.object(
            server._pool, "submit", side_effect=self._capture_submit
        )
        self._pool_submit.start()
        self.addCleanup(self._pool_submit.stop)
        self._guards = [
            mock.patch.object(server, "_resolve_default_provider_id", return_value="prov-test"),
            mock.patch.object(server, "get_app_config", return_value={}),
            mock.patch.object(server, "get_models_by_provider",
                              return_value=[{"model_name": "test-model"}]),
            mock.patch.object(server, "_guard_concurrency"),
        ]
        for g in self._guards:
            g.start()
            self.addCleanup(g.stop)

    def _capture_submit(self, fn, task_id, *args, **kwargs):
        self.captured = (task_id, kwargs)
        return Future()

    def _run(self, **kwargs):
        self.captured = None
        resp = json.loads(server.generate_note(
            video_url="https://www.bilibili.com/video/BV1xx411c7mD",
            platform="bilibili",
            **kwargs,
        ))
        tid, params = self.captured
        with server._tasks_lock:
            server._task_futures.pop(tid, None)
            server._task_events.pop(tid, None)
        shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)
        self.assertEqual(resp["status"], "PENDING")
        return params

    def test_screenshot_true_appends_to_format(self):
        params = self._run(screenshot=True)
        self.assertEqual(params["_format"], ["screenshot"])
        self.assertIs(params["screenshot"], True)

    def test_link_true_appends_to_format(self):
        params = self._run(link=True, format=["toc"])
        self.assertEqual(params["_format"], ["toc", "link"])

    def test_dedup_when_format_already_has_item(self):
        params = self._run(screenshot=True, link=True, format=["screenshot", "toc"])
        self.assertEqual(params["_format"], ["screenshot", "toc", "link"])

    def test_false_booleans_leave_format_unchanged(self):
        params = self._run(screenshot=False, link=False, format=["toc"])
        self.assertEqual(params["_format"], ["toc"])

    def test_no_format_and_no_booleans_empty_list(self):
        params = self._run()
        self.assertEqual(params["_format"], [])
        self.assertIs(params["screenshot"], False)


class StripMediaMarkersTest(unittest.TestCase):
    """#122 A5 的清洗函数：三种写法（星号/方括号/裸时间）都能剥掉。"""

    def _strip(self, md):
        from app.utils.note_helper import strip_media_markers

        return strip_media_markers(md)

    def test_bracketed_starred_content(self):
        self.assertEqual(self._strip("*Content-[04:16]* 后文"), " 后文")

    def test_bare_content(self):
        self.assertEqual(self._strip("标题 Content-04:16 结尾"), "标题  结尾")

    def test_bracketed_starred_screenshot(self):
        self.assertEqual(self._strip("*Screenshot-[01:23]*"), "")

    def test_bare_screenshot(self):
        self.assertEqual(self._strip("Screenshot-00:05"), "")

    def test_plain_text_untouched(self):
        md = "# 标题\n正文不含标记"
        self.assertEqual(self._strip(md), md)


class ListTasksForwardTest(unittest.TestCase):
    """list_tasks 把 limit/offset 透传给 DAO，不再全表拉回切片（#124 B14）。"""

    def test_passes_limit_and_offset_to_dao(self):
        with mock.patch("app.db.video_task_dao.list_tasks", return_value=[]) as m_list:
            server.list_tasks(limit=2, offset=3)
        m_list.assert_called_once_with(limit=2, offset=3)

    def test_omitted_args_pass_through_as_defaults(self):
        with mock.patch("app.db.video_task_dao.list_tasks", return_value=[]) as m_list:
            server.list_tasks()
        m_list.assert_called_once_with(limit=None, offset=0)

    def test_negative_offset_clamped(self):
        with mock.patch("app.db.video_task_dao.list_tasks", return_value=[]) as m_list:
            server.list_tasks(offset=-5)
        m_list.assert_called_once_with(limit=None, offset=0)


class InspectVideoSsrfTest(unittest.TestCase):
    """docs/05 第 16 轮 A1（#136 由 validate_url 并入 inspect_video）：
    inspect_video 对私网/元数据 URL 明确拒绝（下载器边界也会拦）。"""

    def test_literal_metadata_ip_rejected(self):
        resp = json.loads(server.inspect_video("http://169.254.169.254/latest/meta-data/"))
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["platform"], "generic")
        self.assertIn("SSRF", resp["error"])

    def test_loopback_rejected(self):
        resp = json.loads(server.inspect_video("http://127.0.0.1:8080/video"))
        self.assertFalse(resp["ok"])
        self.assertIn("SSRF", resp["error"])

    def test_localhost_hostname_rejected_when_resolves_private(self):
        # conftest 默认把 DNS 桩成公网；此用例显式让该主机判为非公网
        with mock.patch("app.utils.url_safety._host_is_public", return_value=False):
            resp = json.loads(server.inspect_video("http://localhost:8080/video"))
        self.assertFalse(resp["ok"])
        self.assertIn("SSRF", resp["error"])

    def test_public_platform_url_accepted(self):
        # conftest DNS → 公网；bilibili 命中内置平台——inspect 会调 view API 确认
        # 分 P/标题（网络），mock 掉；platform 识别在请求前完成
        with mock.patch(
            "app.services.inspect._inspect_bilibili",
            return_value={"ok": True, "platform": "bilibili", "kind": "single",
                          "title": "t", "video_id": "BV1vc411b7Wa", "total": 1,
                          "truncated": False,
                          "entries": [{"p": 1, "title": "t", "duration": None,
                                       "url": "https://www.bilibili.com/video/BV1vc411b7Wa",
                                       "video_id": "BV1vc411b7Wa"}]},
        ):
            resp = json.loads(server.inspect_video("https://www.bilibili.com/video/BV1vc411b7Wa"))
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["platform"], "bilibili")
        self.assertEqual(resp["kind"], "single")

    def test_local_existing_file_accepted(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            path = f.name
        try:
            resp = json.loads(server.inspect_video(f"file://{path}"))
        finally:
            Path(path).unlink(missing_ok=True)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["platform"], "local")
        self.assertEqual(resp["kind"], "single")

class GetConfigTest(unittest.TestCase):
    """#135：配置只读合并工具 get_config（合并 read_app_config / get_transcriber_config / test_provider）。

    不传 provider_id → 只读汇总（app_config 过滤敏感项 + providers 掩码 + transcriber 状态 +
    cookie 平台名）；传 provider_id → 附加连通性探测（用已存 key，不接受 key 参数）。
    写配置一律走 CLI（MCP 面无写配置工具，凭证红线最干净）。
    """

    def _patch_reads(self):
        return [
            mock.patch.object(
                server, "get_app_config",
                return_value={"default_style": "detailed", "default_provider_id": "p1", "hf_token": "secret"},
            ),
            mock.patch.object(
                server.ProviderService, "get_all_providers_safe",
                return_value=[{"id": "p1", "name": "测试源", "api_key": "sk-***"}],
            ),
            mock.patch.object(
                server.TranscriberConfigManager, "get_config",
                return_value={"transcriber_type": "fast-whisper"},
            ),
            mock.patch.object(
                server.TranscriberConfigManager, "is_model_ready",
                return_value={"ready": True, "downloading": False, "reason": ""},
            ),
            mock.patch.object(
                server.CookieConfigManager, "list_all",
                return_value={"bilibili": "SESSDATA=secret-value"},
            ),
        ]

    def test_summary_shape_and_sensitive_filter(self):
        with ExitStack() as st:
            for p in self._patch_reads():
                st.enter_context(p)
            resp = json.loads(server.get_config())
        self.assertEqual(resp["app_config"]["default_style"], "detailed")
        self.assertEqual(resp["app_config"]["default_provider_id"], "p1")
        self.assertNotIn("hf_token", resp["app_config"])      # 敏感项过滤
        self.assertEqual(resp["providers"][0]["api_key"], "sk-***")  # key 掩码
        self.assertTrue(resp["transcriber"]["ready"])
        self.assertEqual(resp["cookie_configured"], ["bilibili"])   # 只给平台名
        self.assertNotIn("probe", resp)
        # cookie 值绝不出现在返回里（凭证红线）
        self.assertNotIn("SESSDATA", json.dumps(resp))

    def test_probe_no_provider_id_skips_network(self):
        # 不传 provider_id 时绝不触发探测（probe_models 是唯一网络出口）
        with ExitStack() as st:
            for p in self._patch_reads():
                st.enter_context(p)
            with mock.patch.object(server, "probe_models") as m_probe:
                server.get_config()
        m_probe.assert_not_called()

    def test_probe_ok_reports_models(self):
        with ExitStack() as st:
            for p in self._patch_reads():
                st.enter_context(p)
            st.enter_context(mock.patch.object(
                server.ProviderService, "get_provider_by_id",
                return_value={"id": "p1", "name": "x", "api_key": "sk-***", "base_url": "https://x"},
            ))
            st.enter_context(mock.patch.object(
                server, "probe_models",
                return_value={"ok": True, "models": ["m1", "m2"], "error": None},
            ))
            resp = json.loads(server.get_config("p1"))
        self.assertTrue(resp["probe"]["ok"])
        self.assertEqual(resp["probe"]["models"], ["m1", "m2"])

    def test_probe_fail_reports_error(self):
        with ExitStack() as st:
            for p in self._patch_reads():
                st.enter_context(p)
            st.enter_context(mock.patch.object(
                server.ProviderService, "get_provider_by_id",
                return_value={"id": "p1", "name": "x", "api_key": "k", "base_url": "https://x"},
            ))
            st.enter_context(mock.patch.object(
                server, "probe_models",
                return_value={"ok": False, "models": [], "error": "401 Unauthorized"},
            ))
            resp = json.loads(server.get_config("p1"))
        self.assertFalse(resp["probe"]["ok"])
        self.assertEqual(resp["probe"]["error"], "401 Unauthorized")

    def test_probe_unknown_provider_raises(self):
        with ExitStack() as st:
            for p in self._patch_reads():
                st.enter_context(p)
            st.enter_context(mock.patch.object(
                server.ProviderService, "get_provider_by_id", return_value=None,
            ))
            with self.assertRaises(ValueError) as cm:
                server.get_config("nosuch")
        self.assertIn("供应商不存在", str(cm.exception))


class SsrfEntryGuardTest(unittest.TestCase):
    """#133 A1：显式 platform 的入口级 SSRF 守卫。

    #132 A1 只在 generic/youtube 下载器内部校验——显式传 platform=bilibili/
    kuaishou/douyin 时下载器/短链解析直接对 URL 发出站请求，可打内网/云元数据。
    generate_note / prepare_note_material / inspect_video 入口统一拦截；
    本地路径（local 分支）不受影响。
    """

    def test_generate_note_blocks_private_ip_with_explicit_platform(self):
        with self.assertRaises(ValueError) as cm:
            server.generate_note("http://169.254.169.254/latest/meta-data/", platform="bilibili")
        self.assertIn("SSRF", str(cm.exception))

    def test_generate_note_blocks_before_provider_check(self):
        # 全新环境（无 provider）：URL 错误应先于 provider 错误（H6）
        with self.assertRaises(ValueError) as cm:
            server.generate_note("http://169.254.169.254/", platform="bilibili")
        self.assertIn("SSRF", str(cm.exception))
        self.assertNotIn("provider", str(cm.exception))

    def test_prepare_note_material_blocks_private_ip(self):
        with self.assertRaises(ValueError) as cm:
            server.prepare_note_material("http://10.0.0.1/x.mp4", platform="kuaishou")
        self.assertIn("SSRF", str(cm.exception))

    def test_inspect_blocks_private_ip(self):
        resp = json.loads(server.inspect_video("http://169.254.169.254/", platform="bilibili"))
        self.assertFalse(resp["ok"])
        self.assertIn("SSRF", resp["error"])

    def test_public_url_not_blocked(self):
        # conftest 把域名解析桩成公网（8.8.8.8）：合法 URL 不被误拦，
        # 应继续走到 provider 解析（报「需要 provider_id」而非 SSRF）
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", platform="generic")
        self.assertNotIn("SSRF", str(cm.exception))

    def test_local_path_not_blocked(self):
        # 本地路径在 local 分支分流，不触 SSRF 守卫（缺 provider 也轮不到它）
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "clip.mp4"
            f.write_bytes(b"x")
            with self.assertRaises(ValueError) as cm:
                server.generate_note(str(f))
        self.assertIn("provider_id", str(cm.exception))


class ExplicitProviderKeyCheckTest(unittest.TestCase):
    """#133 B1：显式 provider_id 校验 key 已填（#52 只修了默认解析分支）。

    空 key 的内置行（openai/groq seed）显式传入曾放行，下载+转写全跑完后
    才在 SUMMARIZING 报「API Key 未配置」，浪费整轮流水线。
    """

    def test_explicit_provider_with_masked_key_rejected(self):
        with mock.patch(
            "videonote_mcp.server.ProviderService.get_provider_by_id",
            return_value={"id": "p1", "api_key": "****"},
        ):
            with self.assertRaises(ValueError) as cm:
                server.generate_note("https://example.com/v", platform="generic", provider_id="p1")
        self.assertIn("key 为空", str(cm.exception))

    def test_explicit_provider_with_empty_key_rejected(self):
        with mock.patch(
            "videonote_mcp.server.ProviderService.get_provider_by_id",
            return_value={"id": "p1", "api_key": ""},
        ):
            with self.assertRaises(ValueError) as cm:
                server.generate_note("https://example.com/v", platform="generic", provider_id="p1")
        self.assertIn("key 为空", str(cm.exception))

    def test_explicit_provider_not_found_rejected(self):
        with mock.patch(
            "videonote_mcp.server.ProviderService.get_provider_by_id", return_value=None
        ):
            with self.assertRaises(ValueError) as cm:
                server.generate_note("https://example.com/v", platform="generic", provider_id="ghost")
        self.assertIn("供应商不存在", str(cm.exception))

    def test_explicit_provider_with_key_passes(self):
        done = Future()
        done.set_result(None)
        with mock.patch(
            "videonote_mcp.server.ProviderService.get_provider_by_id",
            return_value={"id": "p1", "api_key": "sk-test"},
        ), mock.patch(
            "videonote_mcp.server.get_models_by_provider", return_value=[{"model_name": "t-model"}]
        ), mock.patch("videonote_mcp.server._pool.submit", return_value=done):
            resp = server.generate_note("https://example.com/v", platform="generic", provider_id="p1")
        self.assertIn('"status": "PENDING"', resp)


class InspectLocalFileUriTest(unittest.TestCase):
    """#133 B2：inspect_video/preflight/batch 的 local 分支认 file://。

    曾是全工具面唯一不认 file:// 的本地入口（#105/#107 输入规整的漏网点）——
    同一文件 validate_url/generate_note 可用、inspect 却报「本地文件不存在」。
    entries[].url 应透传规整后的纯路径（generate_note 才能直接消费）。
    """

    def test_inspect_accepts_file_uri_with_space(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "我的 视频.mp4"
            f.write_bytes(b"x")
            out = json.loads(server.inspect_video(f.as_uri()))  # %20/非 ASCII 编码
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["platform"], "local")
        self.assertEqual(out["entries"][0]["url"], str(f))

    def test_inspect_missing_file_uri_reports_not_found(self):
        out = json.loads(server.inspect_video("file:///tmp/videonote_never_exists_133.mp4"))
        self.assertFalse(out["ok"])
        self.assertIn("本地文件不存在", out["error"])


class GenerateNoteQualityOrderTest(unittest.TestCase):
    """#133 B7：quality 校验在 provider 解析前（H6：参数错误先报）。"""

    def test_quality_error_before_provider_error(self):
        # 全新环境（无 provider）：quality 拼错应报 quality 而不是「需要 provider_id」
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", platform="generic", quality="bogus")
        self.assertIn("quality 必须为", str(cm.exception))
        self.assertNotIn("provider", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
