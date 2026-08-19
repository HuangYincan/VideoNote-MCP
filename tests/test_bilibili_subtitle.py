"""BilibiliSubtitleFetcher 的 p 越界处理（#121 B4）：显式 p 越界返回 None。

不碰真实网络：mock requests.get 返回构造的 view API 响应。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.downloaders.bilibili_subtitle import BilibiliSubtitleFetcher

VIEW_JSON = {
    "code": 0,
    "message": "0",
    "data": {
        "aid": 12345,
        "cid": 67890,
        "pages": [
            {"cid": 67890, "page": 1},
            {"cid": 67891, "page": 2},
        ],
    },
}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _fake_get(url, params=None, headers=None, timeout=None):
    if "x/web-interface/view" in url:
        return _FakeResp(VIEW_JSON)
    raise AssertionError(f"unexpected url: {url}")


class SubtitlePOutOfRangeTest(unittest.TestCase):
    def setUp(self):
        self.fetcher = BilibiliSubtitleFetcher()

    def test_p_out_of_range_returns_none(self):
        # p=3 但只有 2 集：返回 None（上层走语音转写），绝不静默取第 1 集字幕
        with mock.patch("app.downloaders.bilibili_subtitle.requests.get", side_effect=_fake_get):
            cid = self.fetcher._get_cid("BV1xx411c7mD", p=3)
        self.assertIsNone(cid)

    def test_p_within_range_resolves(self):
        with mock.patch("app.downloaders.bilibili_subtitle.requests.get", side_effect=_fake_get):
            cid = self.fetcher._get_cid("BV1xx411c7mD", p=2)
        self.assertEqual(cid, 67891)  # pages[1]

    def test_no_p_defaults_to_first_page(self):
        # 没给 p（或 p<1）：默认第 1 集，行为不变
        with mock.patch("app.downloaders.bilibili_subtitle.requests.get", side_effect=_fake_get):
            cid = self.fetcher._get_cid("BV1xx411c7mD")
        self.assertEqual(cid, 67890)


if __name__ == "__main__":
    unittest.main()
