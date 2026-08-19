"""diarization 模块级单例测试（#125 B14）：pipeline 只加载一次、失败不缓存。

不碰真实网络 / pyannote，全 mock。运行：
    cd <repo>
    .venv/bin/python tests/test_diarization.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _install_fake_pyannote(pipeline_cls):
    """注入 fake pyannote.audio 模块（带可替换的 Pipeline 类）。"""
    fake = mock.Mock()
    fake.Pipeline = pipeline_cls
    sys.modules["pyannote"] = fake
    sys.modules["pyannote.audio"] = fake


class _FakePipeline:
    """可注入的 Pipeline 替身：from_pretrained 计数 + 每次调用返回 turns。"""

    from_pretrained_count = 0

    def __init__(self):
        self.calls = []

    @classmethod
    def from_pretrained(cls, model, token=None):
        cls.from_pretrained_count += 1
        return cls()

    def __call__(self, wav_path, **kwargs):
        self.calls.append(wav_path)
        return self

    def itertracks(self, yield_label=True):
        yield (mock.Mock(start=0.0, end=1.0), None, "SPEAKER_00")


class DiarizationSingletonTest(unittest.TestCase):
    def setUp(self):
        from app.services import diarization

        self.mod = diarization
        # 重置模块级缓存与计数
        self.mod._pipeline_cache = None
        self.mod._pipeline_token = ""
        _FakePipeline.from_pretrained_count = 0
        self.td = tempfile.TemporaryDirectory()
        self.wav = Path(self.td.name) / "a.wav"
        self.wav.write_bytes(b"wav")
        os.environ.pop("HUGGINGFACE_HUB_TOKEN", None)

    def tearDown(self):
        self.td.cleanup()
        sys.modules.pop("pyannote", None)
        sys.modules.pop("pyannote.audio", None)
        os.environ.pop("HUGGINGFACE_HUB_TOKEN", None)

    def test_pipeline_loaded_once_then_cached(self):
        """两次调用只 from_pretrained 一次（#125 B14）。"""
        _install_fake_pyannote(_FakePipeline)
        self.mod.diarize_audio(str(self.wav), hf_token="tok")
        self.mod.diarize_audio(str(self.wav), hf_token="tok")
        self.assertEqual(_FakePipeline.from_pretrained_count, 1)

    def test_failure_not_cached_retries_next_time(self):
        """加载失败不缓存：下次调用重试 from_pretrained（#125 B14）。"""

        class _FlakyPipeline(_FakePipeline):
            fail = True

            @classmethod
            def from_pretrained(cls, model, token=None):
                cls.from_pretrained_count += 1
                if cls.fail:
                    raise RuntimeError("模型授权未同意")
                return cls()

        _install_fake_pyannote(_FlakyPipeline)
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.diarize_audio(str(self.wav), hf_token="tok")
        self.assertIn("模型加载失败", str(ctx.exception))
        # 修复后再次调用 → 重新加载成功
        _FlakyPipeline.fail = False
        turns = self.mod.diarize_audio(str(self.wav), hf_token="tok")
        self.assertEqual(_FlakyPipeline.from_pretrained_count, 2)
        self.assertEqual(turns[0]["speaker"], "SPEAKER_00")

    def test_token_change_reloads(self):
        """token 变化 → 重新加载（不同账号授权不同模型）。"""
        _install_fake_pyannote(_FakePipeline)
        self.mod.diarize_audio(str(self.wav), hf_token="tok1")
        self.mod.diarize_audio(str(self.wav), hf_token="tok2")
        self.assertEqual(_FakePipeline.from_pretrained_count, 2)

    def test_missing_token_raises_clear_error(self):
        _install_fake_pyannote(_FakePipeline)
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.diarize_audio(str(self.wav), hf_token="")
        self.assertIn("HF_TOKEN", str(ctx.exception))
        self.assertEqual(_FakePipeline.from_pretrained_count, 0)

    def test_not_installed_raises_install_hint(self):
        sys.modules.pop("pyannote", None)
        sys.modules.pop("pyannote.audio", None)
        with self.assertRaises(RuntimeError) as ctx:
            self.mod.diarize_audio(str(self.wav), hf_token="tok")
        self.assertIn("pyannote.audio", str(ctx.exception))

    def test_missing_file_raises_before_loading(self):
        _install_fake_pyannote(_FakePipeline)
        with self.assertRaises(FileNotFoundError):
            self.mod.diarize_audio(str(Path(self.td.name) / "nope.wav"), hf_token="tok")
        self.assertEqual(_FakePipeline.from_pretrained_count, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
