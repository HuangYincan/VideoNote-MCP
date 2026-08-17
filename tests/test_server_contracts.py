"""Wave A 契约：file://、未知 task、步骤 SUCCESS、默认 provider、密钥拒绝。"""
import json
import shutil
import sys
import tempfile
import unittest
from concurrent.futures import Future
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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

    def test_wait_unknown_returns_immediately(self):
        resp = json.loads(server.wait_for_note("deadbeef0002", timeout=30, poll_interval=5))
        self.assertEqual(resp["status"], "NOT_FOUND")


class StepTaskSuccessTest(unittest.TestCase):
    def tearDown(self):
        shutil.rmtree(server.NOTE_OUTPUT_DIR / "stepok000001", ignore_errors=True)

    def test_run_step_task_writes_success(self):
        def step(task_id, cancel_event):
            return {"kind": "transcript", "transcript": {"full_text": "hi", "segments": []}}

        server._run_step_task("stepok000001", None, step_fn=step)
        resp = json.loads(server.get_task_status("stepok000001"))
        self.assertEqual(resp["status"], "SUCCESS")
        self.assertEqual(resp["result"]["kind"], "transcript")

    def test_step_task_keeps_transcript_by_default(self):
        # docs 审计 G3：transcribe_media 的转写是主产物，get_task_status 默认不剥
        def step(task_id, cancel_event):
            return {"kind": "transcript", "transcript": {"full_text": "hi", "segments": []}}

        server._run_step_task("stepok000002", None, step_fn=step)
        resp = json.loads(server.get_task_status("stepok000002"))
        self.assertEqual(resp["result"]["transcript"]["full_text"], "hi")

    def test_note_result_strips_transcript_by_default(self):
        # 笔记任务（无 kind）默认剥掉转写，include_transcript=True 才保留
        tid = "notestr000001"
        try:
            server._atomic_write_json(
                server.NOTE_OUTPUT_DIR / tid / "result.json",
                {"markdown": "# 笔记", "transcript": {"full_text": "secret", "segments": []}},
            )
            server._write_status(tid, "SUCCESS", message="完成")
            resp = json.loads(server.get_task_status(tid))
            self.assertNotIn("transcript", resp["result"])
            resp2 = json.loads(server.get_task_status(tid, include_transcript=True))
            self.assertEqual(resp2["result"]["transcript"]["full_text"], "secret")
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def test_index_step_task_visible_in_list(self):
        tid = "stepidx000001"
        try:
            server._index_step_task(tid, "transcript", title="clip.wav")
            rows = json.loads(server.list_tasks())
            ids = {r.get("task_id") for r in rows}
            self.assertIn(tid, ids)
        finally:
            from app.db.video_task_dao import delete_task

            try:
                delete_task(tid)
            except Exception:
                pass
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)


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
    def test_add_provider_rejects_api_key(self):
        with self.assertRaises(ValueError) as ctx:
            server.add_provider("x", api_key="sk-secret", base_url="https://api.example", type="custom")
        self.assertIn("不能经 MCP", str(ctx.exception))

    def test_update_provider_rejects_api_key(self):
        with self.assertRaises(ValueError) as ctx:
            server.update_provider("openai", api_key="sk-secret")
        self.assertIn("不能经 MCP", str(ctx.exception))

    def test_set_downloader_cookie_rejects_cookie(self):
        with self.assertRaises(ValueError) as ctx:
            server.set_downloader_cookie("bilibili", cookie="SESSDATA=abc")
        self.assertIn("不能经 MCP", str(ctx.exception))

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


