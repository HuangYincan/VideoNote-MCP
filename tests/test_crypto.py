"""机器级 Fernet 加密（docs/05 #29）：往返、明文兼容、key 丢失回退。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from videonote_mcp import crypto


class CryptoTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._env = mock.patch.dict(os.environ, {"VIDEONOTE_CONFIG_DIR": self._td.name})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_roundtrip(self):
        enc = crypto.encrypt_value("sk-abc123")
        self.assertTrue(enc.startswith("enc:"))
        self.assertEqual(crypto.decrypt_value(enc), "sk-abc123")

    def test_plaintext_pass_through(self):
        # 明文兼容迁移：无前缀原样返回
        self.assertEqual(crypto.decrypt_value("sk-plain"), "sk-plain")
        self.assertIsNone(crypto.decrypt_value(None))
        self.assertEqual(crypto.decrypt_value(""), "")

    def test_empty_not_encrypted(self):
        self.assertEqual(crypto.encrypt_value(""), "")
        self.assertIsNone(crypto.encrypt_value(None))

    def test_key_missing_decrypt_fails(self):
        enc = crypto.encrypt_value("sk-abc123")
        # 删掉 key（模拟跨机器/被清理）→ 解密返回 None
        os.remove(Path(self._td.name) / "fernet.key")
        self.assertIsNone(crypto.decrypt_value(enc))

    def test_key_file_permissions(self):
        crypto.encrypt_value("sk-abc123")
        key_path = Path(self._td.name) / "fernet.key"
        self.assertTrue(key_path.exists())
        mode = key_path.stat().st_mode & 0o777
        self.assertLessEqual(mode, 0o600, "key 文件必须 0600 或更严")

    def test_key_reuse_across_values(self):
        a = crypto.encrypt_value("key-a")
        b = crypto.encrypt_value("key-b")
        self.assertEqual(crypto.decrypt_value(a), "key-a")
        self.assertEqual(crypto.decrypt_value(b), "key-b")


class AppConfigEncryptionTest(unittest.TestCase):
    """app_config.json 的 hf_token 落盘加密 + 读回解密（docs/05 #29）。"""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self._env = mock.patch.dict(os.environ, {"VIDEONOTE_CONFIG_DIR": self._td.name})
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_hf_token_encrypted_at_rest(self):
        import json

        from videonote_mcp.config import get_app_config, set_app_config

        set_app_config("hf_token", "hf_secret_123")
        set_app_config("notes_dir", "/tmp/notes")
        raw = json.loads(
            (Path(self._td.name) / "app_config.json").read_text(encoding="utf-8")
        )
        self.assertTrue(raw["hf_token"].startswith("enc:"), "hf_token 必须加密落盘")
        self.assertEqual(raw["notes_dir"], "/tmp/notes", "非敏感字段不加密")
        self.assertEqual(get_app_config()["hf_token"], "hf_secret_123", "读回解密")

    def test_plaintext_hf_token_still_reads(self):
        # 明文兼容：旧 app_config.json 直接写明文也能读
        import json

        from videonote_mcp.config import get_app_config

        (Path(self._td.name) / "app_config.json").write_text(
            json.dumps({"hf_token": "old-plain", "notes_dir": "/x"}),
            encoding="utf-8",
        )
        self.assertEqual(get_app_config()["hf_token"], "old-plain")


if __name__ == "__main__":
    unittest.main()
