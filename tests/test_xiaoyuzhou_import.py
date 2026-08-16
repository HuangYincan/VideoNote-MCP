"""xiaoyuzhou 模块 import 不得打网。"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class XiaoyuzhouImportTest(unittest.TestCase):
    def test_import_does_not_http(self):
        with mock.patch("requests.get") as get:
            import app.downloaders.xiaoyuzhoufm_download as mod

            get.assert_not_called()
            with self.assertRaises(NotImplementedError):
                mod.Xiaoyuzhoufm_download().download("https://www.xiaoyuzhoufm.com/episode/x")
