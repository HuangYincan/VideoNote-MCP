"""下载器集成测试（docs/05 #31 剩余：真实下载器类 + mock 网络层）。

对每个下载器类跑真实 download / download_video / skip_download 流程，
只 mock yt-dlp（extract_info 与文件写入）与 ffmpeg（local 转码），
验证产出 AudioDownloadResult 的字段契约、ytd_opts 组装、缓存命中语义。

运行：
    cd <repo>
    .venv/bin/python tests/test_downloader_integration.py
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.audio_model import AudioDownloadResult


def _fake_ydl(captured: dict, info: dict, download_ok: bool = True):
    """构造 yt_dlp.YoutubeDL 的 side_effect：捕获 opts，extract_info 返回 info。

    download_ok=False 时模拟「声称下载了但文件没写出来」（download_video 的
    文件存在性检查应抛错）。
    """

    def _make(opts):
        captured.update(opts)
        ydl = mock.Mock()
        ydl.__enter__ = mock.Mock(return_value=ydl)
        ydl.__exit__ = mock.Mock(return_value=False)
        ydl.extract_info.return_value = info
        if download_ok:
            # 模拟 yt-dlp 真实写出 outtmpl 产物
            outtmpl = opts.get("outtmpl", "")
            if outtmpl and "%(id)s" in outtmpl:
                fname = outtmpl % {"id": info.get("id"), "ext": info.get("ext", "m4a")}
                Path(fname).parent.mkdir(parents=True, exist_ok=True)
                Path(fname).write_bytes(b"audio")
        return ydl

    return _make


class BilibiliDownloaderTest(unittest.TestCase):
    def _dl(self):
        from app.downloaders.bilibili_downloader import BilibiliDownloader

        return BilibiliDownloader()

    def _patch_ydl(self, captured, info):
        import app.downloaders.bilibili_downloader as mod

        return mock.patch.object(mod.yt_dlp, "YoutubeDL", side_effect=_fake_ydl(captured, info))

    def test_download_full_result_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = {}
            info = {"id": "BV1xx", "title": "测试视频", "duration": 90, "thumbnail": "http://x/c.jpg"}
            with self._patch_ydl(captured, info):
                result = self._dl().download("https://www.bilibili.com/video/BV1xx", output_dir=tmp)
            self.assertIsInstance(result, AudioDownloadResult)
            self.assertEqual(result.platform, "bilibili")
            self.assertEqual(result.video_id, "BV1xx")
            self.assertEqual(result.file_path, os.path.join(tmp, "BV1xx.mp3"))
            self.assertEqual(result.duration, 90)
            self.assertEqual(result.title, "测试视频")
            # bilibili 音频必须带 Referer
            self.assertEqual(captured["http_headers"], {"Referer": "https://www.bilibili.com"})

    def test_quality_maps_to_preferredquality(self):
        for quality, expected in [("fast", "32"), ("medium", "64"), ("slow", "128")]:
            with tempfile.TemporaryDirectory() as tmp:
                captured = {}
                with self._patch_ydl(captured, {"id": "BV1x", "title": "t", "duration": 1}):
                    self._dl().download("https://www.bilibili.com/video/BV1x", output_dir=tmp, quality=quality)
                self.assertEqual(captured["postprocessors"][0]["preferredquality"], expected)

    def test_skip_download_passes_download_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = {}
            with self._patch_ydl(captured, {"id": "BV1x", "title": "t", "duration": 1}):
                self._dl().download("https://www.bilibili.com/video/BV1x", output_dir=tmp, skip_download=True)
            self.assertNotIn("skip_download", captured)

    def test_download_video_cache_hit_uses_glob(self):
        import app.downloaders.bilibili_downloader as mod

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "BV1xx_p1.mp4").write_bytes(b"v")  # 多 P 命名：id 是 {BV}_pN
            with mock.patch.object(mod.yt_dlp, "YoutubeDL") as m_ydl:
                path = self._dl().download_video("https://www.bilibili.com/video/BV1xx?p=1", output_dir=tmp)
            self.assertEqual(path, os.path.join(tmp, "BV1xx_p1.mp4"))
            m_ydl.assert_not_called()

    def test_download_video_missing_file_raises(self):
        import app.downloaders.bilibili_downloader as mod

        with tempfile.TemporaryDirectory() as tmp:
            captured = {}
            with mock.patch.object(mod.yt_dlp, "YoutubeDL",
                                   side_effect=_fake_ydl(captured, {"id": "BV1x", "title": "t"}, download_ok=False)):
                with self.assertRaises(FileNotFoundError):
                    self._dl().download_video("https://www.bilibili.com/video/BV1x", output_dir=tmp)


class YoutubeDownloaderTest(unittest.TestCase):
    def _dl(self):
        from app.downloaders.youtube_downloader import YoutubeDownloader

        with mock.patch("app.downloaders.youtube_downloader.CookieConfigManager") as m_cfm:
            m_cfm.return_value.get.return_value = ""
            return YoutubeDownloader()

    def _patch_ydl(self, captured, info):
        import app.downloaders.youtube_downloader as mod

        with mock.patch.object(mod, "_apply_proxy", return_value=None):
            return mock.patch.object(mod.yt_dlp, "YoutubeDL", side_effect=_fake_ydl(captured, info))

    def test_download_uses_ext_from_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = {}
            info = {"id": "dQw4w9WgXcQ", "title": "YT", "duration": 60, "ext": "webm", "tags": ["a"]}
            with self._patch_ydl(captured, info):
                result = self._dl().download("https://youtu.be/dQw4w9WgXcQ", output_dir=tmp)
            self.assertEqual(result.file_path, os.path.join(tmp, "dQw4w9WgXcQ.webm"))
            self.assertEqual(result.platform, "youtube")
            self.assertEqual(result.raw_info["tags"], ["a"])

    def test_download_video_existing_short_circuits(self):
        import app.downloaders.youtube_downloader as mod

        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "dQw4w9WgXcQ.mp4").write_bytes(b"v")
            with mock.patch.object(mod.yt_dlp, "YoutubeDL") as m_ydl:
                path = self._dl().download_video("https://youtu.be/dQw4w9WgXcQ", output_dir=tmp)
            self.assertEqual(path, os.path.join(tmp, "dQw4w9WgXcQ.mp4"))
            m_ydl.assert_not_called()

    def test_download_video_retry_on_retryable_error(self):
        """ytdlp_retry 对网络类错误重试（3 次），业务错误立即抛。"""
        import yt_dlp

        import app.downloaders.youtube_downloader as mod

        with tempfile.TemporaryDirectory() as tmp:
            ydl = mock.Mock()
            ydl.__enter__ = mock.Mock(return_value=ydl)
            ydl.__exit__ = mock.Mock(return_value=False)
            state = {"raised": False}

            def _extract(*args, **kwargs):
                if not state["raised"]:
                    state["raised"] = True
                    raise yt_dlp.utils.DownloadError("timed out")
                Path(tmp, "dQw4w9WgXcQ.mp4").write_bytes(b"v")  # 模拟 yt-dlp 下载产物
                return {"id": "dQw4w9WgXcQ", "title": "t"}

            ydl.extract_info.side_effect = _extract
            with mock.patch.object(mod, "_apply_proxy", return_value=None), \
                 mock.patch.object(mod.yt_dlp, "YoutubeDL", return_value=ydl):
                path = self._dl().download_video("https://youtu.be/dQw4w9WgXcQ", output_dir=tmp)
            self.assertEqual(path, os.path.join(tmp, "dQw4w9WgXcQ.mp4"))
            self.assertEqual(ydl.extract_info.call_count, 2)

    def test_download_video_business_error_no_retry(self):
        """登录墙等业务错误立即抛，不重试。"""
        import yt_dlp

        import app.downloaders.youtube_downloader as mod

        with tempfile.TemporaryDirectory() as tmp:
            ydl = mock.Mock()
            ydl.__enter__ = mock.Mock(return_value=ydl)
            ydl.__exit__ = mock.Mock(return_value=False)
            ydl.extract_info.side_effect = [
                yt_dlp.utils.DownloadError("Video unavailable, this video is private"),
                {"id": "dQw4w9WgXcQ", "title": "t"},
            ]
            with mock.patch.object(mod, "_apply_proxy", return_value=None), \
                 mock.patch.object(mod.yt_dlp, "YoutubeDL", return_value=ydl):
                with self.assertRaises(yt_dlp.utils.DownloadError):
                    self._dl().download_video("https://youtu.be/dQw4w9WgXcQ", output_dir=tmp)
            self.assertEqual(ydl.extract_info.call_count, 1)


class LocalDownloaderTest(unittest.TestCase):
    def _dl(self):
        from app.downloaders.local_downloader import LocalDownloader

        return LocalDownloader()

    def test_skip_download_returns_source_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "talk.mp4")
            src.write_bytes(b"media")
            result = self._dl().download(str(src), output_dir=tmp, skip_download=True)
            self.assertEqual(result.file_path, str(src))
            self.assertEqual(result.platform, "local")
            self.assertEqual(result.video_id, "talk")

    def test_download_converts_to_mp3(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp, "talk.mp4")
            src.write_bytes(b"media")
            with mock.patch("app.downloaders.local_downloader.subprocess.run") as m_run, \
                 mock.patch("app.downloaders.local_downloader.save_cover_to_static", return_value=""):
                Path(tmp, "talk.mp3").write_bytes(b"x")  # 模拟 ffmpeg 转换产物
                result = self._dl().download(str(src), output_dir=tmp)
            self.assertEqual(result.file_path, str(Path(tmp, "talk.mp3")))
            m_run.assert_called()

    def test_download_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            self._dl().download("/no/such/file.mp4")

    def test_download_video_missing_raises(self):
        with self.assertRaises(FileNotFoundError):
            self._dl().download_video("/no/such/file.mp4")


class GenericDownloaderTest(unittest.TestCase):
    def _dl(self):
        from app.downloaders.generic_downloader import GenericDownloader

        return GenericDownloader()

    def _patch_ydl(self, captured, info):
        import app.downloaders.generic_downloader as mod

        with mock.patch.object(mod.CookieConfigManager, "get", return_value=""):
            return mock.patch.object(mod.yt_dlp, "YoutubeDL", side_effect=_fake_ydl(captured, info))

    def test_download_result_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            captured = {}
            info = {"id": "g1", "title": "generic", "duration": 5, "extractor": "GenericIE", "ext": "m4a"}
            with self._patch_ydl(captured, info):
                result = self._dl().download("https://site.example/v", output_dir=tmp)
            self.assertEqual(result.platform, "generic")
            self.assertEqual(result.raw_info["extractor"], "GenericIE")
            self.assertEqual(result.file_path, os.path.join(tmp, "g1.m4a"))
            self.assertIn("noplaylist", captured)

    def test_download_video_missing_file_raises(self):
        import app.downloaders.generic_downloader as mod

        with tempfile.TemporaryDirectory() as tmp:
            captured = {}
            with mock.patch.object(mod.CookieConfigManager, "get", return_value=""), \
                 mock.patch.object(mod.yt_dlp, "YoutubeDL",
                                   side_effect=_fake_ydl(captured, {"id": "g1", "title": "t"}, download_ok=False)):
                with self.assertRaises(RuntimeError):
                    self._dl().download_video("https://site.example/v", output_dir=tmp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
