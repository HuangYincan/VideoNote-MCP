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


class PipelineRetiredPropagationTest(unittest.TestCase):
    """退役异常必须穿过分块通用容错向上传播，不能把缺块结果当成功返回。

    否则：第一块成功、第二块退役被跳过 → 流水线返回部分结果 → 写入任务/跨任务缓存
    → 后续同模型请求命中缺块缓存（评审 Spec P2）。
    """

    def _result(self, text):
        from app.models.transcriber_model import TranscriptResult, TranscriptSegment

        return TranscriptResult(
            language="zh",
            full_text=text,
            segments=[TranscriptSegment(start=0.0, end=1.0, text=text)],
        )

    def _fake(self, fn):
        class _T:
            def transcript(self, file_path, cancel_event=None):
                return fn(cancel_event)

        return _T()

    def _patches(self):
        return (
            mock.patch(
                "app.transcriber.audio_preprocess.normalize_to_wav",
                return_value="/tmp/vn_review58.wav",
            ),
            mock.patch(
                "app.transcriber.audio_preprocess.chunk_if_long",
                return_value=["c1", "c2", "c3"],
            ),
            mock.patch("app.services.pipeline.chunk_duration_guess", return_value=10.0),
            mock.patch(
                "app.services.pipeline.apply_diarization",
                side_effect=lambda audio_file, segments, **kw: segments,
            ),
        )

    def test_retired_error_propagates_and_no_partial_result(self):
        from app.exceptions.task import TranscriberRetiredError
        from app.services import pipeline

        calls = {"n": 0}

        def fn(_cancel):
            calls["n"] += 1
            if calls["n"] == 1:
                return self._result("第一块")
            raise TranscriberRetiredError("retired")

        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            with self.assertRaises(TranscriberRetiredError):
                pipeline._transcribe_with_preprocess("/tmp/src.mp4", self._fake(fn))
        self.assertEqual(calls["n"], 2, "第一块成功后，第二块应触发退役异常并向上传播")

    def test_generic_chunk_failure_still_skipped(self):
        """通用单块失败仍按原语义跳过，返回 truncated 部分结果（不被本次改动放大）。"""
        from app.services import pipeline

        calls = {"n": 0}

        def fn(_cancel):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("单块瞬时失败")
            return self._result("后续块")

        p1, p2, p3, p4 = self._patches()
        with p1, p2, p3, p4:
            result = pipeline._transcribe_with_preprocess("/tmp/src.mp4", self._fake(fn))
        self.assertTrue(result.get("truncated"))
        self.assertEqual(calls["n"], 3)


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
