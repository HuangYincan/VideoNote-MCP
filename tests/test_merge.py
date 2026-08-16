"""多文件合并（app/services/merge.py）单元测试。

不碰真实 ffmpeg 转换 —— mock subprocess.run 验证流程与参数。

覆盖：
1. 少于 2 个文件报错；
2. 文件不存在报错；
3. 正常合并：统一转 wav → concat → 返回输出路径；
4. 输出目录自动创建。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.merge import merge_audio


class MergeAudioTest(unittest.TestCase):
    def test_requires_at_least_two_files(self):
        with self.assertRaises(ValueError):
            merge_audio(["/tmp/a.mp3"])

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            merge_audio(["/no/a.mp3", "/no/b.mp3"])

    def test_merge_produces_output(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d) / "a.mp3"
            b = Path(d) / "b.mp3"
            a.write_bytes(b"fake-a")
            b.write_bytes(b"fake-b")
            # mock subprocess.run：成功返回，record 调用
            with mock.patch("app.services.merge.subprocess.run", return_value=mock.Mock(returncode=0)) as run:
                out = merge_audio([str(a), str(b)], out_dir=d, out_name="merged")
            self.assertTrue(out.endswith("merged.wav"))
            # ffmpeg 被调用：2 个文件各转 wav + 1 次 concat = 3 次
            self.assertEqual(run.call_count, 3)

    def test_concat_failure_raises(self):
        with tempfile.TemporaryDirectory() as d:
            a = Path(d) / "a.mp3"
            b = Path(d) / "b.mp3"
            a.write_bytes(b"fake-a")
            b.write_bytes(b"fake-b")
            # 转换成功但 concat 失败
            def fake_run(cmd, **kw):
                if "-f" in cmd and "concat" in cmd:
                    return mock.Mock(returncode=1, stderr=b"boom")
                return mock.Mock(returncode=0)

            with mock.patch("app.services.merge.subprocess.run", side_effect=fake_run):
                with self.assertRaises(RuntimeError):
                    merge_audio([str(a), str(b)], out_dir=d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
