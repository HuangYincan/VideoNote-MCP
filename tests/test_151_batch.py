"""#151 MCP 默认由当前 Agent 写笔记：保留独立于 Skills 的运行时契约。"""
from __future__ import annotations

import inspect
import json
import unittest
from unittest import mock

from videonote_mcp import server


class HealthCheckDefaultNeedProviderTest(unittest.TestCase):
    def test_signature_defaults_false(self):
        params = inspect.signature(server.health_check).parameters
        self.assertIs(params["need_provider"].default, False)

    def test_multi_duration_hint_prefers_prepare(self):
        info = {
            "ok": True,
            "kind": "multi",
            "total": 12,
            "entries": [{"duration": 60, "url": "https://example.com/p1"}],
        }
        with mock.patch.object(server.shutil, "which", return_value="/usr/bin/ffmpeg"):
            with mock.patch.object(
                server.shutil, "disk_usage", return_value=mock.Mock(free=10 * 1024**3)
            ):
                with mock.patch.object(
                    server.TranscriberConfigManager,
                    "is_model_ready",
                    return_value={
                        "ready": True,
                        "transcriber_type": "fast-whisper",
                        "model_size": "small",
                        "downloading": False,
                        "reason": "",
                    },
                ):
                    with mock.patch(
                        "app.services.inspect.inspect_video", return_value=info
                    ):
                        data = json.loads(server.health_check(url="https://example.com/list"))
        detail = {c["name"]: c for c in data["checks"]}["duration"]["detail"]
        self.assertIn("prepare_note_material", detail)
        self.assertIn("batch_generate_notes", detail)
        self.assertIn("后备", detail)


class ToolDocstringRoutingTest(unittest.TestCase):
    def test_inspect_video_docstring_default_prepare(self):
        doc = server.inspect_video.__doc__ or ""
        self.assertIn("prepare_note_material", doc)
        self.assertIn("后备", doc)

    def test_generate_note_docstring_is_fallback(self):
        doc = server.generate_note.__doc__ or ""
        self.assertIn("后备", doc)
        self.assertIn("prepare_note_material", doc)

    def test_batch_generate_notes_docstring_is_fallback(self):
        doc = server.batch_generate_notes.__doc__ or ""
        self.assertIn("后备", doc)
        self.assertIn("prepare_note_material", doc)
