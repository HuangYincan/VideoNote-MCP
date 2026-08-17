"""说话人分离（app/services/diarization.py）单元测试。

不碰真实 pyannote（重依赖未装）——验证：
1. pyannote 未装时 diarize_audio 抛 RuntimeError 带安装指引；
2. 缺 HF_TOKEN 时报错；
3. assign_speakers 按时间重叠对齐给段填 speaker。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.transcriber_model import TranscriptSegment
from app.services import diarization


def _real_import_blocker():
    """返回一个 __import__ 替身：仅对 pyannote.audio 抛 ImportError，其余透传。"""
    real_import = __import__

    def fake_import(name, *args, **kwargs):
        if name == "pyannote.audio":
            raise ImportError("no pyannote")
        return real_import(name, *args, **kwargs)

    return fake_import


class DiarizeNotInstalledTest(unittest.TestCase):
    def test_raises_install_hint_when_pyannote_missing(self):
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            with mock.patch("builtins.__import__", side_effect=_real_import_blocker()):
                with self.assertRaises(RuntimeError) as ctx:
                    diarization.diarize_audio(f.name, hf_token="t")
        self.assertIn("pyannote", str(ctx.exception))

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            diarization.diarize_audio("/no/such.wav", hf_token="t")

    def test_missing_token_raises(self):
        # pyannote 可 import 但未传 token 且环境无 HUGGINGFACE_HUB_TOKEN → RuntimeError
        import types

        fake_pkg = types.ModuleType("pyannote.audio")
        fake_pkg.Pipeline = mock.Mock()
        sys.modules["pyannote.audio"] = fake_pkg
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                with mock.patch.dict(
                    "os.environ", {}, clear=False
                ):
                    # 确保模块内读 HUGGINGFACE_HUB_TOKEN 为空
                    with mock.patch(
                        "app.services.diarization.os.environ.get", return_value=""
                    ):
                        with self.assertRaises(RuntimeError) as ctx:
                            diarization.diarize_audio(f.name)  # 无 hf_token
                    self.assertIn("HF_TOKEN", str(ctx.exception))
        finally:
            sys.modules.pop("pyannote.audio", None)


class AssignSpeakersTest(unittest.TestCase):
    def _turns(self):
        return [
            {"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
            {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"},
        ]

    def test_assigns_overlap_speaker(self):
        segs = [
            TranscriptSegment(0, 2, "你好"),
            TranscriptSegment(6, 8, "世界"),
        ]
        out = diarization.assign_speakers(segs, self._turns())
        self.assertEqual(out[0].speaker, "SPEAKER_00")
        self.assertEqual(out[1].speaker, "SPEAKER_01")

    def test_no_overlap_keeps_none(self):
        segs = [TranscriptSegment(100, 102, "无重叠")]
        out = diarization.assign_speakers(segs, self._turns())
        self.assertIsNone(out[0].speaker)

    def test_original_not_mutated(self):
        segs = [TranscriptSegment(0, 2, "你好")]
        diarization.assign_speakers(segs, self._turns())
        self.assertIsNone(segs[0].speaker)


class NumSpeakersValidationTest(unittest.TestCase):
    """num_speakers 无效值（0/负/非 int）此前静默回退自动检测（falsy → kwargs 省略）——
    加 warning 提示；合法值正常透传（#101）。"""

    def _run(self, num_speakers):
        import types

        fake_pipeline = mock.Mock()
        fake_pipeline.return_value = fake_pipeline  # pipeline(wav) 调用自返回，itertracks 在实例上
        fake_pipeline.itertracks.return_value = iter([])
        fake_pkg = types.ModuleType("pyannote.audio")
        fake_pkg.Pipeline = mock.Mock()
        fake_pkg.Pipeline.from_pretrained = mock.Mock(return_value=fake_pipeline)
        sys.modules["pyannote.audio"] = fake_pkg
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav") as f:
                diarization.diarize_audio(f.name, hf_token="t", num_speakers=num_speakers)
        finally:
            sys.modules.pop("pyannote.audio", None)
        return fake_pipeline

    def test_zero_falls_back_with_warning(self):
        with self.assertLogs("app.services.diarization", level="WARNING") as logs:
            pipe = self._run(0)
        self.assertTrue(any("回退自动检测" in m for m in logs.output))
        self.assertEqual(pipe.call_args.kwargs, {})  # 不带 num_speakers

    def test_negative_falls_back_with_warning(self):
        with self.assertLogs("app.services.diarization", level="WARNING") as logs:
            pipe = self._run(-3)
        self.assertTrue(any("回退自动检测" in m for m in logs.output))
        self.assertEqual(pipe.call_args.kwargs, {})

    def test_none_passes_without_warning(self):
        pipe = self._run(None)
        self.assertEqual(pipe.call_args.kwargs, {})

    def test_valid_passes_through(self):
        pipe = self._run(2)
        self.assertEqual(pipe.call_args.kwargs, {"num_speakers": 2})


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestDiarizationMainPath(unittest.TestCase):
    """#31 主路径接入：apply_diarization 开关行为 + prompt speaker 渲染。"""

    def test_apply_diarization_disabled_returns_segments(self):
        # 配置默认 diarization=off：原样返回，不碰 pyannote/normalize
        from app.services import pipeline
        from app.services.transcriber_config_manager import TranscriberConfigManager

        if TranscriberConfigManager().get_diarization():
            self.skipTest("测试环境配置了 diarization，跳过（默认应为关）")
        segs = [TranscriptSegment(start=0.0, end=1.0, text="a")]
        with mock.patch("app.transcriber.audio_preprocess.normalize_to_wav", side_effect=AssertionError("不应归一化")) as m:
            out = pipeline.apply_diarization("/tmp/x.mp3", segs)
        self.assertIs(out, segs)
        m.assert_not_called()

    def test_apply_diarization_failure_returns_segments(self):
        # 启用时 pyannote 未装 → 失败回退原 segments，不抛
        from app.services import pipeline
        from app.services.transcriber_config_manager import TranscriberConfigManager

        mgr = TranscriberConfigManager()
        old = mgr.get_diarization()
        segs = [TranscriptSegment(start=0.0, end=1.0, text="a")]
        try:
            with mock.patch.object(type(mgr), "get_diarization", return_value=True):
                with mock.patch.object(type(mgr), "get_diarization_speakers", return_value=None):
                    out = pipeline.apply_diarization("/tmp/x.mp3", segs)
        finally:
            _ = old
        self.assertIs(out, segs)

    def test_prompt_renders_speaker_when_multiple(self):
        from app.gpt.universal_gpt import UniversalGPT

        gpt = UniversalGPT.__new__(UniversalGPT)  # 不跑 __init__（避免 env/IO）
        segs = [
            TranscriptSegment(start=0.0, end=1.0, text="hello", speaker="SPEAKER_00"),
            TranscriptSegment(start=1.0, end=2.0, text="world", speaker="SPEAKER_01"),
        ]
        text = gpt._build_segment_text(segs)
        self.assertIn("[SPEAKER_00]", text)
        self.assertIn("[SPEAKER_01]", text)

    def test_prompt_skips_single_speaker_noise(self):
        from app.gpt.universal_gpt import UniversalGPT

        gpt = UniversalGPT.__new__(UniversalGPT)
        segs = [
            TranscriptSegment(start=0.0, end=1.0, text="hello", speaker="SPEAKER_00"),
            TranscriptSegment(start=1.0, end=2.0, text="world", speaker="SPEAKER_00"),
        ]
        text = gpt._build_segment_text(segs)
        self.assertNotIn("SPEAKER_00", text)


