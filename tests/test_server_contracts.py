"""Wave A 契约：file://、未知 task、步骤 SUCCESS、默认 provider、密钥拒绝。"""
import json
import shutil
import sys
import tempfile
import unittest
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


class ConcurrencyGuardTest(unittest.TestCase):
    def test_guard_raises_when_full(self):
        fake = mock.Mock()
        fake.done.return_value = False
        old = dict(server._task_futures)
        try:
            with server._tasks_lock:
                server._task_futures.clear()
                for i in range(server._MAX_WORKERS):
                    server._task_futures[f"busy{i}"] = fake
            with self.assertRaises(ValueError) as ctx:
                server._guard_concurrency()
            self.assertIn("进行中任务", str(ctx.exception))
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


if __name__ == "__main__":
    unittest.main()
