"""#138 工具精简 16→10：新增合并工具的契约验证。

覆盖点（合并后新增/变化的入口行为）：
1. task 工具 action 分派：默认 status、非法 action 拒绝、segment_range 仅 transcript 生效；
2. cleanup 参数组合冲突显式报错（task_id + include_config/include_models、全局 + include_note），
   全局 dry_run 预览形状保留；
3. process_media 三分支必填参数校验（export 缺 task_id / merge 缺 files / diarize 缺
   audio_file），hf_token 蜜罐与 action 无关（防切换分支绕过凭证红线）；
4. health_check 统一形状：ok/checks 恒在（db 检查项并入），need_provider 默认 False（#151），
   显式 True 才查供应商。
"""
import json
import sys
import unittest
from pathlib import Path

# 确保能 import videonote_mcp.server（vendored app.* 在其内部 import）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 必须先 import server：其模块顶层 setup_environment() 会把 NOTE_OUTPUT_DIR 等
# 环境变量设为数据目录的绝对路径（pytest 由 conftest.py 隔离到 /tmp/videonote_pytest）
import videonote_mcp.server as server


class TaskActionValidationTest(unittest.TestCase):
    """task 工具 action 分派与非法值拒绝（#138）。"""

    def test_default_action_is_status(self):
        # 默认 action=status：不存在的任务 → NOT_FOUND（与 status 分支契约一致）
        resp = json.loads(server.task("deadbeef0001"))
        self.assertEqual(resp["status"], "NOT_FOUND")

    def test_segment_range_ignored_in_status_action(self):
        # segment_range 仅 transcript 分支生效；status 分支传了也忽略（不报错）
        resp = json.loads(server.task("deadbeef0001", segment_range="0-10"))
        self.assertEqual(resp["status"], "NOT_FOUND")

    def test_unknown_action_rejected(self):
        # schema Literal 已约束 action；直接调用/老客户端传非法值入口显式报错
        with self.assertRaises(ValueError):
            server.task("deadbeef0001", action="bogus")


class CleanupConflictValidationTest(unittest.TestCase):
    """cleanup 参数组合冲突显式报错（#138：静默忽略会让 Agent 误以为已生效）。"""

    def test_single_task_with_include_config_rejected(self):
        with self.assertRaises(ValueError):
            server.cleanup("task-1", include_config=True)

    def test_single_task_with_include_models_rejected(self):
        with self.assertRaises(ValueError):
            server.cleanup("task-1", include_models=True)

    def test_global_with_include_note_rejected(self):
        with self.assertRaises(ValueError):
            server.cleanup(include_note=True)

    def test_global_dry_run_shape_kept(self):
        # 全局 dry_run 预览形状（原 cleanup_all dry_run，#137 契约）保留
        resp = json.loads(server.cleanup(dry_run=True))
        self.assertTrue(resp["dry_run"])
        self.assertIn("would_clean", resp)
        self.assertIn("would_keep", resp)


class ProcessMediaActionValidationTest(unittest.TestCase):
    """process_media 三分支必填参数校验 + hf_token 蜜罐与 action 无关（#138）。"""

    def test_export_requires_task_id(self):
        # export 缺 task_id 会拼出数据目录本身（NOTE_OUTPUT_DIR / ""），入口显式报错
        with self.assertRaises(ValueError):
            server.process_media(action="export")

    def test_merge_requires_files(self):
        with self.assertRaises(ValueError):
            server.process_media(action="merge")

    def test_diarize_requires_audio_file(self):
        with self.assertRaises(ValueError):
            server.process_media(action="diarize")

    def test_unknown_action_rejected(self):
        with self.assertRaises(ValueError):
            server.process_media(action="bogus")

    def test_hf_token_rejected_on_diarize(self):
        # 蜜罐参数：非空即拒（凭证红线，原 diarize_media 契约 #122 保留）
        with self.assertRaises(ValueError):
            server.process_media(action="diarize", audio_file="/tmp/x.wav", hf_token="hf_xxx")

    def test_hf_token_rejected_regardless_of_action(self):
        # 防切换 action 绕过：export 分支传 hf_token 同样拒（蜜罐在入口、与 action 无关）
        with self.assertRaises(ValueError):
            server.process_media(action="export", task_id="task-1", hf_token="hf_xxx")


class HealthCheckShapeTest(unittest.TestCase):
    """health_check 统一形状：ok/checks 恒在、db 检查项并入（#138）。"""

    def test_base_shape_has_ok_and_checks(self):
        data = json.loads(server.health_check())
        self.assertIn("ok", data)
        self.assertIn("checks", data)
        names = [c["name"] for c in data["checks"]]
        self.assertIn("ffmpeg", names)
        self.assertIn("db", names)
        self.assertIn("disk", names)
        self.assertIn("transcriber", names)
        self.assertIn("queue", names)

    def test_meta_fields_kept(self):
        # 原 health_check 元信息字段保留（HealthCheckVersionTest 依赖：#138 零改动通过）
        data = json.loads(server.health_check())
        for key in (
            "server_version", "plugin_version", "whisper_models", "engine_advice",
            "audio_enhance", "keyed_providers", "queue_length", "max_workers",
            "data_dir", "skill_refresh",
        ):
            self.assertIn(key, data, f"health_check 缺少元信息字段 {key}")

    def test_need_provider_false_skips_provider_check(self):
        # #124 A12：素材包场景（prepare_note_material 不调 LLM）跳过供应商检查
        data = json.loads(server.health_check(need_provider=False))
        names = [c["name"] for c in data["checks"]]
        self.assertNotIn("provider", names)
