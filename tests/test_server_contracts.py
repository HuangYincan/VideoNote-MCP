"""Wave A 契约：file://、未知 task、步骤 SUCCESS、默认 provider、密钥拒绝。"""
import json
import shutil
import sys
import tempfile
import unittest
import uuid
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

    def test_string_format_rejected_in_summarize_note(self):
        with self.assertRaises(ValueError) as cm:
            server.summarize_note(
                {"language": "zh", "full_text": "x", "segments": []}, format="toc"
            )
        self.assertIn("format 必须是字符串列表", str(cm.exception))


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

    def test_extract_frames_uses_shared_helper(self):
        # extract_frames 换用共享校验后行为不变：非法值仍在入口被拒（文件存在检查在前）
        tmp = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        tmp.close()
        try:
            with self.assertRaises(ValueError) as cm:
                server.extract_frames(tmp.name, grid_size=[1, 2, 3])
            self.assertIn("grid_size 必须是两个正整数", str(cm.exception))
        finally:
            Path(tmp.name).unlink(missing_ok=True)


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

    def test_zero_limit_clamped_to_one(self):
        tasks = json.loads(server.list_tasks(limit=0))
        self.assertEqual(len(tasks), 1)


class SetTranscriberValidationTest(unittest.TestCase):
    """set_transcriber 未知引擎：持久化后运行时 get_transcriber 静默回退 fast-whisper——
    用户以为配了云端引擎实际跑本地；入口显式报错（#103）。"""

    def test_unknown_type_rejected(self):
        with self.assertRaises(ValueError) as cm:
            server.set_transcriber("bogus-engine")
        self.assertIn("transcriber_type 必须是", str(cm.exception))
        self.assertIn("fast-whisper", str(cm.exception))

    def test_valid_types_pass(self):
        for t in ("fast-whisper", "groq", "bcut", "kuaishou", "mlx-whisper", "funasr"):
            with mock.patch.object(
                server.TranscriberConfigManager,
                "update_config",
                return_value={"transcriber_type": t, "whisper_model_size": "small"},
            ) as m:
                resp = json.loads(server.set_transcriber(t))
            m.assert_called_once()
            self.assertEqual(resp["transcriber_type"], t)

    def test_whitelist_matches_enum(self):
        # 白名单与 TranscriberType 枚举同源——防止两处漂移
        from app.transcriber.transcriber_provider import TranscriberType

        self.assertEqual(set(server._TRANSCRIBER_TYPES), {e.value for e in TranscriberType})


class SetTranscriberSizeValidationTest(unittest.TestCase):
    """set_transcriber 的 whisper_model_size 同样入口校验（#103 只校了引擎）：
    非法尺寸被持久化后，任务跑到 TRANSCRIBING 才因模型加载失败炸（或 preflight
    报「未下载」）——与运行时同源（whisper_models 注册表），#108。"""

    def test_bogus_size_rejected(self):
        with self.assertRaises(ValueError) as cm:
            server.set_transcriber("fast-whisper", whisper_model_size="bogus-size")
        self.assertIn("未知 whisper 模型尺寸", str(cm.exception))
        self.assertIn("large-v3", str(cm.exception))

    def test_bogus_size_rejected_even_for_cloud_engine(self):
        # 云端引擎忽略尺寸，但尺寸仍会被持久化——拼错就该现在报，而不是切回本地时炸
        with self.assertRaises(ValueError) as cm:
            server.set_transcriber("groq", whisper_model_size="bogus-size")
        self.assertIn("未知 whisper 模型尺寸", str(cm.exception))

    def test_builtin_size_passes(self):
        with mock.patch.object(
            server.TranscriberConfigManager,
            "update_config",
            return_value={"transcriber_type": "fast-whisper", "whisper_model_size": "small"},
        ) as m:
            resp = json.loads(server.set_transcriber("fast-whisper", whisper_model_size="small"))
        m.assert_called_once()
        self.assertEqual(resp["whisper_model_size"], "small")

    def test_repo_id_passthrough_accepted(self):
        # 含 "/" 的 HF repo_id 是合法运行时输入（resolve 直通），不得被白名单误伤
        with mock.patch.object(
            server.TranscriberConfigManager,
            "update_config",
            return_value={"transcriber_type": "fast-whisper", "whisper_model_size": "Systran/faster-whisper-small"},
        ) as m:
            server.set_transcriber("fast-whisper", whisper_model_size="Systran/faster-whisper-small")
        m.assert_called_once()

    def test_local_dir_passthrough_accepted(self):
        # 已存在的本地目录同样是合法运行时输入
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(
                server.TranscriberConfigManager,
                "update_config",
                return_value={"transcriber_type": "fast-whisper", "whisper_model_size": td},
            ) as m:
                server.set_transcriber("fast-whisper", whisper_model_size=td)
            m.assert_called_once()


