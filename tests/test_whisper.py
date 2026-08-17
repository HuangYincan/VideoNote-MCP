"""whisper 模型加载失败处理（#124 B18）：只对 cache 损坏类异常 purge，其余不删。

不碰真实模型下载/加载——只测异常分类与「本地路径模型绝不删除」的守卫。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class IsCacheErrorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app.transcriber.whisper import WhisperTranscriber

        cls.wt = WhisperTranscriber

    def _local_entry_error(self):
        from huggingface_hub.utils import LocalEntryNotFoundError

        return LocalEntryNotFoundError("快照不完整")

    def test_local_entry_not_found_is_cache_error(self):
        self.assertTrue(self.wt._is_cache_error(self._local_entry_error()))

    def test_oserror_is_cache_error(self):
        self.assertTrue(self.wt._is_cache_error(OSError("磁盘 IO 错误")))

    def test_network_errors_not_cache_error(self):
        """网络瞬时故障不 purge：删掉只丢可断点续传的半截下载。"""
        for exc in (ConnectionError("down"), TimeoutError("slow")):
            self.assertFalse(self.wt._is_cache_error(exc))

    def test_value_error_not_cache_error(self):
        self.assertFalse(self.wt._is_cache_error(ValueError("bad size")))

    def test_entry_not_found_404_not_cache_error(self):
        """repo 404 不 purge：模型名拼错与本地 cache 无关。"""
        from huggingface_hub.utils import EntryNotFoundError

        self.assertFalse(self.wt._is_cache_error(EntryNotFoundError("no repo")))


class PurgeGuardTest(unittest.TestCase):
    """本地路径模型加载失败绝不删用户文件（_purge_cache 既有守卫，B18 重测）。"""

    @classmethod
    def setUpClass(cls):
        from app.transcriber.whisper import WhisperTranscriber

        cls.wt = WhisperTranscriber

    def test_local_target_never_deleted(self):
        import tempfile

        from app.transcriber.whisper_models import is_local_target

        with tempfile.TemporaryDirectory() as td:
            local_model = str(Path(td) / "my-model-dir")
            Path(local_model).mkdir()
            marker = Path(local_model) / "model.bin"
            marker.write_bytes(b"user-file")
            with mock.patch("app.transcriber.whisper.resolve_whisper_model", return_value=local_model), \
                 mock.patch("app.transcriber.whisper.is_local_target", side_effect=is_local_target):
                self.wt._purge_cache(td, "local-custom")
            # 用户文件原封不动（旧实现会 rmtree 掉整目录）
            self.assertTrue(marker.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
