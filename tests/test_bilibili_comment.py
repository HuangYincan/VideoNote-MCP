"""
BilibiliCommentFetcher 测试（mock requests.get，不碰真实网络）。

运行（仓库根目录）：
    PYTHONPATH=. VIDEONOTE_CONFIG_DIR=/tmp/bn_test_cfg VIDEONOTE_DATA_DIR=/tmp/bn_test_data \
    .venv/bin/python tests/test_bilibili_comment.py
"""

import json
import os
import sys
import threading
import unittest
from unittest.mock import patch

# 测试配置落到临时目录，避免污染仓库 config/ 与 logs/
os.environ.setdefault("VIDEONOTE_CONFIG_DIR", "/tmp/bn_test_cfg")
os.environ.setdefault("VIDEONOTE_DATA_DIR", "/tmp/bn_test_data")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.downloaders.bilibili_comment import BilibiliCommentFetcher  # noqa: E402
from app.exceptions.task import TaskCancelledError  # noqa: E402

VIDEO_URL = "https://www.bilibili.com/video/BV1xx411c7mD"
VIDEO_URL_P2 = "https://www.bilibili.com/video/BV1xx411c7mD?p=2"

# ---------- 固定 mock 数据 ----------

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

# 8 条弹幕：窗口 0-30 有 3 条，30-60 2 条，60-90 2 条，90-120 1 条
DANMAKU_XML = (
    "<?xml version=\"1.0\" encoding=\"UTF-8\"?><i>"
    "<chatserver>chat.bilibili.com</chatserver><chatid>67890</chatid>"
    "<d p=\"0.5,1,25,16777215,1700000000,0,1,1\">前方高能</d>"
    "<d p=\"10.2,1,25,16777215,1700000000,0,2,2\">哈哈哈哈</d>"
    "<d p=\"20.7,1,25,16777215,1700000000,0,3,3\">前方高能</d>"
    "<d p=\"31.0,1,25,16777215,1700000000,0,4,4\">yyds</d>"
    "<d p=\"45.0,1,25,16777215,1700000000,0,5,5\">23333</d>"
    "<d p=\"60.0,1,25,16777215,1700000000,0,6,6\">经典永流传</d>"
    "<d p=\"62.0,1,25,16777215,1700000000,0,7,7\">经典永流传</d>"
    "<d p=\"90.5,1,25,16777215,1700000000,0,8,8\">妙啊</d>"
    "</i>"
).encode("utf-8")

EMPTY_XML = b"<i><chatserver>chat.bilibili.com</chatserver></i>"
CORRUPT_XML = b"<<< not xml at all"
CORRUPT_XML2 = "<i><d p=\"1.0,1,25,16777215\">半截<d>".encode("utf-8")

_PAGE0_LIKES = {1: 3, 2: 9, 3: 1, 4: 7, 5: 5, 6: 10, 7: 2, 8: 8, 9: 4, 10: 6}
_PAGE1_NEW_LIKES = {11: 20, 12: 18, 13: 16, 14: 14, 15: 12, 16: 11, 17: 13, 18: 15, 19: 17, 20: 19}


def _reply(rpid, likes, ctime_off):
    return {
        "rpid": rpid,
        "member": {"uname": f"用户{rpid}"},
        "content": {"message": f"内容{rpid}"},
        "like": likes,
        "ctime": 1700000000 + ctime_off,
    }


def _reply_page(entries, next_page):
    replies = [_reply(rpid, likes, ctime_off) for rpid, likes, ctime_off in entries]
    cursor = {"next": next_page} if next_page is not None else {}
    return {"code": 0, "message": "0", "data": {"replies": replies, "cursor": cursor}}


REPLY_PAGE0 = _reply_page(
    [(rpid, likes, rpid) for rpid, likes in _PAGE0_LIKES.items()],
    next_page=1,
)

