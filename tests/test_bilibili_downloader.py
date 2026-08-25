"""BilibiliDownloader 的缓存匹配与字幕回退清理（#121 B5/B6）。

不碰真实网络/yt-dlp：mock BilibiliSubtitleFetcher 与 yt_dlp.YoutubeDL。
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.downloaders.bilibili_downloader import BilibiliDownloader


class _YdlCtx:
    def __init__(self, info):
        self._info = info

    def extract_info(self, *a, **k):
        return self._info

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class CacheGlobExactMatchTest(unittest.TestCase):
    """缓存 glob 精确匹配：{BV}.mp4 或 {BV}_pN.mp4，不做前缀误配（#121 B5）。"""

    def setUp(self):
        patcher = mock.patch.object(
            BilibiliDownloader, "_write_netscape_cookie_file", return_value=None
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dl = BilibiliDownloader()

    def test_prefix_of_other_video_does_not_hit_cache(self):
        # 目录里只有 BV12345_p1.mp4（另一视频）：查询 BV1234 不得命中 → 走下载
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "BV12345_p1.mp4").write_bytes(b"x")
            with mock.patch(
                "app.downloaders.bilibili_downloader.yt_dlp.YoutubeDL",
                return_value=_YdlCtx({"id": "BV1234"}),
            ) as m_ydl:
                with self.assertRaises(FileNotFoundError):
                    self.dl.download_video("https://www.bilibili.com/video/BV1234", output_dir=tmp)
            m_ydl.assert_called_once()  # 缓存未命中 → 真的去下载

    def test_multip_video_hits_exact_cache(self):
        # 同视频分 P 缓存 {BV}_pN.mp4：精确命中，不再下载
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "BV1234_p1.mp4").write_bytes(b"x")
            with mock.patch("app.downloaders.bilibili_downloader.yt_dlp.YoutubeDL") as m_ydl:
                got = self.dl.download_video("https://www.bilibili.com/video/BV1234", output_dir=tmp)
            self.assertEqual(got, str(Path(tmp) / "BV1234_p1.mp4"))
            m_ydl.assert_not_called()

    def test_single_video_hits_exact_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "BV1234.mp4").write_bytes(b"x")
            with mock.patch("app.downloaders.bilibili_downloader.yt_dlp.YoutubeDL") as m_ydl:
                got = self.dl.download_video("https://www.bilibili.com/video/BV1234", output_dir=tmp)
            self.assertEqual(got, str(Path(tmp) / "BV1234.mp4"))
            m_ydl.assert_not_called()

    def test_p2_request_does_not_hit_p1_cache(self):
        """#122 B2：?p=2 请求只匹配 {BV}_p2.mp4——目录里只有 {BV}_p1.mp4 不命中，必须真下载。

        旧实现 glob `{BV}_p*.mp4` 会把 p1 文件误配给 p2 请求（拿错集视频）。
        """
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "BV1234_p1.mp4").write_bytes(b"x")
            with mock.patch(
                "app.downloaders.bilibili_downloader.yt_dlp.YoutubeDL",
                return_value=_YdlCtx({"id": "BV1234"}),
            ) as m_ydl:
                with self.assertRaises(FileNotFoundError):
                    self.dl.download_video(
                        "https://www.bilibili.com/video/BV1234?p=2", output_dir=tmp
                    )
            m_ydl.assert_called_once()  # p1 缓存不得命中 p2 请求

    def test_p2_request_hits_exact_p2_cache(self):
        """#122 B2：?p=2 且目录有 {BV}_p2.mp4 → 精确命中，不下载。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "BV1234_p2.mp4").write_bytes(b"x")
            with mock.patch("app.downloaders.bilibili_downloader.yt_dlp.YoutubeDL") as m_ydl:
                got = self.dl.download_video(
                    "https://www.bilibili.com/video/BV1234?p=2", output_dir=tmp
                )
            self.assertEqual(got, str(Path(tmp) / "BV1234_p2.mp4"))
            m_ydl.assert_not_called()


class SubtitleFallbackCleanupTest(unittest.TestCase):
    """download_subtitles 的 yt-dlp 回退落临时目录、解析后清理（#121 B6）。

    旧实现：output_dir 缺省写数据根，yt-dlp 字幕文件成为常驻垃圾。
    """

    def setUp(self):
        patcher = mock.patch.object(
            BilibiliDownloader, "_write_netscape_cookie_file", return_value=None
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dl = BilibiliDownloader()

    def _patch_ydl(self, info):
        return mock.patch(
            "app.downloaders.bilibili_downloader.yt_dlp.YoutubeDL",
            return_value=_YdlCtx(info),
        )

    def test_fallback_cleans_temp_dir_on_no_subtitles(self):
        # player API 无字幕（返回 None）→ 走 yt-dlp 回退；仍无字幕 → None 且无残留
        with mock.patch(
            "app.downloaders.bilibili_subtitle.BilibiliSubtitleFetcher.fetch_subtitles",
            return_value=None,
        ):
            with self._patch_ydl({"requested_subtitles": {}}) as m_ydl:
                got = self.dl.download_subtitles("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertIsNone(got)
        m_ydl.assert_called_once()
        # 数据根没有新增文件，临时目录已清

        from app.utils.path_helper import get_data_dir

        leftovers = list(Path(get_data_dir()).rglob("*.srt")) + list(Path(get_data_dir()).rglob("*.json3"))
        self.assertEqual(leftovers, [])
        import glob as _glob

        self.assertEqual(_glob.glob("/tmp/videonote_subs_*"), [])

    def test_fallback_cleans_temp_dir_on_parse_success(self):
        # 回退成功解析出字幕 → 返回 TranscriptResult，临时文件同样清理
        info = {
            "requested_subtitles": {
                "zh-CN": {"ext": "srt", "data": "1\n00:00:00,000 --> 00:00:02,000\n你好\n\n"}
            }
        }
        with mock.patch(
            "app.downloaders.bilibili_subtitle.BilibiliSubtitleFetcher.fetch_subtitles",
            return_value=None,
        ):
            with self._patch_ydl(info):
                got = self.dl.download_subtitles("https://www.bilibili.com/video/BV1xx411c7mD")
        self.assertIsNotNone(got)
        self.assertEqual(got.segments[0].text, "你好")
        import glob as _glob

        self.assertEqual(_glob.glob("/tmp/videonote_subs_*"), [])


class BilibiliEntrySshBlockTest(unittest.TestCase):
    """#140 复扫 B1：BilibiliDownloader 自身入口 SSRF 校验——公共 app 层函数
    不依赖 MCP 入口 _guard_remote_url 兜底（与 generic/youtube 下载器同款内部防线）。"""

    def setUp(self):
        patcher = mock.patch.object(
            BilibiliDownloader, "_write_netscape_cookie_file", return_value=None
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dl = BilibiliDownloader()

    def test_download_private_url_blocked_before_ytdlp(self):
        out = tempfile.mkdtemp(prefix="vn_bili_")
        try:
            with mock.patch("app.downloaders.bilibili_downloader.yt_dlp") as m_yt:
                with self.assertRaises(ValueError) as cm:
                    self.dl.download(
                        "http://169.254.169.254/latest/meta-data/",
                        output_dir=out,
                    )
            self.assertIn("SSRF", str(cm.exception))
            m_yt.assert_not_called()  # yt-dlp 从未被触达
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_download_video_private_url_blocked(self):
        out = tempfile.mkdtemp(prefix="vn_bili_")
        try:
            with self.assertRaises(ValueError):
                self.dl.download_video("http://127.0.0.1/x", output_dir=out)
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
