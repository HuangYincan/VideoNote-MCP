"""机器级 Fernet 加密（docs/05 #29）：往返、明文兼容、加密失败 fail-closed。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
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
        self._encryption_status = mock.patch.object(crypto, "_encryption_failed", False)
        self._encryption_status.start()
        self.addCleanup(self._encryption_status.stop)

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

    def test_encrypt_status_defaults_fernet(self):
        self.assertEqual(crypto.encrypt_status(), "fernet")

    def test_key_creation_failure_raises_and_marks_status(self):
        """Fernet key 创建失败必须拒绝写入，不能回退明文。"""
        with mock.patch.object(
            crypto.os, "open", side_effect=PermissionError("read-only")
        ), self.assertRaises(crypto.EncryptionError):
            crypto.encrypt_value("sk-ro")
        self.assertEqual(crypto.encrypt_status(), "encryption-error")
        self.assertFalse((Path(self._td.name) / "fernet.key").exists())

    def test_invalid_existing_key_fails_closed(self):
        """已有损坏 key 时不能新建替代 key，也不能返回明文。"""
        key_path = Path(self._td.name) / "fernet.key"
        key_path.write_bytes(b"invalid-key")
        with self.assertRaises(crypto.EncryptionError):
            crypto.encrypt_value("sk-invalid-key")
        self.assertEqual(key_path.read_bytes(), b"invalid-key")

    def test_encrypt_exception_raises_and_marks_status(self):
        """Fernet 加密自身异常必须抛出，不能返回明文。"""
        with mock.patch(
            "cryptography.fernet.Fernet.encrypt", side_effect=Exception("boom")
        ), self.assertRaises(crypto.EncryptionError):
            crypto.encrypt_value("sk-x")
        self.assertEqual(crypto.encrypt_status(), "encryption-error")


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

    def test_hf_token_encrypt_failure_preserves_existing_file(self):
        """敏感配置加密失败时，原配置必须保持不变且不执行原子写。"""
        import json

        from videonote_mcp.config import get_app_config, set_app_config

        set_app_config("hf_token", "old-secret")
        set_app_config("notes_dir", "/old")
        config_path = Path(self._td.name) / "app_config.json"
        before = config_path.read_bytes()

        with mock.patch(
            "videonote_mcp.config.encrypt_value",
            side_effect=crypto.EncryptionError("encryption unavailable"),
        ), self.assertRaises(crypto.EncryptionError):
            set_app_config("hf_token", "new-secret")

        self.assertEqual(config_path.read_bytes(), before)
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertTrue(raw["hf_token"].startswith("enc:"))
        self.assertEqual(get_app_config(), {"hf_token": "old-secret", "notes_dir": "/old"})


class ProviderDaoEncryptionTest(unittest.TestCase):
    def _db(self, row=None):
        db = mock.Mock()
        db.query.return_value.filter_by.return_value.first.return_value = row
        return db

    def test_insert_encryption_failure_does_not_add_or_commit(self):
        from app.db.provider_dao import insert_provider

        db = self._db()
        with mock.patch("app.db.provider_dao.get_db", return_value=iter([db])), mock.patch(
                "videonote_mcp.crypto.encrypt_value",
                side_effect=crypto.EncryptionError("encryption unavailable"),
        ), self.assertRaises(crypto.EncryptionError):
            insert_provider("id", "name", "secret", "https://example.com", "logo", "custom")

        db.add.assert_not_called()
        db.commit.assert_not_called()
        db.rollback.assert_called_once()
        db.close.assert_called_once()

    def test_update_encryption_failure_does_not_mutate_or_commit(self):
        from app.db.provider_dao import update_provider

        row = SimpleNamespace(name="old-name", api_key="old-secret")
        db = self._db(row)
        with mock.patch("app.db.provider_dao.get_db", return_value=iter([db])), mock.patch(
                "videonote_mcp.crypto.encrypt_value",
                side_effect=crypto.EncryptionError("encryption unavailable"),
        ), self.assertRaises(crypto.EncryptionError):
            update_provider("id", name="new-name", api_key="new-secret")

        self.assertEqual(row.name, "old-name")
        db.commit.assert_not_called()
        db.rollback.assert_called_once()
        db.close.assert_called_once()

    def test_update_unreadable_encrypted_key_aborts_entire_update(self):
        from app.db.provider_dao import update_provider

        row = SimpleNamespace(name="old-name", api_key="old-secret")
        db = self._db(row)
        with mock.patch("app.db.provider_dao.get_db", return_value=iter([db])), mock.patch(
            "videonote_mcp.crypto.decrypt_value", return_value=None
        ), self.assertRaises(crypto.EncryptionError):
            update_provider("id", name="new-name", api_key="enc:unreadable")

        self.assertEqual(row.name, "old-name")
        db.commit.assert_not_called()
        db.rollback.assert_called_once()
        db.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
