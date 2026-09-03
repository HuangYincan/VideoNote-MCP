"""#151 Skill 默认由当前 Agent 写笔记：非多模态才走配置 LLM。

契约：
1. SKILL.md 把 prepare_note_material 当默认、generate_note/batch 当后备；
2. health_check 默认 need_provider=False；合集预解析提示默认 prepare 而非 batch；
3. inspect_video / generate_note / batch_generate_notes docstring 口径与 SKILL 一致。
"""
from __future__ import annotations

import inspect
import json
import unittest
from pathlib import Path
from unittest import mock

from videonote_mcp import server

_REPO = Path(__file__).resolve().parent.parent
_SKILL = (_REPO / "skills" / "videonote" / "SKILL.md").read_text(encoding="utf-8")


class SkillDefaultAgentWriterTest(unittest.TestCase):
    def test_skill_default_path_is_prepare_not_generate(self):
        self.assertIn("prepare_note_material", _SKILL)
        self.assertIn("health_check(need_provider=False)", _SKILL)
        self.assertIn("后备路径", _SKILL)
        # 默认路径禁止把合集交给 batch
        self.assertIn("不要**对默认路径用 `batch_generate_notes`", _SKILL)
        # 判定条款：不能看图才走配置 LLM
        self.assertIn("纯文本模型", _SKILL)
        self.assertIn("后备 LLM", _SKILL)

    def test_skill_does_not_treat_agent_direct_false_as_generate(self):
        self.assertIn("不要**因为该键是 false 就改走 `generate_note`", _SKILL)


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