# 第 2 页：10 条新评论 + 5 条与第 1 页重复（rpid 6-10，likes 故意给很大，验证按 rpid 去重）
REPLY_PAGE1 = _reply_page(
    [(rpid, likes, rpid) for rpid, likes in _PAGE1_NEW_LIKES.items()]
    + [(rpid, 999 + rpid, rpid) for rpid in (6, 7, 8, 9, 10)],
    next_page=None,
)

REPLY_EMPTY = {"code": 0, "message": "0", "data": {"replies": [], "cursor": {}}}
REPLY_ERROR = {"code": -400, "message": "请求错误", "data": None}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def content(self):
        if isinstance(self._payload, (bytes, bytearray)):
            return bytes(self._payload)
        return json.dumps(self._payload).encode("utf-8")


def _make_fake_get(reply_pages=None, dm_content=DANMAKU_XML):
    """构造 requests.get 的 side_effect。reply_pages: {next参数: page_json}。"""
    reply_pages = reply_pages if reply_pages is not None else {0: REPLY_PAGE0, 1: REPLY_PAGE1}
    calls = []

    def fake_get(url, params=None, headers=None, timeout=None, **kwargs):
        params = params or {}
        headers = headers or {}
        calls.append({"url": url, "params": params, "headers": headers})
        if "x/web-interface/view" in url:
            if params.get("p") == 2:
                return FakeResponse(VIEW_JSON)
            return FakeResponse(VIEW_JSON)
        if "dm/list.so" in url:
            return FakeResponse(dm_content)
        if "x/v2/reply/main" in url:
            nxt = params.get("next")
            if nxt in reply_pages:
                return FakeResponse(reply_pages[nxt])
            raise AssertionError(f"unexpected reply page: {nxt}")
        raise AssertionError(f"unexpected url: {url}")

    return fake_get, calls