class SummarizeTranscriptShapeTest(unittest.TestCase):
    """summarize_note 的 transcript 形状：缺 segments/full_text 时曾静默拿空素材让 LLM
    凭空生成笔记（还烧配额）；入口显式报错（#104）。"""

    @staticmethod
    def _submit(transcript):
        """stub provider/模型/线程池，让 summarize_note 干净走到提交点。"""
        done = Future()
        done.set_result(None)
        with mock.patch(
            "videonote_mcp.server._resolve_default_provider_id", return_value="t-provider"
        ), mock.patch(
            "videonote_mcp.server.get_models_by_provider", return_value=[{"model_name": "t-model"}]
        ), mock.patch("videonote_mcp.server._pool.submit", return_value=done):
            return server.summarize_note(transcript)

    def test_empty_dict_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._submit({})
        self.assertIn("transcript 缺少内容字段", str(cm.exception))

    def test_missing_content_fields_rejected(self):
        # 传 fetch 结果外层（{"ok": ...}）是常见传错——必须报错而不是拿空素材总结
        with self.assertRaises(ValueError) as cm:
            self._submit({"ok": True, "language": "zh"})
        self.assertIn("segments 或 full_text", str(cm.exception))

    def test_garbage_json_string_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._submit('{"not": "a transcript"}')
        self.assertIn("transcript 缺少内容字段", str(cm.exception))

    def test_non_dict_rejected(self):
        with self.assertRaises(ValueError) as cm:
            self._submit(42)
        self.assertIn("transcript 缺少内容字段", str(cm.exception))

    def test_empty_but_shaped_transcript_accepted(self):
        # 静音视频的合法转写（segments 为空但字段在）不拦截——是否总结是用户的决定
        resp = self._submit({"language": "zh", "segments": [], "full_text": ""})
        self.assertIn('"status": "PENDING"', resp)

    def test_full_text_only_accepted(self):
        resp = self._submit({"full_text": "hello"})
        self.assertIn('"status": "PENDING"', resp)

    def test_segments_only_accepted(self):
        resp = self._submit({"segments": [{"start": 0, "end": 1, "text": "hi"}]})
        self.assertIn('"status": "PENDING"', resp)


