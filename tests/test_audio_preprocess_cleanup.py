"""cleanup_preprocess_files 必须能删掉 normalize_to_wav 的产物（不再二次拼 _16k）。"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.transcriber.audio_preprocess import cleanup_preprocess_files


class CleanupPreprocessFilesTest(unittest.TestCase):
    def test_deletes_16k_and_parts(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            wav = d / "lecture_16k.wav"
            part = d / "lecture_16k_part_000.wav"
            denoised = d / "lecture_16k_denoised.wav"
            source = d / "lecture.mp3"
            for f in (wav, part, denoised, source):
                f.write_bytes(b"x")
            cleanup_preprocess_files(str(wav))
            self.assertFalse(wav.exists())
            self.assertFalse(part.exists())
            self.assertFalse(denoised.exists())
            self.assertTrue(source.exists())

    def test_source_path_does_not_delete_original(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            src = d / "talk.wav"
            extra = d / "talk_16k.wav"
            src.write_bytes(b"s")
            extra.write_bytes(b"e")
            cleanup_preprocess_files(str(src))
            self.assertTrue(src.exists())
            self.assertFalse(extra.exists())
