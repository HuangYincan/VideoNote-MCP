"""app/utils/json_store 安全读写 + 三个配置管理器加固（docs/05 #106 扫描 1 号）。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.cookie_manager import CookieConfigManager
from app.services.proxy_config_manager import ProxyConfigManager
from app.services.transcriber_config_manager import TranscriberConfigManager
from app.utils.json_store import read_json, write_json_atomic


class JsonStoreReadTest(unittest.TestCase):
    def test_missing_returns_default(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(read_json(Path(td) / "nope.json"), {})
            self.assertEqual(read_json(Path(td) / "nope.json", {"a": 1}), {"a": 1})

    def test_valid_content(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "ok.json"
            p.write_text(json.dumps({"a": 1}), encoding="utf-8")
            self.assertEqual(read_json(p), {"a": 1})

    def test_corrupt_warns_and_backs_up(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cfg.json"
            p.write_text("{broken json", encoding="utf-8")
            with self.assertLogs("app.utils.json_store", level="WARNING") as logs:
                out = read_json(p)
            self.assertEqual(out, {})
            self.assertTrue(any("配置" in m for m in logs.output))
            # 损坏文件被移走保留（供排查），不再挡在配置读取路径上
            self.assertFalse(p.exists())
            self.assertTrue(Path(str(p) + ".corrupt").exists())

    def test_non_dict_root_warns(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "list.json"
            p.write_text("[1,2]", encoding="utf-8")
            with self.assertLogs("app.utils.json_store", level="WARNING") as logs:
                out = read_json(p)
            self.assertEqual(out, {})
            self.assertTrue(any("配置" in m for m in logs.output))


class JsonStoreWriteTest(unittest.TestCase):
    def test_atomic_write_content_and_mode(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "cfg.json"
            write_json_atomic(p, {"a": "中文", "b": [1]})
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": "中文", "b": [1]})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
            # 无 .tmp 残留
            self.assertFalse(Path(str(p) + ".tmp").exists())


class CookieManagerCorruptionTest(unittest.TestCase):
    """损坏的 downloader.json：曾静默返回 {}（cookie 悄悄消失），set() 更会把
    其它平台 cookie 永久抹掉且全程无日志（#106 最严重项）。"""

    def _mgr(self, td, content):
        p = Path(td) / "downloader.json"
        p.write_text(content, encoding="utf-8")
        return CookieConfigManager(filepath=str(p))

    def test_get_on_corrupt_warns_and_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = self._mgr(td, "{broken")
            with self.assertLogs("app.utils.json_store", level="WARNING") as logs:
                self.assertIsNone(mgr.get("bilibili"))
            self.assertTrue(any("配置" in m for m in logs.output))

    def test_set_on_corrupt_warns_and_backs_up(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = self._mgr(td, '{"bilibili": {"cookie": "SESS=old"}}'[:20])  # 截断 → 损坏
            with self.assertLogs("app.utils.json_store", level="WARNING"):
                mgr.set("youtube", "SESS=new")
            self.assertEqual(mgr.get("youtube"), "SESS=new")
            # 损坏原件被备份（其它平台 cookie 可从备份恢复，不再无声消失）
            self.assertTrue(Path(td, "downloader.json.corrupt").exists())

    def test_write_is_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = CookieConfigManager(filepath=str(Path(td) / "downloader.json"))
            mgr.set("bilibili", "SESS=x")
            self.assertEqual(mgr.get("bilibili"), "SESS=x")
            self.assertFalse(Path(td, "downloader.json.tmp").exists())


class TranscriberConfigCorruptionTest(unittest.TestCase):
    def test_corrupt_falls_back_to_env_with_warning(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "transcriber.json"
            p.write_text("{broken", encoding="utf-8")
            mgr = TranscriberConfigManager(filepath=str(p))
            with self.assertLogs("app.utils.json_store", level="WARNING"):
                cfg = mgr.get_config()
            self.assertEqual(cfg["transcriber_type"], "fast-whisper")  # env 默认，不炸
            self.assertTrue(Path(str(p) + ".corrupt").exists())


class ProxyConfigCorruptionTest(unittest.TestCase):
    def test_corrupt_falls_back_with_warning(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "proxy.json"
            p.write_text("{broken", encoding="utf-8")
            mgr = ProxyConfigManager(filepath=str(p))
            with self.assertLogs("app.utils.json_store", level="WARNING"):
                cfg = mgr.get_config()
            self.assertFalse(cfg["enabled"])
            self.assertTrue(Path(str(p) + ".corrupt").exists())


if __name__ == "__main__":
    unittest.main()