class ExtractFramesIntervalWarningTest(unittest.TestCase):
    """video_interval 非数值静默回退 6——与 num_speakers 同口径（#101），打 warning 后回退（#104）。"""

    def test_non_numeric_interval_warns_and_falls_back(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "clip.mp4"
            f.write_bytes(b"x")
            with mock.patch.object(server, "_submit_step_task", return_value="f1") as m:
                with self.assertLogs("videonote_mcp.server", level="WARNING") as logs:
                    server.extract_frames(str(f), video_interval="abc")
            kwargs = m.call_args.kwargs
            self.assertEqual(kwargs["video_interval"], 6)
            self.assertTrue(any("video_interval" in msg for msg in logs.output))

    def test_numeric_string_still_accepted(self):
        # 数字字符串是合法输入（int() 可转），不打扰
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "clip.mp4"
            f.write_bytes(b"x")
            with mock.patch.object(server, "_submit_step_task", return_value="f1") as m, mock.patch.object(
                server.logger, "warning"
            ) as w:
                server.extract_frames(str(f), video_interval="10")
            self.assertEqual(m.call_args.kwargs["video_interval"], 10)
            self.assertFalse(any("video_interval" in str(c) for c in w.call_args_list))


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


class FetchSubtitlesPlatformTest(unittest.TestCase):
    """fetch_subtitles 的 platform 参数：拼错平台曾被 pipeline 吞掉异常转成「该视频
    没有可用平台字幕」——误报成视频没字幕；入口白名单显式报错（#105）。"""

    def test_bogus_platform_reported_explicitly(self):
        resp = json.loads(server.fetch_subtitles("https://example.com/v", platform="bilibil"))
        self.assertFalse(resp["ok"])
        self.assertIn("platform 只支持", resp["error"])
        self.assertIn("bilibili", resp["error"])

    def test_empty_platform_reported_explicitly(self):
        # 空串曾因 falsy 静默走自动检测（用户的拼写错误无声消失）
        resp = json.loads(server.fetch_subtitles("https://example.com/v", platform=""))
        self.assertFalse(resp["ok"])
        self.assertIn("platform 只支持", resp["error"])

    def test_valid_platform_passes_to_pipeline(self):
        with mock.patch(
            "videonote_mcp.server.pipeline.fetch_subtitles", return_value=None
        ) as m:
            resp = json.loads(server.fetch_subtitles("https://example.com/v", platform="bilibili"))
        m.assert_called_once_with("https://example.com/v", "bilibili")
        self.assertNotIn("platform 只支持", resp.get("error", ""))

    def test_none_platform_auto_detect(self):
        with mock.patch(
            "videonote_mcp.server.pipeline.fetch_subtitles",
            return_value={"language": "zh", "segments": [], "full_text": ""},
        ) as m:
            resp = json.loads(server.fetch_subtitles("https://example.com/v"))
        m.assert_called_once_with("https://example.com/v", None)
        self.assertTrue(resp["ok"])

    def test_whitelist_matches_factory(self):
        from app.services.constant import SUPPORT_PLATFORM_MAP

        self.assertEqual(set(server._KNOWN_PLATFORMS), set(SUPPORT_PLATFORM_MAP))


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
    或在工具入口炸 ValueError——打 warning 后回退，与 #104 缺省链口径一致（#107）。"""

    def test_tool_junk_config_falls_back_to_env(self):
        tid = ExportFormatsWhitelistTest._task_with_transcript()
        try:
            with mock.patch.object(
                server, "get_app_config", return_value={"default_export_formats": "srt,vtt"}
            ), mock.patch.dict(
                "os.environ", {"VIDEONOTE_DEFAULT_EXPORT_FORMATS": '["vtt"]'}, clear=False
            ), mock.patch.object(server.logger, "warning") as w:
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
                server, "get_app_config", return_value={"default_export_formats": {"a": "b"}}
            ), mock.patch.dict("os.environ", {"VIDEONOTE_DEFAULT_EXPORT_FORMATS": ""}, clear=False):
                resp = json.loads(server.export_transcript(tid))
            self.assertTrue(resp["ok"])
            self.assertIn("srt", resp["formats"])
        finally:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / tid, ignore_errors=True)

    def test_auto_export_junk_config_falls_back_to_env(self):
        with mock.patch.object(
            server, "get_app_config", return_value={"default_export_formats": "srt"}
        ), mock.patch.dict(
            "os.environ", {"VIDEONOTE_DEFAULT_EXPORT_FORMATS": '["vtt"]'}, clear=False
        ), mock.patch("videonote_mcp.export.export_transcript") as m, mock.patch.object(
            server.logger, "warning"
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
        """env 用值或空串显式覆盖，防本机环境残留（env_or 空串→None→回退默认）。"""
        with mock.patch.object(server, "get_app_config", return_value=cfg), mock.patch.dict(
            "os.environ", {"VIDEONOTE_VIDEO_INTERVAL": env_value}, clear=False
        ), mock.patch.object(server.logger, "warning") as w:
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
            server, "get_app_config", return_value={"video_interval": 0}
        ), mock.patch.dict(
            "os.environ", {"VIDEONOTE_VIDEO_INTERVAL": "6"}, clear=False
        ), mock.patch(
            "videonote_mcp.server._resolve_default_provider_id", return_value="t-provider"
        ), mock.patch(
            "videonote_mcp.server.get_models_by_provider", return_value=[{"model_name": "t-model"}]
        ), mock.patch("videonote_mcp.server._pool.submit", return_value=done) as m:
            server.generate_note("https://example.com/v")
        self.assertEqual(m.call_args.kwargs["video_interval"], 0)


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
            self.assertNotIn("ok", resp)  # 正常全局清理形状无 ok 字段
            m.assert_called_once_with(include_config=False, include_models=False)
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


if __name__ == "__main__":
    unittest.main()
