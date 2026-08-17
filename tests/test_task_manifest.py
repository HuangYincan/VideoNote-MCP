"""
task_manifest 清理功能测试（不碰真实网络/数据库，只用临时目录）。

运行（仓库根目录）：
    PYTHONPATH=. .venv/bin/python tests/test_task_manifest.py
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 数据层重构：cleanup_all_files 会同步清空 video_tasks 全局索引 → 用隔离 DB。
# 与会话级 conftest 同库（全量 pytest 时 conftest 已设，setdefault 不覆盖）；
# 直接 `python tests/test_task_manifest.py` 时 conftest 不加载，setdefault 兜底。
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/videonote_pytest/video_note.db")

from app.utils.task_manifest import (  # noqa: E402
    cleanup_all_files,
    cleanup_task_files,
    get_task_meta,
    get_task_paths,
    list_task_files,
    record_task_meta,
    record_task_paths,
)


class TaskManifestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # resolve 掉 macOS 的 /var → /private/var 软链，保证 manifest 记录与解析一致
        self.root = Path(self._tmp.name).resolve()
        # 目录布局模拟 MCP 数据目录（config.setup_environment 建的那一套）
        self.note_dir = self.root / "note_results"
        self.screens = self.root / "static" / "screenshots"
        self.cfg = self.root / "config"
        self.logs = self.root / "logs"
        self.models = self.root / "models"
        for d in (self.note_dir, self.screens, self.cfg, self.logs, self.models):
            d.mkdir(parents=True, exist_ok=True)
        os.environ["VIDEONOTE_DATA_DIR"] = str(self.root)
        os.environ["NOTE_OUTPUT_DIR"] = str(self.note_dir)
        os.environ["IMAGE_OUTPUT_DIR"] = str(self.screens)
        os.environ["VIDEONOTE_CONFIG_DIR"] = str(self.cfg)

    def tearDown(self):
        self._tmp.cleanup()
        for k in (
            "VIDEONOTE_DATA_DIR",
            "NOTE_OUTPUT_DIR",
            "IMAGE_OUTPUT_DIR",
            "VIDEONOTE_CONFIG_DIR",
        ):
            os.environ.pop(k, None)

    # ---------- 造假 task 产物 ----------

    def _make_task(self, task_id: str) -> Path:
        """造假 task（新结构）：task_dir/raw + gen（note.md/Assets/frames）+ status/result/manifest。"""
        task_dir = self.note_dir / task_id
        raw = task_dir / "raw"
        gen = task_dir / "gen"
        raw.mkdir(parents=True, exist_ok=True)
        gen.mkdir(parents=True, exist_ok=True)
        (raw / "video.mp4").write_bytes(b"x")
        (gen / "transcript.json").write_text("{}", encoding="utf-8")
        (gen / "note.md").write_text("# 最终笔记", encoding="utf-8")
        assets = gen / "Assets"
        assets.mkdir(parents=True, exist_ok=True)
        (assets / "1.jpg").write_bytes(b"y")
        frames = gen / "frames"
        frames.mkdir(parents=True, exist_ok=True)
        (frames / "f1.jpg").write_bytes(b"f")
        (task_dir / "status.json").write_text('{"status":"SUCCESS"}', encoding="utf-8")
        (task_dir / "result.json").write_text('{"markdown":"# 最终"}', encoding="utf-8")
        record_task_paths(
            task_id,
            [
                task_dir,
                raw,
                gen,
                gen / "transcript.json",
                gen / "note.md",
                task_dir / "status.json",
                task_dir / "result.json",
            ],
        )
        record_task_meta(task_id, {"title": "测试笔记"})
        return task_dir

    # ---------- manifest 记录 / 读取 ----------

    def test_record_and_get_dedup(self):
        tid = "abc123"
        td = self.note_dir / tid
        record_task_paths(tid, [str(td / "a.json"), str(td / "b.json")])
        record_task_paths(tid, [str(td / "b.json"), str(td / "c.json")])
        paths = get_task_paths(tid)
        self.assertEqual(len(paths), 3)
        self.assertIn(str(td / "a.json"), paths)
        self.assertIn(str(td / "b.json"), paths)
        self.assertIn(str(td / "c.json"), paths)
        # manifest 落在任务文件夹内
        self.assertTrue((td / "manifest.json").exists())

    def test_record_meta_preserved(self):
        tid = "meta01"
        record_task_paths(tid, [str(self.note_dir / tid / "x.json")])
        record_task_meta(tid, {"title": "标题A"})
        record_task_paths(tid, [str(self.note_dir / tid / "y.json")])  # 再次记路径，meta 不丢
        self.assertEqual(get_task_meta(tid).get("title"), "标题A")

    def test_get_task_paths_missing(self):
        self.assertEqual(get_task_paths("nope"), [])

    def test_record_empty_task_id_noop(self):
        record_task_paths("", [str(self.note_dir / "x.json")])
        self.assertFalse((self.note_dir / ".manifest.json").exists())

    # ---------- get_task_files（先查后清） ----------

    def test_list_task_files(self):
        tid = "task01"
        task_dir = self._make_task(tid)
        info = list_task_files(tid)
        self.assertEqual(info["task_id"], tid)
        # manifest 记录的路径都在 existing 里（去重后）
        for p in get_task_paths(tid):
            self.assertIn(str(Path(p)), info["existing"])
        # 任务文件夹 / raw / gen 与最终笔记都在
        self.assertTrue(any("raw" == Path(s).name for s in info["existing"]))
        self.assertTrue(any(s.endswith("note.md") for s in info["existing"]))
        self.assertTrue(task_dir.exists())
        # meta 透出
        self.assertEqual(info["meta"].get("title"), "测试笔记")

    # ---------- cleanup_note ----------

    def test_cleanup_note_keeps_note(self):
        tid = "task02"
        task_dir = self._make_task(tid)
        gen = task_dir / "gen"
        raw = task_dir / "raw"
        res = cleanup_task_files(tid, include_note=False)
        # raw 整个被删
        self.assertFalse(raw.exists())
        # gen 内非 note.md 被删（transcript/frames）
        self.assertFalse((gen / "transcript.json").exists())
        self.assertFalse((gen / "frames").exists())
        # Assets/ 是 note.md 相对引用（Assets/...）的截图，保留笔记时必须保留
        self.assertTrue((gen / "Assets").exists())
        # 最终笔记保留
        self.assertTrue((gen / "note.md").exists())
        self.assertTrue(res["note_kept"])
        # 控制文件保留（status/manifest/result）
        self.assertTrue((task_dir / "status.json").exists())
        self.assertTrue((task_dir / "manifest.json").exists())
        self.assertTrue((task_dir / "result.json").exists())

    def test_cleanup_note_include_note(self):
        tid = "task03"
        task_dir = self._make_task(tid)
        res = cleanup_task_files(tid, include_note=True)
        # 连最终笔记 + 整个任务文件夹一起删
        self.assertFalse(task_dir.exists())
        self.assertFalse(res["note_kept"])

    # ---------- 路径穿越防护 ----------

    def test_cleanup_note_path_traversal_rejected(self):
        tid = "task04"
        self._make_task(tid)
        outside = self.root.parent / "evil.txt"
        outside.write_text("do not delete", encoding="utf-8")
        # 恶意路径进 manifest：数据目录外的绝对路径 + 相对穿越路径
        record_task_paths(tid, [str(outside), "../../../../etc/passwd"])
        res = cleanup_task_files(tid, include_note=True)
        # 外部文件仍在（越界路径被 resolve 校验拒绝）
        self.assertTrue(outside.exists())
        self.assertNotIn(str(outside), res["deleted"])
        self.assertNotIn(str(outside), res["errors"])
        # 数据目录内的正常产物照常被删
        self.assertFalse((self.note_dir / tid).exists())
        outside.unlink(missing_ok=True)

    # ---------- cleanup_all ----------

    def test_cleanup_all_keeps_config_models(self):
        (self.note_dir / "x.json").write_text("{}", encoding="utf-8")
        (self.screens / "a.jpg").write_bytes(b"a")
        (self.logs / "mcp_stderr.log").write_text("log", encoding="utf-8")
        (self.cfg / "app_config.json").write_text("{}", encoding="utf-8")
        (self.models / "whisper").mkdir(exist_ok=True)
        (self.models / "whisper" / "model.bin").write_bytes(b"m")
        res = cleanup_all_files(include_config=False, include_models=False)
        # 清空 note_results / screenshots / logs
        self.assertEqual(list(self.note_dir.iterdir()), [])
        self.assertEqual(list(self.screens.iterdir()), [])
        self.assertEqual(list(self.logs.iterdir()), [])
        # 保留 config / models
        self.assertTrue((self.cfg / "app_config.json").exists())
        self.assertTrue((self.models / "whisper" / "model.bin").exists())
        self.assertIn("config", res["kept"])
        self.assertIn("models", res["kept"])

    def test_cleanup_all_include_config(self):
        (self.note_dir / "y.json").write_text("{}", encoding="utf-8")
        (self.cfg / "app_config.json").write_text("{}", encoding="utf-8")
        (self.models / "whisper").mkdir(exist_ok=True)
        (self.models / "whisper" / "model.bin").write_bytes(b"m")
        res = cleanup_all_files(include_config=True, include_models=False)
        # config 被清，models 保留
        self.assertEqual(list(self.cfg.iterdir()), [])
        self.assertTrue((self.models / "whisper" / "model.bin").exists())
        self.assertIn("models", res["kept"])
        self.assertNotIn("config", res["kept"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
