"""下载器健壮性测试（docs/05 #36 剩余子项）。

覆盖：
- kuaishou：失败点抛 RuntimeError（原先 None 时 `video_details['data']` → TypeError）
- bcut：轮询指数退避 + 全部 HTTP 调用带 timeout
- generic：cookie 走 http_headers 注入（原先 Netscape example.com 永不生效）
- note._download_media：audio.json 在但实体文件悬空 → 视为缓存失效重新下载

运行：
    cd <repo>
    .venv/bin/python tests/test_downloader_robustness.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.downloaders.kuaishou_helper.kuaishou import KuaiShou


class KuaiShouFailureTest(unittest.TestCase):
    """快手各失败点必须抛明确异常，不能把 None 当 dict 用（TypeError）。"""

    def _mk(self):
        return KuaiShou()

    def test_no_link_raises(self):
        with self.assertRaisesRegex(RuntimeError, "URL 解析失败"):
            self._mk().run("无链接文本")

    def test_no_cookies_raises(self):
        ks = self._mk()
        with mock.patch.object(ks, "_extract_kuaishou_link", return_value="https://v.kuaishou.com/abc"), \
             mock.patch.object(ks, "get_temp_cookies", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "cookies 解析失败"):
                ks.run("x")

    def test_no_photo_id_raises(self):
        ks = self._mk()
        with mock.patch.object(ks, "_extract_kuaishou_link", return_value="https://v.kuaishou.com/abc"), \
             mock.patch.object(ks, "get_temp_cookies", return_value="did=1"), \
             mock.patch.object(ks, "get_photo_id", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "ID 解析失败"):
                ks.run("x")

    def test_no_details_raises_not_typeerror(self):
        ks = self._mk()
        with mock.patch.object(ks, "_extract_kuaishou_link", return_value="https://v.kuaishou.com/abc"), \
             mock.patch.object(ks, "get_temp_cookies", return_value="did=1"), \
             mock.patch.object(ks, "get_photo_id", return_value="ph1"), \
             mock.patch.object(ks, "get_video_details", return_value=None):
            with self.assertRaises(RuntimeError):
                ks.run("x")

    def test_empty_data_raises(self):
        ks = self._mk()
        with mock.patch.object(ks, "_extract_kuaishou_link", return_value="https://v.kuaishou.com/abc"), \
             mock.patch.object(ks, "get_temp_cookies", return_value="did=1"), \
             mock.patch.object(ks, "get_photo_id", return_value="ph1"), \
             mock.patch.object(ks, "get_video_details", return_value={"data": None}):
            with self.assertRaisesRegex(RuntimeError, "无 data"):
                ks.run("x")

    def test_success_returns_data(self):
        ks = self._mk()
        payload = {"data": {"visionVideoDetail": {"photo": {"id": "ph1"}}}}
        with mock.patch.object(ks, "_extract_kuaishou_link", return_value="https://v.kuaishou.com/abc"), \
             mock.patch.object(ks, "get_temp_cookies", return_value="did=1"), \
             mock.patch.object(ks, "get_photo_id", return_value="ph1"), \
             mock.patch.object(ks, "get_video_details", return_value=payload):
            self.assertEqual(ks.run("x"), payload["data"])

    def test_get_photo_id_unmatched_returns_none(self):
        ks = self._mk()
        with mock.patch("app.downloaders.kuaishou_helper.kuaishou.requests") as m_req:
            m_req.get.return_value.url = "https://v.kuaishou.com/fWvrA9B"
            self.assertIsNone(ks.get_photo_id("https://v.kuaishou.com/fWvrA9B"))

    def test_get_photo_id_matched(self):
        ks = self._mk()
        with mock.patch("app.downloaders.kuaishou_helper.kuaishou.requests") as m_req:
            m_req.get.return_value.url = "https://www.kuaishou.com/short-video/3xabc123"
            self.assertEqual(ks.get_photo_id("https://v.kuaishou.com/x"), "3xabc123")


class BcutBackoffTest(unittest.TestCase):
    """必剪轮询指数退避 1→2→4→5s 封顶；HTTP 调用带 timeout。"""

    def test_poll_backoff_sequence(self):
        from app.transcriber.bcut import BcutTranscriber

        t = BcutTranscriber()
        states = iter([{"state": 0}, {"state": 0}, {"state": 0}, {"state": 4, "result": json.dumps({"utterances": []})}])
        sleeps = []
        with mock.patch.object(t, "_upload"), \
             mock.patch.object(t, "_create_task"), \
             mock.patch.object(t, "_query_result", side_effect=lambda: next(states)), \
             mock.patch("app.transcriber.bcut.time.sleep", side_effect=lambda s: sleeps.append(s)):
            t.transcript("/tmp/fake.mp3")
        # i=0,1,2 → 1,2,4；第 4 次循环命中 state=4 直接 break
        self.assertEqual(sleeps, [1, 2, 4])

    def test_http_calls_have_timeout(self):
        from app.transcriber.bcut import BcutTranscriber

        t = BcutTranscriber()
        resp = mock.Mock()
        resp.raise_for_status.return_value = None
        resp.json.return_value = {"code": 0, "data": {"task_id": "t1"}}
        with mock.patch.object(t.session, "post", return_value=resp) as m_post:
            t._create_task()
        self.assertIn("timeout", m_post.call_args.kwargs)
        self.assertGreaterEqual(m_post.call_args.kwargs["timeout"][1], 10)


class GenericCookieTest(unittest.TestCase):
    """generic cookie 走 http_headers 注入（不再写 example.com Netscape 文件）。"""

    def _download_with_cookie(self, cookie):
        import app.downloaders.generic_downloader as mod

        captured = {}

        def _fake_ydl(opts):
            captured.update(opts)
            ydl = mock.Mock()
            ydl.__enter__ = mock.Mock(return_value=ydl)
            ydl.__exit__ = mock.Mock(return_value=False)
            ydl.extract_info.return_value = {"id": "vid1", "title": "t", "duration": 1, "ext": "m4a"}
            return ydl

        with mock.patch.object(mod.CookieConfigManager, "get", return_value=cookie), \
             mock.patch.object(mod, "get_data_dir", return_value=tempfile.gettempdir()), \
             mock.patch.object(mod.yt_dlp, "YoutubeDL", side_effect=_fake_ydl):
            mod.GenericDownloader().download("https://example-site.com/video")
        return captured

    def test_cookie_injected_as_header(self):
        captured = self._download_with_cookie("sessionid=abc123")
        self.assertEqual(captured["http_headers"], {"Cookie": "sessionid=abc123"})
        self.assertNotIn("cookiefile", captured)

    def test_no_cookie_no_headers(self):
        captured = self._download_with_cookie("")
        self.assertNotIn("http_headers", captured)

    def test_no_cookie_file_left_on_disk(self):
        import app.downloaders.generic_downloader as mod

        with mock.patch.object(mod.CookieConfigManager, "get", return_value="sessionid=abc123"), \
             mock.patch.object(mod, "get_data_dir", return_value=tempfile.gettempdir()):
            dl = mod.GenericDownloader()
            self.assertEqual(dl._get_cookie(), "sessionid=abc123")
        self.assertIsNone(getattr(dl, "_cookiefile", None))


class AudioCacheStaleTest(unittest.TestCase):
    """audio.json 在但实体文件悬空 → 需要音频时视为缓存失效重新下载。"""

    def _gen(self):
        from app.services.note import NoteGenerator

        return NoteGenerator()

    def _run_download_media(self, gen, downloader, audio_cache_file, skip_download):
        return gen._download_media(
            downloader=downloader,
            video_url="https://example.com/v.mp4",
            quality="fast",
            audio_cache_file=audio_cache_file,
            status_phase="downloading",
            platform="generic",
            output_path=None,
            screenshot=False,
            video_understanding=False,
            video_interval=6,
            grid_size=[],
            skip_download=skip_download,
        )

    def _write_stale_cache(self, cache_file: Path):
        cache_file.write_text(
            json.dumps({"file_path": "/no/such/audio.mp3", "title": "t", "duration": 1.0,
                        "cover_url": None, "platform": "generic", "video_id": "v1", "raw_info": {}}),
            encoding="utf-8",
        )

    def test_stale_cache_redownloads_when_audio_needed(self):
        from app.models.audio_model import AudioDownloadResult

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "audio.json"
            self._write_stale_cache(cache)
            downloader = mock.Mock()
            downloader.download.return_value = AudioDownloadResult(
                file_path="/real/audio.mp3", title="t", duration=1.0,
                cover_url=None, platform="generic", video_id="v1", raw_info={},
            )
            gen = self._gen()
            result = self._run_download_media(gen, downloader, cache, skip_download=False)
            self.assertEqual(result.file_path, "/real/audio.mp3")
            downloader.download.assert_called_once()

    def test_stale_cache_ok_when_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "audio.json"
            self._write_stale_cache(cache)
            downloader = mock.Mock()
            gen = self._gen()
            result = self._run_download_media(gen, downloader, cache, skip_download=True)
            self.assertEqual(result.file_path, "/no/such/audio.mp3")
            downloader.download.assert_not_called()

    def test_corrupt_cache_redownloads(self):
        from app.models.audio_model import AudioDownloadResult

        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "audio.json"
            cache.write_text("{not json", encoding="utf-8")
            downloader = mock.Mock()
            downloader.download.return_value = AudioDownloadResult(
                file_path="/real/audio.mp3", title="t", duration=1.0,
                cover_url=None, platform="generic", video_id="v1", raw_info={},
            )
            gen = self._gen()
            with mock.patch("app.services.note.logger") as m_log:
                self._run_download_media(gen, downloader, cache, skip_download=False)
            m_log.warning.assert_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