class ListModelsShapeTest(unittest.TestCase):
    def test_live_models_normalized(self):
        provider = {"id": "p1", "api_key": "sk", "base_url": "https://x", "name": "x"}
        with mock.patch.object(server.ProviderService, "get_provider_by_id", return_value=provider):
            with mock.patch.object(server, "_fetch_live_models", return_value=["b-model", "a-model"]):
                data = json.loads(server.list_models("p1"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["source"], "provider_api")
        self.assertEqual(data["models"], [{"id": "a-model", "name": "a-model"}, {"id": "b-model", "name": "b-model"}])

    def test_db_fallback_normalized(self):
        provider = {"id": "p1", "api_key": "sk", "base_url": "https://x", "name": "x"}
        with mock.patch.object(server.ProviderService, "get_provider_by_id", return_value=provider):
            with mock.patch.object(server, "_fetch_live_models", return_value=None):
                with mock.patch.object(
                    server, "get_models_by_provider", return_value=[{"model_name": "local-1"}]
                ):
                    data = json.loads(server.list_models("p1"))
        self.assertTrue(data["ok"])
        self.assertEqual(data["source"], "database")
        self.assertEqual(data["models"], [{"id": "local-1", "name": "local-1"}])


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


class ExtractFramesValidationTest(unittest.TestCase):
    """docs 审计（F 组后续）：extract_frames 参数校验——非法 interval/grid 不应透传。"""

    def test_invalid_grid_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "clip.mp4"
            f.write_bytes(b"x")
            with self.assertRaises(ValueError):
                server.extract_frames(str(f), grid_size=[0, 3])
            with self.assertRaises(ValueError):
                server.extract_frames(str(f), grid_size=[3])
            with self.assertRaises(ValueError):
                server.extract_frames(str(f), grid_size=[3, "a"])

    def test_zero_interval_clamped_to_one(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "clip.mp4"
            f.write_bytes(b"x")
            with mock.patch.object(server, "_submit_step_task", return_value="f1") as m:
                server.extract_frames(str(f), video_interval=0)
            kwargs = m.call_args.kwargs
            self.assertEqual(kwargs["video_interval"], 1)


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

    def test_bogus_style_rejected_in_summarize_note(self):
        with self.assertRaises(ValueError) as cm:
            server.summarize_note(
                {"language": "zh", "full_text": "x", "segments": []}, style="nope"
            )
        self.assertIn("style 必须是", str(cm.exception))

    def test_bogus_format_rejected_in_summarize_note(self):
        with self.assertRaises(ValueError) as cm:
            server.summarize_note(
                {"language": "zh", "full_text": "x", "segments": []}, format=["toc", "bad"]
            )
        self.assertIn("format 只支持", str(cm.exception))

    def test_none_style_skips_validation(self):
        # 默认路径（None → setup 配置）不被白名单拦截，继续走 provider 解析
        with self.assertRaises(ValueError) as cm:
            server.generate_note("https://example.com/v", style=None)
        self.assertNotIn("style 必须是", str(cm.exception))


class FetchCommentsLimitTest(unittest.TestCase):
    """fetch_comments 的 limit<=0 会令 fetcher 的 `len(seen) >= limit` 恒真——静默空结果，钳制到 ≥1。"""

    def test_limit_clamped_to_one(self):
        with mock.patch(
            "app.downloaders.bilibili_comment.BilibiliCommentFetcher.fetch_comments",
            return_value={"ok": True, "comments": []},
        ) as m:
            server.fetch_comments("https://www.bilibili.com/video/BV1xx411c7mD", limit=0)
        self.assertEqual(m.call_args.kwargs.get("limit"), 1)

    def test_negative_limit_clamped(self):
        with mock.patch(
            "app.downloaders.bilibili_comment.BilibiliCommentFetcher.fetch_comments",
            return_value={"ok": True, "comments": []},
        ) as m:
            server.fetch_comments("https://www.bilibili.com/video/BV1xx411c7mD", limit=-5)
        self.assertEqual(m.call_args.kwargs.get("limit"), 1)

    def test_valid_limit_passes_through(self):
        with mock.patch(
            "app.downloaders.bilibili_comment.BilibiliCommentFetcher.fetch_comments",
            return_value={"ok": True, "comments": []},
        ) as m:
            server.fetch_comments("https://www.bilibili.com/video/BV1xx411c7mD", limit=10)
        self.assertEqual(m.call_args.kwargs.get("limit"), 10)


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


class ProviderConfigToolsTest(unittest.TestCase):
    """Phase 2d：delete_provider / delete_model / test_provider / read_app_config。"""

    def test_delete_provider_deletes_and_clears_default(self):
        with mock.patch.object(
            server.ProviderService, "get_provider_by_id", return_value={"id": "p1", "name": "测试源"}
        ):
            with mock.patch.object(server.ProviderService, "delete_provider") as m_del:
                with mock.patch.object(server, "remove_app_config") as m_rm:
                    resp = json.loads(server.delete_provider("p1"))
        self.assertTrue(resp["deleted"])
        self.assertEqual(resp["id"], "p1")
        m_del.assert_called_once_with("p1")
        m_rm.assert_called_once_with("default_model:p1")

    def test_delete_provider_missing_raises(self):
        with mock.patch.object(server.ProviderService, "get_provider_by_id", return_value=None):
            with self.assertRaises(ValueError):
                server.delete_provider("nosuch")

    def test_delete_model_resolves_and_deletes(self):
        with mock.patch.object(
            server, "get_models_by_provider", return_value=[{"id": 7, "model_name": "local-1"}]
        ):
            with mock.patch.object(server, "_dao_delete_model") as m_del:
                with mock.patch.object(server, "remove_app_config"):
                    resp = json.loads(server.delete_model("p1", "local-1"))
        self.assertTrue(resp["deleted"])
        m_del.assert_called_once_with(7)

    def test_delete_model_missing_raises(self):
        with mock.patch.object(server, "get_models_by_provider", return_value=[]):
            with self.assertRaises(ValueError):
                server.delete_model("p1", "nope")

    def test_test_provider_ok_and_fail(self):
        provider = {"id": "p1", "api_key": "sk-123", "base_url": "https://api.x", "name": "x"}
        with mock.patch.object(server.ProviderService, "get_provider_by_id", return_value=provider):
            with mock.patch.object(
                server, "probe_models", return_value={"ok": True, "models": ["b", "a", "a"]}
            ):
                ok = json.loads(server.test_provider("p1"))
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["count"], 3)
        self.assertEqual(ok["models"], ["a", "b"])

        with mock.patch.object(server.ProviderService, "get_provider_by_id", return_value=provider):
            with mock.patch.object(
                server, "probe_models", return_value={"ok": False, "error": "401"}
            ):
                bad = json.loads(server.test_provider("p1"))
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error"], "401")

    def test_read_app_config_filters_sensitive_and_reports_default(self):
        cfg = {
            "hf_token": "hf_xxx",
            "notes_dir": "/tmp/notes",
            "default_model:p1": "gpt-4o",
            "default_export_formats": ["md", "pdf"],
        }
        with mock.patch.object(server, "get_app_config", return_value=cfg):
            with mock.patch.object(server, "_resolve_default_provider_id", return_value="p1"):
                data = json.loads(server.read_app_config())
        self.assertNotIn("hf_token", data)
        self.assertIn("notes_dir", data)
        self.assertEqual(data["default_provider_id"], "p1")
        self.assertEqual(data["default_model:p1"], "gpt-4o")
        self.assertEqual(data["default_export_formats"], ["md", "pdf"])


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


if __name__ == "__main__":
    unittest.main()
