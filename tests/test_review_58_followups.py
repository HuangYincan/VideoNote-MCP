"""PR #58 评审遗留问题的回归测试（S2）。

覆盖：
- `run_ffmpeg_cancellable` 的 PIPE→DEVNULL 降级与 `stdin=DEVNULL` 收口；
- `LocalDownloader` 两个调用点不再传 PIPE；
- `note.py` 独立 ffmpeg 入口（截图/视频理解路径）补齐 `-nostdin` + `stdin=DEVNULL`；
- Windows 预热分支的平台/开关判定（`videonote_mcp.preheat`）。

S1/R5（回收竞态）、R2（退役）、S3（重建取消）、R4（首建挂计时器）的回归在
`tests/test_gpu_idle_release.py`。
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_REPO_ROOT = Path(__file__).resolve().parents[1]


class RunFfmpegCancellableIsolationTest(unittest.TestCase):
    """helper 只 poll、从不读管道：PIPE 必须就地降级，stdin 必须隔离。"""

    def _run(self, **kwargs):
        proc = mock.Mock()
        proc.poll = mock.Mock(return_value=0)
        proc.returncode = 0
        with mock.patch(
            "app.downloaders.common.subprocess.Popen", return_value=proc
        ) as popen:
            from app.downloaders.common import run_ffmpeg_cancellable

            run_ffmpeg_cancellable(["ffmpeg", "-i", "in", "out"], **kwargs)
        return popen.call_args

    def test_pipe_downgraded_to_devnull(self):
        # 旧实现把 PIPE 原样传给 Popen → ffmpeg 写满缓冲后死锁，永不返回
        call = self._run(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)

    def test_stdin_always_devnull(self):
        # 子进程不得继承 MCP 的 JSON-RPC stdin 管道（收尾阶段可能触碰句柄而卡住）
        call = self._run()
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)

    def test_explicit_streams_preserved(self):
        call = self._run(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertEqual(call.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["stderr"], subprocess.DEVNULL)


class LocalDownloaderCallSitesTest(unittest.TestCase):
    """本地转 mp3 / 抽封面两个调用点不得再传 PIPE（issue #56 本地视频死锁根因）。"""

    def test_callers_do_not_pass_pipe(self):
        from app.downloaders.local_downloader import LocalDownloader

        dl = LocalDownloader()
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "clip.mp4"
            src.write_bytes(b"fake")
            out = Path(td) / "clip.mp3"
            with mock.patch(
                "app.downloaders.local_downloader.run_ffmpeg_cancellable"
            ) as helper:
                dl.convert_to_mp3(str(src), str(out))
                dl.extract_cover(str(src), output_dir=td)
        self.assertEqual(helper.call_count, 2)
        for call in helper.call_args_list:
            self.assertNotIn("stdout", call.kwargs)
            self.assertNotIn("stderr", call.kwargs)


class NoteExtractAudioStdinTest(unittest.TestCase):
    """`note.py` 的独立 ffmpeg 入口（截图/视频理解）也要隔离 stdin。"""

    def test_nostdin_and_devnull_stdin(self):
        from app.services.note import _extract_audio_from_video

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "clip.mp4"
            src.write_bytes(b"fake")
            proc = mock.Mock()
            proc.poll = mock.Mock(return_value=0)
            proc.returncode = 0
            proc.communicate = mock.Mock(return_value=("", ""))
            with mock.patch("subprocess.Popen", return_value=proc) as popen:
                _extract_audio_from_video(str(src), td)
        cmd = popen.call_args.args[0]
        self.assertIn("-nostdin", cmd, "命令行必须带 -nostdin")
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)


class PreheatTest(unittest.TestCase):
    """Windows 预热分支：平台 + 开关判定可测，导入失败不影响启动。"""

    @classmethod
    def setUpClass(cls):
        from videonote_mcp import preheat

        cls.preheat = preheat

    def test_non_windows_is_noop(self):
        with mock.patch.object(self.preheat.importlib, "import_module") as imp:
            self.assertFalse(
                self.preheat.preheat_transcriber_engine(
                    platform="linux", enabled=True, logger=mock.Mock()
                )
            )
        imp.assert_not_called()

    def test_disabled_switch_skips_import(self):
        with mock.patch.object(self.preheat.importlib, "import_module") as imp:
            self.assertFalse(
                self.preheat.preheat_transcriber_engine(
                    platform="win32", enabled=False, logger=mock.Mock()
                )
            )
        imp.assert_not_called()

    def test_win32_enabled_imports_engine(self):
        with mock.patch.object(self.preheat.importlib, "import_module") as imp:
            self.assertTrue(
                self.preheat.preheat_transcriber_engine(
                    platform="win32", enabled=True, logger=mock.Mock()
                )
            )
        imp.assert_called_once_with("faster_whisper")

    def test_import_failure_is_non_fatal(self):
        log = mock.Mock()
        with mock.patch.object(
            self.preheat.importlib, "import_module", side_effect=RuntimeError("boom")
        ):
            self.assertFalse(
                self.preheat.preheat_transcriber_engine(
                    platform="win32", enabled=True, logger=log
                )
            )
        log.warning.assert_called_once()

    def test_env_flag_default_on_and_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(self.preheat.ENV_PREHEAT, None)
            with mock.patch.object(self.preheat.importlib, "import_module") as imp:
                self.assertTrue(
                    self.preheat.preheat_transcriber_engine(
                        platform="win32", logger=mock.Mock()
                    )
                )
            imp.assert_called_once()
        with mock.patch.dict(os.environ, {self.preheat.ENV_PREHEAT: "0"}):
            with mock.patch.object(self.preheat.importlib, "import_module") as imp:
                self.assertFalse(
                    self.preheat.preheat_transcriber_engine(
                        platform="win32", logger=mock.Mock()
                    )
                )
            imp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
