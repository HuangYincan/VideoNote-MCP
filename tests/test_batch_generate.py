"""batch_generate_notes 契约（docs/05 #30）：展开→逐条提交→单条失败不阻断。"""
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import videonote_mcp.server as server


def _fake_generate_note(url, **kwargs):
    return json.dumps({"task_id": f"task-{abs(hash(url))}", "status": "PENDING", "platform": "bilibili"})


class BatchGenerateTest(unittest.TestCase):
    def test_multi_expands_and_submits_each(self):
        entries = [
            {"p": 1, "title": "P1", "duration": 100, "url": "https://b23.tv/BV1x", "video_id": "BV1x"},
            {"p": 2, "title": "P2", "duration": 200, "url": "https://b23.tv/BV1x?p=2", "video_id": "BV1x"},
        ]
        with mock.patch("app.services.inspect.inspect_video", return_value={
            "ok": True, "platform": "bilibili", "kind": "multi",
            "title": "合集", "total": 2, "truncated": False, "entries": entries,
        }), mock.patch.object(server, "generate_note", side_effect=_fake_generate_note) as gn:
            out = json.loads(server.batch_generate_notes("https://b23.tv/BV1x"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["submitted"], 2)
        self.assertEqual(len(out["tasks"]), 2)
        self.assertEqual(out["tasks"][0]["p"], 1)
        self.assertIn("task_id", out["tasks"][0])
        self.assertEqual(gn.call_count, 2)

    def test_single_failure_does_not_block_others(self):
        entries = [
            {"p": 1, "title": "P1", "duration": 100, "url": "u1", "video_id": "v1"},
            {"p": 2, "title": "P2", "duration": 200, "url": "u2", "video_id": "v2"},
        ]
        def _flaky(url, **kwargs):
            if url == "u1":
                raise ValueError("供应商无 key")
            return _fake_generate_note(url, **kwargs)
        with mock.patch("app.services.inspect.inspect_video", return_value={
            "ok": True, "platform": "bilibili", "kind": "multi",
            "title": "合集", "total": 2, "truncated": False, "entries": entries,
        }), mock.patch.object(server, "generate_note", side_effect=_flaky):
            out = json.loads(server.batch_generate_notes("u1"))
        self.assertTrue(out["ok"], "至少一条成功")
        self.assertEqual(out["submitted"], 1)
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("供应商无 key", out["errors"][0]["error"])
        self.assertEqual(out["tasks"][0]["url"], "u2")

    def test_single_entry_degenerates_to_one_task(self):
        with mock.patch("app.services.inspect.inspect_video", return_value={
            "ok": True, "platform": "bilibili", "kind": "single",
            "title": "单集", "total": 1, "truncated": False, "entries": [],
        }), mock.patch.object(server, "generate_note", side_effect=_fake_generate_note) as gn:
            out = json.loads(server.batch_generate_notes("https://b23.tv/BV1x"))
        self.assertEqual(out["submitted"], 1)
        self.assertEqual(gn.call_count, 1)

    def test_single_success_shape_matches_multi(self):
        """单集成功分支补齐 truncated/remaining/platform/kind——Agent 按一种形状
        解析，不再因单集分支缺键 KeyError（#124 A11）。"""
        with mock.patch("app.services.inspect.inspect_video", return_value={
            "ok": True, "platform": "bilibili", "kind": "single",
            "title": "单集", "total": 1, "truncated": False, "entries": [],
        }), mock.patch.object(server, "generate_note", side_effect=_fake_generate_note):
            out = json.loads(server.batch_generate_notes("https://b23.tv/BV1x"))
        self.assertTrue(out["ok"])
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["submitted"], 1)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["remaining"], 0)
        self.assertEqual(out["platform"], "bilibili")
        self.assertEqual(out["kind"], "single")

    def test_single_failure_shape_matches_multi(self):
        """单集失败分支同样带 platform/kind/truncated/remaining（inspect 失败分支有、
        单集提交失败分支曾缺，#124 A11）。"""
        def _fail(url, **kwargs):
            raise ValueError("供应商无 key")

        with mock.patch("app.services.inspect.inspect_video", return_value={
            "ok": True, "platform": "bilibili", "kind": "single",
            "title": "单集", "total": 1, "truncated": False, "entries": [],
        }), mock.patch.object(server, "generate_note", side_effect=_fail):
            out = json.loads(server.batch_generate_notes("https://b23.tv/BV1x"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["submitted"], 0)
        self.assertFalse(out["truncated"])
        self.assertEqual(out["remaining"], 0)
        self.assertEqual(out["platform"], "bilibili")
        self.assertEqual(out["kind"], "single")
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("供应商无 key", out["errors"][0]["error"])

    def test_inspect_failure_passes_through(self):
        # C6 归一化：inspect 失败也走统一 errors[] 形状（此前直接透传 {error: ...}，
        # Agent 要猜两种返回结构）
        with mock.patch("app.services.inspect.inspect_video", return_value={
            "ok": False, "platform": "unknown", "kind": "single", "error": "解析失败",
        }):
            out = json.loads(server.batch_generate_notes("https://x.invalid"))
        self.assertFalse(out["ok"])
        self.assertEqual(out["total"], 0)
        self.assertEqual(out["submitted"], 0)
        self.assertEqual(len(out["errors"]), 1)
        self.assertIn("解析失败", out["errors"][0]["error"])
        self.assertEqual(out["errors"][0]["url"], "https://x.invalid")

    def test_passes_through_advanced_params(self):
        """批量应透传 generate_note 的全部高级参数（视频理解/弹幕/link/notes_dir 等）。"""
        entries = [
            {"p": 1, "title": "P1", "duration": 100, "url": "u1", "video_id": "v1"},
        ]
        captured = {}

        def _capture(url, **kwargs):
            captured.update(kwargs)
            return _fake_generate_note(url, **kwargs)

        with mock.patch("app.services.inspect.inspect_video", return_value={
            "ok": True, "platform": "bilibili", "kind": "multi",
            "title": "合集", "total": 1, "truncated": False, "entries": entries,
        }), mock.patch.object(server, "generate_note", side_effect=_capture) as gn:
            server.batch_generate_notes(
                "u1",
                link=True,
                video_understanding=True,
                video_interval=4,
                grid_size=[3, 3],
                include_comments=True,
                comments_limit=30,
                notes_dir="/tmp/notes",
            )
        self.assertEqual(gn.call_count, 1)
        self.assertTrue(captured["link"])
        self.assertTrue(captured["video_understanding"])
        self.assertEqual(captured["video_interval"], 4)
        self.assertEqual(captured["grid_size"], [3, 3])
        self.assertTrue(captured["include_comments"])
        self.assertEqual(captured["comments_limit"], 30)
        self.assertEqual(captured["notes_dir"], "/tmp/notes")

    def test_batch_bypasses_concurrency_guard_when_full(self):
        """满员时批量仍全量提交（#121 C1）：thread-local 旁路只对 batch 生效。

        worker 毫秒级把任务置 running，第 4 条起逐条被门禁拒——批量「排队等待」
        语义靠线程池承担（max_entries ≤ 50 封顶），不做运行中上限拒绝。
        """
        entries = [
            {"p": i, "title": f"P{i}", "duration": 60, "url": f"u{i}", "video_id": f"v{i}"}
            for i in range(1, 11)
        ]
        fake = mock.Mock()
        fake.running.return_value = True
        old = dict(server._task_futures)
        try:
            with server._tasks_lock:
                server._task_futures.clear()
                for i in range(server._MAX_WORKERS):
                    server._task_futures[f"busy{i}"] = fake
            with mock.patch("app.services.inspect.inspect_video", return_value={
                "ok": True, "platform": "bilibili", "kind": "multi",
                "title": "合集", "total": 10, "truncated": False, "entries": entries,
            }), mock.patch.object(server, "generate_note", side_effect=_fake_generate_note) as gn:
                out = json.loads(server.batch_generate_notes("u1"))
            self.assertEqual(out["submitted"], 10)
            self.assertEqual(out["errors"], [])
            self.assertEqual(gn.call_count, 10)
            # 旁路已复位（finally 清 thread-local）：批量结束后直接调用门禁仍拒绝
            with self.assertRaises(ValueError):
                server._guard_concurrency()
        finally:
            with server._tasks_lock:
                server._task_futures.clear()
                server._task_futures.update(old)


if __name__ == "__main__":
    unittest.main()
