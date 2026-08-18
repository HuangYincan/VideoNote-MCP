"""app 层静默降级加固（docs/05 #106 扫描 2/3/7 号）：模型名解析、分块时长探测、播放列表坏条目。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import inspect as inspect_mod
from app.services import pipeline as pipeline_mod
from app.utils.model_status import check_whisper_model_exists


class ModelStatusResolveTest(unittest.TestCase):
    """模型名无法解析时曾静默返回 False——门禁谎报「模型未下载」让用户反复重下（#106-2）。"""

    def test_unresolvable_size_warns_and_returns_false(self):
        with self.assertLogs("app.utils.model_status", level="WARNING") as logs:
            ok = check_whisper_model_exists("bogus-size")
        self.assertFalse(ok)
        self.assertTrue(any("无法解析" in m for m in logs.output))

    def test_other_resolve_error_warns(self):
        with mock.patch(
            "app.transcriber.whisper_models._registry.resolve", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("app.utils.model_status", level="WARNING") as logs:
                ok = check_whisper_model_exists("tiny")
        self.assertFalse(ok)
        self.assertTrue(any("失败" in m and "boom" in m for m in logs.output))


class ChunkDurationGuessTest(unittest.TestCase):
    """probe_duration 失败曾静默回退 1800s——分段时间轴逐块漂移且任务照常 SUCCESS（#106-3）。"""

    def test_probe_failure_warns_and_falls_back(self):
        with mock.patch(
            "app.transcriber.audio_preprocess.probe_duration", side_effect=RuntimeError("no ffprobe")
        ):
            with self.assertLogs("app.services.pipeline", level="WARNING") as logs:
                d = pipeline_mod.chunk_duration_guess("/tmp/x.wav")
        self.assertEqual(d, 1800.0)
        self.assertTrue(any("回退 1800s" in m for m in logs.output))

    def test_probe_zero_warns_and_falls_back(self):
        with mock.patch("app.transcriber.audio_preprocess.probe_duration", return_value=0.0):
            with self.assertLogs("app.services.pipeline", level="WARNING") as logs:
                d = pipeline_mod.chunk_duration_guess("/tmp/x.wav")
        self.assertEqual(d, 1800.0)
        self.assertTrue(any("回退 1800s" in m for m in logs.output))

    def test_probe_value_passes_through(self):
        with mock.patch("app.transcriber.audio_preprocess.probe_duration", return_value=123.5), mock.patch.object(
            pipeline_mod.logger, "warning"
        ) as w:
            d = pipeline_mod.chunk_duration_guess("/tmp/x.wav")
        self.assertEqual(d, 123.5)
        self.assertFalse(any("回退 1800s" in str(c) for c in w.call_args_list))


class InspectPlaylistBadEntryTest(unittest.TestCase):
    """播放列表坏条目曾以成功形状返回——Agent 拿无效 URL 去下载阶段才失败（#106-7）。"""

    @staticmethod
    def _run(info):
        class _YDL:
            def __init__(self, opts):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return info

        with mock.patch("yt_dlp.YoutubeDL", _YDL):
            return inspect_mod.inspect_video("https://example.com/playlist")

    def test_bad_entry_skipped_with_warning(self):
        info = {
            "_type": "playlist",
            "id": "PLx",
            "title": "pl",
            "entries": [
                {"id": "e1", "title": "ok", "webpage_url": "https://a.com/v1"},
                {"id": "e2", "title": "bad", "webpage_url": None, "url": "relative/path"},
            ],
        }
        with self.assertLogs("app.services.inspect", level="WARNING") as logs:
            out = self._run(info)
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["entries"]), 1)
        self.assertEqual(out["entries"][0]["url"], "https://a.com/v1")
        self.assertTrue(any("已跳过" in m for m in logs.output))

    def test_all_bad_returns_error(self):
        info = {
            "_type": "playlist",
            "id": "PLx",
            "title": "pl",
            "entries": [
                {"id": "e1", "title": "bad", "webpage_url": None, "url": "rel/1"},
                {"id": "e2", "title": "bad", "webpage_url": "not-a-url"},
            ],
        }
        with self.assertLogs("app.services.inspect", level="WARNING"):
            out = self._run(info)
        self.assertFalse(out["ok"])
        self.assertIn("无可用条目", out["error"])


class WhisperCustomJsonReadTest(unittest.TestCase):
    """自定义 whisper 模型走手工编辑 JSON 的读路径（#134：写 API 已删为死代码）。

    resolve / visible_model_names 仍读 config/whisper_models.json 的自定义登记
    （手工编辑即可生效）；坏 JSON 按空处理不静默崩。
    """

    def test_handwritten_custom_json_honored_by_resolve(self):
        from app.transcriber.whisper_models import WhisperModelRegistry

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "whisper_models.json"
            cfg.write_text(json.dumps({"my-model": "local/models/ct2"}), encoding="utf-8")
            reg = WhisperModelRegistry(filepath=str(cfg))
            self.assertEqual(reg.resolve("my-model"), "local/models/ct2")
            self.assertIn("my-model", reg.visible_model_names())

    def test_bad_custom_json_falls_back_to_empty(self):
        from app.transcriber.whisper_models import WhisperModelRegistry

        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "whisper_models.json"
            cfg.write_text("{broken", encoding="utf-8")
            reg = WhisperModelRegistry(filepath=str(cfg))
            with self.assertLogs("app.transcriber.whisper_models", level="WARNING"):
                self.assertNotIn("whatever", reg.visible_model_names())
            # resolve 不受坏自定义 JSON 影响（回到内置档）
            self.assertTrue(reg.resolve("small").startswith("Systran/"))


if __name__ == "__main__":
    unittest.main()