class DiarizeMediaCleanupTest(unittest.TestCase):
    """MCP diarize_media 的归一化临时文件清理（#121 C2）。

    归一化产物写在源文件旁（audio_preprocess 缺省路径），用完即清——
    否则每次调用在用户目录永久残留 <原名>_16k.wav（小时级视频数百 MB）。
    """

    def setUp(self):
        import os
        import shutil

        self._os = os
        self._shutil = shutil
        self.tmp = tempfile.mkdtemp(prefix="vn_diarize_")
        self.src = os.path.join(self.tmp, "in.mp4")
        Path(self.src).write_bytes(b"fake")
        self.wav = os.path.join(self.tmp, "in_16k.wav")

    def tearDown(self):
        self._shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, side_effect=None):
        import videonote_mcp.server as server

        with mock.patch("videonote_mcp.server._coerce_local_path", return_value=Path(self.src)):
            with mock.patch(
                "app.transcriber.audio_preprocess.normalize_to_wav", return_value=self.wav
            ):
                with mock.patch("app.services.diarization.diarize_audio", side_effect=side_effect):
                    with mock.patch(
                        "app.transcriber.audio_preprocess.cleanup_preprocess_files"
                    ) as m_clean:
                        server.diarize_media(self.src)
        return m_clean

    def test_success_path_cleans_normalized_wav(self):

        m_clean = self._call(side_effect=[{"segments": [{"start": 0, "end": 1, "speaker": "A"}]}])
        m_clean.assert_called_once_with(self.wav)

    def test_exception_path_still_cleans(self):
        import json as _json

        import videonote_mcp.server as server

        # 工具入口不裸抛（外层 except 归一为 {ok: False, error}），
        # 但 finally 清理必须执行——不残留归一化 wav
        with mock.patch("videonote_mcp.server._coerce_local_path", return_value=Path(self.src)):
            with mock.patch(
                "app.transcriber.audio_preprocess.normalize_to_wav", return_value=self.wav
            ):
                with mock.patch(
                    "app.services.diarization.diarize_audio",
                    side_effect=RuntimeError("pyannote 未装"),
                ):
                    with mock.patch(
                        "app.transcriber.audio_preprocess.cleanup_preprocess_files"
                    ) as m_clean:
                        out = server.diarize_media(self.src)
        m_clean.assert_called_once_with(self.wav)
        self.assertFalse(_json.loads(out)["ok"])


if __name__ == "__main__":
    unittest.main()
