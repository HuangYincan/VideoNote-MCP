"""save_cover_to_static 封面落盘位置的隔离验证。

背景：MCP 模式下 data 目录是隔离的（VIDEONOTE_DATA_DIR），但 save_cover_to_static
原来写 `CWD/static/cover/` —— 会把封面写进仓库工作区（冒烟测试发现 `static/cover/smoke_cover.jpg`）。
修复后：有 VIDEONOTE_DATA_DIR 时写 `<data>/static/cover/`；无 env 时回退 CWD（兼容 CLI）。
"""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils.video_helper import save_cover_to_static  # noqa: E402


class SaveCoverTest(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.old_data_dir = os.environ.get("VIDEONOTE_DATA_DIR")
        self.old_cwd = os.getcwd()

    def tearDown(self):
        if self.old_data_dir is None:
            os.environ.pop("VIDEONOTE_DATA_DIR", None)
        else:
            os.environ["VIDEONOTE_DATA_DIR"] = self.old_data_dir
        os.chdir(self.old_cwd)
        self.td.cleanup()

    def _cover_file(self) -> Path:
        p = Path(self.td.name) / "src_cover.jpg"
        p.write_bytes(b"cover-bytes")
        return p

    def test_writes_to_data_dir_when_env_set(self):
        data_dir = Path(self.td.name) / "data"
        os.environ["VIDEONOTE_DATA_DIR"] = str(data_dir)
        url = save_cover_to_static(str(self._cover_file()))
        target = data_dir / "static" / "cover" / "src_cover.jpg"
        self.assertTrue(target.exists())
        self.assertEqual(target.read_bytes(), b"cover-bytes")
        # 返回 file:// 绝对路径（agent 可直接 Read；无后端可指）
        self.assertTrue(url.startswith("file://"))
        self.assertIn("src_cover.jpg", url)

    def test_falls_back_to_cwd_static_without_env(self):
        os.environ.pop("VIDEONOTE_DATA_DIR", None)
        with tempfile.TemporaryDirectory() as cwd:
            os.chdir(cwd)
            save_cover_to_static(str(self._cover_file()))
            target = Path(cwd) / "static" / "cover" / "src_cover.jpg"
            self.assertTrue(target.exists())
            # 清理本测试在临时 CWD 造的 static（仍在 with 块内，目录有效）
            shutil.rmtree(target.parent.parent, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
