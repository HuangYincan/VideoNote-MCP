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

    def test_inspect_failure_passes_through(self):
        with mock.patch("app.services.inspect.inspect_video", return_value={
            "ok": False, "platform": "unknown", "kind": "single", "error": "解析失败",
        }):
            out = json.loads(server.batch_generate_notes("https://x.invalid"))
        self.assertFalse(out["ok"])
        self.assertIn("解析失败", out["error"])

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


if __name__ == "__main__":
    unittest.main()
