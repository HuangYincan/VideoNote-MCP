"""inspect_video：分 P / 播放列表解析（mock 网络）。"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import inspect as inspect_mod

VIEW_MULTI = {
    "code": 0,
    "message": "0",
    "data": {
        "aid": 1,
        "bvid": "BV1xx411c7mD",
        "title": "系列课",
        "duration": 300,
        "cid": 100,
        "pages": [
            {"page": 1, "part": "第一讲", "duration": 120, "cid": 100},
            {"page": 2, "part": "第二讲", "duration": 180, "cid": 101},
        ],
    },
}

VIEW_SINGLE = {
    "code": 0,
    "data": {
        "aid": 2,
        "bvid": "BV1aa411c7mD",
        "title": "单集",
        "duration": 90,
        "cid": 200,
        "pages": [{"page": 1, "part": "单集", "duration": 90, "cid": 200}],
    },
}


class InspectBilibiliTest(unittest.TestCase):
    def test_multi_p_lists_urls(self):
        fake = mock.Mock()
        fake.json.return_value = VIEW_MULTI
        with mock.patch("app.services.inspect.public_get_retry", return_value=fake):
            out = inspect_mod.inspect_video("https://www.bilibili.com/video/BV1xx411c7mD?p=2")
        self.assertTrue(out["ok"])
        self.assertEqual(out["kind"], "multi")
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["current_p"], 2)
        self.assertEqual(out["title"], "系列课")
        self.assertEqual(
            [e["url"] for e in out["entries"]],
            [
                "https://www.bilibili.com/video/BV1xx411c7mD?p=1",
                "https://www.bilibili.com/video/BV1xx411c7mD?p=2",
            ],
        )
        self.assertEqual(out["entries"][1]["title"], "第二讲")

    def test_single_is_kind_single(self):
        fake = mock.Mock()
        fake.json.return_value = VIEW_SINGLE
        with mock.patch("app.services.inspect.public_get_retry", return_value=fake):
            out = inspect_mod.inspect_video("https://www.bilibili.com/video/BV1aa411c7mD")
        self.assertTrue(out["ok"])
        self.assertEqual(out["kind"], "single")
        self.assertEqual(out["total"], 1)
        self.assertEqual(out["entries"][0]["url"], "https://www.bilibili.com/video/BV1aa411c7mD?p=1")

    def test_view_error(self):
        fake = mock.Mock()
        fake.json.return_value = {"code": -404, "message": "啥都木有"}
        with mock.patch("app.services.inspect.public_get_retry", return_value=fake):
            out = inspect_mod.inspect_video("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertFalse(out["ok"])
        self.assertIn("-404", out["error"])

    def test_empty_url(self):
        out = inspect_mod.inspect_video("  ")
        self.assertFalse(out["ok"])


class InspectYtdlpTest(unittest.TestCase):
    def test_playlist(self):
        info = {
            "_type": "playlist",
            "id": "PLxxx",
            "title": "A playlist",
            "entries": [
                {"id": "aaaaaaaaaaa", "title": "ep1", "duration": 10, "webpage_url": None, "url": "aaaaaaaaaaa"},
                {"id": "bbbbbbbbbbb", "title": "ep2", "duration": 20, "webpage_url": "https://www.youtube.com/watch?v=bbbbbbbbbbb"},
            ],
        }

        class _YDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return info

        with mock.patch("yt_dlp.YoutubeDL", _YDL):
            out = inspect_mod.inspect_video("https://www.youtube.com/playlist?list=PLxxx")
        self.assertTrue(out["ok"])
        self.assertEqual(out["kind"], "multi")
        self.assertEqual(out["total"], 2)
        self.assertEqual(out["entries"][0]["url"], "https://www.youtube.com/watch?v=aaaaaaaaaaa")
        self.assertEqual(out["entries"][1]["title"], "ep2")

    def test_single_video(self):
        info = {
            "id": "ccccccccccc",
            "title": "one",
            "duration": 33,
            "webpage_url": "https://www.youtube.com/watch?v=ccccccccccc",
        }

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
            out = inspect_mod.inspect_video("https://youtu.be/ccccccccccc")
        self.assertTrue(out["ok"])
        self.assertEqual(out["kind"], "single")
        self.assertEqual(out["entries"][0]["video_id"], "ccccccccccc")

    def test_local_passthrough(self):
        from videonote_mcp.server import DATA_DIR

        p = DATA_DIR / "inspect_local_foo.mp4"
        p.write_bytes(b"fake")
        try:
            out = inspect_mod.inspect_video(str(p))
        finally:
            p.unlink(missing_ok=True)
        self.assertTrue(out["ok"])
        self.assertEqual(out["platform"], "local")
        self.assertEqual(out["kind"], "single")
        self.assertEqual(out["entries"][0]["url"], str(p))

    def test_local_missing_inside_data_dir_rejected(self):
        from videonote_mcp.server import DATA_DIR

        missing = DATA_DIR / "videonote_never_exists.mp4"
        out = inspect_mod.inspect_video(str(missing))
        self.assertFalse(out["ok"])
        self.assertEqual(out["platform"], "local")
        self.assertIn("不存在", out["error"])

    def test_local_outside_data_dir_rejected(self):
        out = inspect_mod.inspect_video("/tmp/videonote_never_exists.mp4")
        self.assertFalse(out["ok"])
        self.assertEqual(out["platform"], "local")
        self.assertIn("VIDEONOTE_ALLOW_EXTERNAL_PATHS", out["error"])


class InspectCookieInjectionTest(unittest.TestCase):
    """#122 B3：yt-dlp 路径 cookie 改用 http_headers 注入。

    旧实现写 Netscape 临时文件但域名绑死 .example.com、cookie 塞进 generic 字段，
    yt-dlp 永远不会带上；也不留临时文件。
    """

    def _capture_opts(self, info):
        captured = {}

        class _YDL:
            def __init__(self, opts):
                captured["opts"] = opts

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def extract_info(self, url, download=False):
                return info

        return captured, _YDL

    def test_cookie_injected_as_http_header(self):
        captured, ydl_cls = self._capture_opts(
            {"id": "c0000000000", "title": "t", "webpage_url": "https://www.youtube.com/watch?v=c0000000000"}
        )
        with mock.patch("yt_dlp.YoutubeDL", ydl_cls), mock.patch(
            "app.services.cookie_manager.CookieConfigManager.get", return_value="SESSDATA=abc"
        ):
            out = inspect_mod.inspect_video("https://www.youtube.com/watch?v=c0000000000")
        self.assertTrue(out["ok"])
        self.assertEqual(captured["opts"]["http_headers"], {"Cookie": "SESSDATA=abc"})
        self.assertNotIn("cookiefile", captured["opts"])  # 不再依赖 Netscape 临时文件

    def test_no_cookie_no_http_headers(self):
        captured, ydl_cls = self._capture_opts(
            {"id": "c0000000001", "title": "t", "webpage_url": "https://www.youtube.com/watch?v=c0000000001"}
        )
        with mock.patch("yt_dlp.YoutubeDL", ydl_cls), mock.patch(
            "app.services.cookie_manager.CookieConfigManager.get", return_value=""
        ):
            out = inspect_mod.inspect_video("https://www.youtube.com/watch?v=c0000000001")
        self.assertTrue(out["ok"])
        self.assertNotIn("http_headers", captured["opts"])
        self.assertNotIn("cookiefile", captured["opts"])


if __name__ == "__main__":
    unittest.main()