class BilibiliCommentFetcherTest(unittest.TestCase):
    def setUp(self):
        self.fetcher = BilibiliCommentFetcher()
        self.fetcher._cookie = ""  # 无 cookie，验证不崩

    # ---------- 弹幕 ----------

    def test_parse_danmaku_xml_and_window(self):
        items = self.fetcher._parse_danmaku_xml(DANMAKU_XML.decode("utf-8"))
        self.assertEqual(len(items), 8)
        summary = self.fetcher._build_danmaku_summary(items)
        self.assertIn("弹幕高密度时段", summary)
        self.assertIn("00:00-00:30(3条)", summary)
        self.assertIn("00:30-01:00(2条)", summary)
        self.assertIn("01:30-02:00(1条)", summary)
        self.assertIn("高频弹幕", summary)
        self.assertIn("前方高能", summary)
        # 高频词按出现次数排序，前两个都是出现 2 次
        kw_part = summary.split("高频弹幕：", 1)[1]
        kws = [k.strip() for k in kw_part.split("、") if k.strip()]
        self.assertGreaterEqual(len(kws), 2)
        self.assertEqual(kws[0], "前方高能")

    def test_fetch_danmaku_ok(self):
        fake_get, calls = _make_fake_get()
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=fake_get):
            res = self.fetcher.fetch_danmaku(VIDEO_URL)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["source"], "bilibili")
        self.assertEqual(res["bvid"], "BV1xx411c7mD")
        self.assertEqual(res["cid"], 67890)
        self.assertIn("弹幕高密度时段", res["danmaku_summary"])
        self.assertIn("高频弹幕", res["danmaku_summary"])
        self.assertIsNone(res["error"])
        # 请求顺序：view → dm
        self.assertIn("x/web-interface/view", calls[0]["url"])
        self.assertIn("dm/list.so", calls[1]["url"])

    def test_fetch_danmaku_p2(self):
        """分 P 视频：?p=2 应取第 2 集的 cid。"""
        fake_get, calls = _make_fake_get()
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=fake_get):
            res = self.fetcher.fetch_danmaku(VIDEO_URL_P2)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["cid"], 67891)
        # view 请求带 p=2
        self.assertEqual(calls[0]["params"].get("p"), 2)
        # dm 请求用第 2 集 cid
        self.assertEqual(calls[1]["params"].get("oid"), 67891)

    def test_fetch_danmaku_empty(self):
        fake_get, _ = _make_fake_get(dm_content=EMPTY_XML)
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=fake_get):
            res = self.fetcher.fetch_danmaku(VIDEO_URL)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["danmaku_summary"], "")

    def test_fetch_danmaku_corrupt_xml(self):
        for bad in (CORRUPT_XML, CORRUPT_XML2):
            fake_get, _ = _make_fake_get(dm_content=bad)
            with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=fake_get):
                res = self.fetcher.fetch_danmaku(VIDEO_URL)
            self.assertFalse(res["ok"], res)
            self.assertIn("弹幕 XML 解析失败", res["error"])
            self.assertIsNotNone(res["error"])

    def test_parse_danmaku_rejects_dtd_and_entities(self):
        """#142 A3：弹幕 XML 来自网络（不可信输入）——DTD/实体声明必须在解析前被拒绝，
        否则恶意响应可 XXE（读本地文件/内网探测）或实体扩展 DoS。"""
        evil_dtd = (
            '<?xml version="1.0"?><!DOCTYPE i [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            '<i><d p="1.0,1,25,16777215">&xxe;</d></i>'
        )
        with self.assertRaises(ValueError):
            self.fetcher._parse_danmaku_xml(evil_dtd)
        evil_entity = (
            '<?xml version="1.0"?><!DOCTYPE i [<!ENTITY a "aaaaaaaaaaaaaa">]>'
            '<i><d p="1.0,1,25,16777215">&a;</d></i>'
        )
        with self.assertRaises(ValueError):
            self.fetcher._parse_danmaku_xml(evil_entity)
        # 大小写变体也拦（<!Entity / <!Doctype）
        with self.assertRaises(ValueError):
            self.fetcher._parse_danmaku_xml(
                '<?xml version="1.0"?><!ENTITY xx "y"><i><d p="1.0,1,25,16777215">z</d></i>'
            )

    def test_fetch_danmaku_oversize_rejected(self):
        """#142 A3：响应体积上限（decode 前检查）——超限拒绝解析而不是先拉进内存。"""
        from app.downloaders.bilibili_comment import DANMAKU_MAX_XML_BYTES

        oversize = b"x" * (DANMAKU_MAX_XML_BYTES + 1)
        fake_get, _ = _make_fake_get(dm_content=oversize)
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=fake_get):
            res = self.fetcher.fetch_danmaku(VIDEO_URL)
        self.assertFalse(res["ok"], res)
        self.assertIn("过大", res["error"])
        self.assertEqual(res["danmaku_summary"], "")

    # ---------- 评论 ----------

    def test_fetch_comments_ok_dedup_sort(self):
        fake_get, calls = _make_fake_get()
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=fake_get):
            res = self.fetcher.fetch_comments(VIDEO_URL, limit=100)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["source"], "bilibili")
        self.assertEqual(res["bvid"], "BV1xx411c7mD")
        self.assertEqual(res["aid"], 12345)
        # 拉了两页 25 条原始，去重后 20 条（rpid 6-10 的重复项被丢弃，即使 likes 更大）
        self.assertEqual(len(res["comments"]), 20)
        # 按 likes 降序
        likes = [c["likes"] for c in res["comments"]]
        self.assertEqual(likes, sorted(likes, reverse=True))
        self.assertEqual(likes[0], 20)  # rpid=11
        # 字段正确
        c6 = next(c for c in res["comments"] if c["user"] == "用户6")
        self.assertEqual(c6["content"], "内容6")
        self.assertEqual(c6["likes"], 10)
        self.assertEqual(c6["ctime"], 1700000000 + 6)
        # 翻了两页（view + reply + reply）
        self.assertEqual(sum(1 for c in calls if "x/v2/reply/main" in c["url"]), 2)
        self.assertIsNone(res["error"])

    def test_fetch_comments_limit(self):
        fake_get, _ = _make_fake_get()
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=fake_get):
            res = self.fetcher.fetch_comments(VIDEO_URL, limit=5)
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(res["comments"]), 5)
        # 第 1 页按 likes 最高的 5 条：用户6(10), 用户2(9), 用户8(8), 用户4(7), 用户10(6)
        top_users = [c["user"] for c in res["comments"]]
        self.assertEqual(top_users, ["用户6", "用户2", "用户8", "用户4", "用户10"])

    def test_fetch_comments_empty(self):
        fake_get, _ = _make_fake_get(reply_pages={0: REPLY_EMPTY})
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=fake_get):
            res = self.fetcher.fetch_comments(VIDEO_URL)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["comments"], [])

    def test_fetch_comments_api_error(self):
        fake_get, _ = _make_fake_get(reply_pages={0: REPLY_ERROR})
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=fake_get):
            res = self.fetcher.fetch_comments(VIDEO_URL)
        self.assertFalse(res["ok"], res)
        self.assertEqual(res["comments"], [])
        self.assertIn("请求错误", res["error"])

    # ---------- 通用 ----------

    def test_bad_url(self):
        with patch("app.downloaders.bilibili_comment.public_get_retry") as m:
            res = self.fetcher.fetch_danmaku("https://example.com/not-a-bili-video")
        self.assertFalse(res["ok"])
        self.assertIn("无法从 URL 提取 BV id", res["error"])
        m.assert_not_called()  # 解析失败不应发任何请求

    def test_no_cookie_no_crash_and_no_cookie_header(self):
        """无 cookie 时请求正常，且所有请求头都不带 Cookie。"""
        fake_get, calls = _make_fake_get()
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=fake_get) as m:
            d = self.fetcher.fetch_danmaku(VIDEO_URL)
            c = self.fetcher.fetch_comments(VIDEO_URL)
        self.assertTrue(d["ok"], d)
        self.assertTrue(c["ok"], c)
        for call in m.call_args_list:
            headers = call.kwargs.get("headers") or {}
            self.assertNotIn("Cookie", headers)

    def test_network_exception(self):
        # view 请求断网 → _get_meta 兜底返回 None，error 为通用提示
        def boom_view(url, params=None, headers=None, timeout=None, **kwargs):
            raise ConnectionError("network down")

        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=boom_view):
            d = self.fetcher.fetch_danmaku(VIDEO_URL)
        self.assertFalse(d["ok"])
        self.assertIn("获取视频元信息失败", d["error"])

        # view 正常、dm/reply 断网 → error 透传异常信息
        def boom_dm(url, params=None, headers=None, timeout=None, **kwargs):
            if "x/web-interface/view" in url:
                return FakeResponse(VIEW_JSON)
            raise ConnectionError("network down")

        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=boom_dm):
            d = self.fetcher.fetch_danmaku(VIDEO_URL)
            c = self.fetcher.fetch_comments(VIDEO_URL)
        self.assertFalse(d["ok"])
        self.assertIn("network down", d["error"])
        self.assertFalse(c["ok"])
        self.assertIn("network down", c["error"])


    def test_p_out_of_range_returns_none(self):
        # 显式 p 越界（VIEW_JSON 只有 2 集）：返回 None 而非静默取第 1 集——
        # 调用方以为拿到了第 p 集的评论，实际是第 1 集内容（#121 B4）
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=_make_fake_get()[0]):
            meta = self.fetcher._get_meta("BV1xx411c7mD", p=3)
        self.assertIsNone(meta)

    def test_p_within_range_resolves(self):
        with patch("app.downloaders.bilibili_comment.public_get_retry", side_effect=_make_fake_get()[0]):
            meta = self.fetcher._get_meta("BV1xx411c7mD", p=2)
        self.assertEqual(meta, (12345, 67891))  # pages[1] 的 cid

    def test_cancel_skips_network(self):
        event = threading.Event()
        event.set()
        with patch("app.downloaders.bilibili_comment.public_get_retry") as m:
            with self.assertRaises(TaskCancelledError):
                self.fetcher.fetch_danmaku(VIDEO_URL, cancel_event=event)
            with self.assertRaises(TaskCancelledError):
                self.fetcher.fetch_comments(VIDEO_URL, cancel_event=event)
        m.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
