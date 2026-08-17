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
class WriteTextAtomicTest(unittest.TestCase):
    """write_text_atomic：tmp + replace，写盘中断不留半截文件（#124 B13）。"""

    def test_writes_content_and_cleans_tmp(self):
        import tempfile

        from app.utils.json_store import write_text_atomic

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "note.md"
            write_text_atomic(p, "# 标题\n正文")
            self.assertEqual(p.read_text(encoding="utf-8"), "# 标题\n正文")
            self.assertFalse(Path(str(p) + ".tmp").exists())
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_overwrites_existing(self):
        import tempfile

        from app.utils.json_store import write_text_atomic

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "note.md"
            p.write_text("旧", encoding="utf-8")
            write_text_atomic(p, "新内容")
            self.assertEqual(p.read_text(encoding="utf-8"), "新内容")

    def test_nested_dir_created(self):
        import tempfile

        from app.utils.json_store import write_text_atomic

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a" / "b" / "note.md"
            write_text_atomic(p, "x")
            self.assertTrue(p.exists())


class CookieConcurrentWriteTest(unittest.TestCase):
    """并发 set 不同平台互不覆盖（#124 B15）：读-改-写区间加锁。"""

    def test_concurrent_sets_all_survive(self):
        import tempfile
        import threading

        from app.services.cookie_manager import CookieConfigManager

        with tempfile.TemporaryDirectory() as td:
            cm = CookieConfigManager(str(Path(td) / "downloader.json"))

            def _set(i):
                for _ in range(3):
                    cm.set(f"p{i}", f"cookie-{i}")

            threads = [threading.Thread(target=_set, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            got = cm.list_all()
            self.assertEqual(len(got), 20)  # 无锁时读-改-写竞态会互相覆盖
            for i in range(20):
                self.assertEqual(got[f"p{i}"], f"cookie-{i}")

    def test_lock_is_class_level(self):
        from app.services.cookie_manager import CookieConfigManager

        # class-level 锁：多实例并发也互斥（每个 NoteGenerator 各持一个实例）
        self.assertIsInstance(CookieConfigManager._lock, type(__import__("threading").RLock()))

if __name__ == "__main__":
    unittest.main()
