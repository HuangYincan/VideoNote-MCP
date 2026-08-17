"""Groq 转写压缩临时文件的失败清理（#121 B7）。

compress_audio 先 mkstemp 落盘再跑 ffmpeg：ffmpeg 失败时旧实现不删临时文件
（调用方拿不到路径，transcript() 的 finally 无从删起）→ 残留临时 mp3。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.transcriber import groq


class CompressAudioCleanupTest(unittest.TestCase):
    def _patch_mkstemp(self, path):
        return mock.patch("tempfile.mkstemp", return_value=(12345, path))

    def test_ffmpeg_failure_removes_temp_file(self):
        tmp = "/tmp/videonote_groq_fail_test.mp3"
        Path(tmp).write_bytes(b"")
        try:
            with self._patch_mkstemp(tmp):
                with mock.patch("os.close"):
                    with mock.patch("ffmpeg.input") as m_in:
                        m_in.return_value.output.return_value.run.side_effect = RuntimeError("ffmpeg 炸了")
                        with self.assertRaises(RuntimeError):
                            groq.compress_audio("/tmp/in.mp4")
            self.assertFalse(Path(tmp).exists())  # 临时文件已清
        finally:
            Path(tmp).unlink(missing_ok=True)

    def test_success_keeps_and_returns_path(self):
        tmp = "/tmp/videonote_groq_ok_test.mp3"
        Path(tmp).unlink(missing_ok=True)
        Path(tmp).write_bytes(b"")  # 模拟 mkstemp 已创建真实文件
        try:
            with self._patch_mkstemp(tmp):
                with mock.patch("os.close"):
                    with mock.patch("ffmpeg.input") as m_in:
                        m_in.return_value.output.return_value.run.return_value = None
                        got = groq.compress_audio("/tmp/in.mp4")
            self.assertEqual(got, tmp)
            self.assertTrue(Path(tmp).exists())
        finally:
            Path(tmp).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
