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
import os
import subprocess
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
        from app.transcriber.bcut import (
            API_COMMIT_UPLOAD,
            API_CREATE_TASK,
            API_REQ_UPLOAD,
            BcutTranscriber,
        )

        t = BcutTranscriber()
        states = iter([{"state": 0}, {"state": 0}, {"state": 0}, {"state": 4, "result": json.dumps({"utterances": []})}])
        sleeps = []

        def _resp(data, headers=None):
            resp = mock.Mock()
            resp.raise_for_status.return_value = None
            resp.json.return_value = {"code": 0, "data": data}
            resp.headers = headers if headers is not None else {}
            return resp

        def _post(url, *a, **k):
            if url == API_REQ_UPLOAD:
                return _resp({
                    "size": 100, "in_boss_key": "k", "resource_id": "r",
                    "upload_id": "u", "upload_urls": ["https://upload.example/p1"],
                    "per_size": 1024,
                })
            if url == API_COMMIT_UPLOAD:
                return _resp({"download_url": "https://dl.example/a.mp3"})
            if url == API_CREATE_TASK:
                return _resp({"task_id": "t1"})
            raise AssertionError(f"未知 POST: {url}")

        def _get(*a, **k):
            return _resp(next(states))

        with tempfile.TemporaryDirectory() as td:
            fake = Path(td) / "fake.mp3"
            fake.write_bytes(b"x" * 100)
            # mock 下沉到 session 层（#139 C1）：真实走 _upload/_create_task/_query_result 绑定链
            with mock.patch.object(t.session, "post", side_effect=_post), \
                 mock.patch.object(t.session, "put", return_value=_resp(None, headers={"Etag": '"e1"'})), \
                 mock.patch.object(t.session, "get", side_effect=_get), \
                 mock.patch("app.transcriber.bcut.time.sleep", side_effect=lambda s: sleeps.append(s)):
                t.transcript(str(fake))
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

        with mock.patch("app.services.cookie_manager.read_json", return_value={"generic": cookie}), \
             mock.patch.object(mod, "get_data_dir", return_value=tempfile.gettempdir()), \
             mock.patch.object(mod.yt_dlp, "YoutubeDL", side_effect=_fake_ydl):
            mod.GenericDownloader().download("https://example-site.com/video")
        return captured

    def test_cookie_injected_as_header(self):
        captured = self._download_with_cookie("sessionid=abc123")
        headers = captured["http_headers"]
        self.assertEqual(headers["Cookie"], "sessionid=abc123")
        # 浏览器样头（防 YouTube 人机验证）与 Cookie 共存
        self.assertIn("Chrome/", headers["User-Agent"])
        self.assertNotIn("cookiefile", captured)

    def test_no_cookie_still_browser_headers(self):
        captured = self._download_with_cookie("")
        headers = captured["http_headers"]
        self.assertNotIn("Cookie", headers)
        self.assertIn("Chrome/", headers["User-Agent"])

    def test_no_cookie_file_left_on_disk(self):
        import app.downloaders.generic_downloader as mod

        with mock.patch("app.services.cookie_manager.read_json", return_value={"generic": "sessionid=abc123"}), \
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


class DownloaderWeakRegistryTest(unittest.TestCase):
    """下载器实例注册表弱引用化（#123 B5）：强引用 list 让实例引用计数永不归零、
    __del__ 不触发 → SESSDATA cookie 文件滞留 /tmp。改 WeakSet 后实例出作用域即 GC；
    atexit 兜底（_cleanup_created）对仍存活实例照常清理。"""

    class _FakeDL:
        def __init__(self):
            self.cleaned = 0

        def _cleanup_cookie_file(self):
            self.cleaned += 1

    def _patch_factory(self):
        from app.services import constant

        return mock.patch.dict(constant._DOWNLOADER_FACTORY, {"fake": self._FakeDL})

    def test_scope_end_gc_collects_instance(self):
        """WeakSet 不持有强引用：唯一强引用释放后实例立即从注册表消失（__del__ 可触发）。"""
        import gc

        from app.services import constant

        with self._patch_factory():
            inst = constant.get_downloader("fake")  # 显式持有 → 存活
            self.assertIn(inst, list(constant._created))
            del inst  # 唯一强引用释放 → 引用计数即时归零，WeakSet 同步移除
        gc.collect()  # 对已回收对象无害
        self.assertEqual(list(constant._created), [])

    def test_atexit_cleanup_still_cleans_living_instances(self):
        from app.services import constant

        with self._patch_factory():
            inst = constant.get_downloader("fake")  # 外部强引用 → 存活
            constant._cleanup_created()
            self.assertEqual(inst.cleaned, 1)  # 兜底清理照常触发
            inst.cleaned = 0
            constant._cleanup_created()  # 幂等：可重复调用
            self.assertEqual(inst.cleaned, 1)


class DouyinPercentDirTest(unittest.TestCase):
    """output_dir 含字面 %（如 /tmp/100%off/）时 download_video 不应炸（#123 B10）。

    旧实现 `output_path % {...}` 对整个字符串做 %-格式化——`100%off` 里的 %o 是
    非法格式符 → ValueError，下载失败。改 Path 拼接后目录含 % 也能正常下载。
    """

    def test_percent_in_output_dir_no_crash(self):
        from app.downloaders.douyin_downloader import DouyinDownloader

        with tempfile.TemporaryDirectory() as tmp:
            out_dir = os.path.join(tmp, "100%off")
            dl = DouyinDownloader()
            dl.extract_video_id = mock.Mock(return_value="v123")
            dl.fetch_video_info = mock.Mock(
                return_value={
                    "aweme_detail": {
                        "aweme_id": "7234567890",
                        "video": {"download_addr": {"url_list": ["https://example.com/v.mp4"]}},
                    }
                }
            )
            resp = mock.Mock()
            resp.__enter__ = mock.Mock(return_value=resp)
            resp.__exit__ = mock.Mock(return_value=False)
            resp.raise_for_status = mock.Mock()
            resp.iter_content = mock.Mock(return_value=iter([b"x" * 10]))
            with mock.patch("app.downloaders.douyin_downloader.requests.get",
                            return_value=resp) as m_get:
                path = dl.download_video("https://v.douyin.com/abc/", output_dir=out_dir)
            expected = os.path.join(out_dir, "7234567890.mp4")
            self.assertEqual(path, expected)
            self.assertTrue(os.path.exists(expected))
            m_get.assert_called_once()


class KuaishouVideoPathTest(unittest.TestCase):
    """快手 mp3 缓存命中时 mp4 缺失 → 补下，不返回悬空 video_path（#123 B9）。"""

    def _photo_info(self):
        return {
            "id": "ph1", "caption": "标题", "duration": 10,
            "coverUrl": "https://x/c.jpg", "photoUrl": "https://x/v.mp4",
        }

    def _run_download(self, td, need_video=True):
        from app.downloaders.kuaishou_downloader import KuaiShouDownloader

        dl = KuaiShouDownloader()
        video_raw = {"visionVideoDetail": {"photo": self._photo_info()}, "tags": []}
        with mock.patch("app.downloaders.kuaishou_downloader.KuaiShou") as m_ks:
            m_ks.return_value.run.return_value = video_raw
            with mock.patch("requests.get") as m_get:
                resp = mock.Mock()
                resp.status_code = 200
                resp.__enter__ = mock.Mock(return_value=resp)  # B8 后 `with requests.get`
                resp.__exit__ = mock.Mock(return_value=False)
                resp.iter_content = mock.Mock(return_value=iter([b"x" * 10]))
                m_get.return_value = resp
                result = dl.download("https://v.kuaishou.com/x", output_dir=td, need_video=need_video)
                return result, m_get

    def test_mp3_cache_without_mp4_redownloads_video(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "ph1.mp3"), "wb") as f:
                f.write(b"audio")
            result, m_get = self._run_download(td)
            # mp4 被补下：video_path 不悬空
            self.assertTrue(os.path.exists(result.video_path))
            self.assertEqual(result.video_path, os.path.join(td, "ph1.mp4"))
            m_get.assert_called_once()

    def test_mp3_cache_with_mp4_skips_download(self):
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "ph1.mp3"), "wb") as f:
                f.write(b"a")
            with open(os.path.join(td, "ph1.mp4"), "wb") as f:
                f.write(b"v")
            result, m_get = self._run_download(td)
            self.assertTrue(os.path.exists(result.video_path))
            m_get.assert_not_called()  # mp4 已在 → 不重复下载


class DouyinDownloadBehaviorTest(unittest.TestCase):
    """抖音 download() 修复行为（#124 B4/B5/B6）。"""

    def _detail(self, duration_ms=15300, music_url="https://example.com/m.mp3"):
        return {
            "aweme_detail": {
                "aweme_id": "7234567890",
                "item_title": "测试视频",
                "video": {
                    "duration": duration_ms,
                    "cover_original_scale": {"url_list": ["https://c.jpg"]},
                },
                "music": {"play_url": {"url_list": [music_url]}},
            }
        }

    def _download(self, td, detail):
        from app.downloaders.douyin_downloader import DouyinDownloader

        dl = DouyinDownloader()
        dl.fetch_video_info = mock.Mock(return_value=detail)
        resp = mock.Mock()
        resp.__enter__ = mock.Mock(return_value=resp)
        resp.__exit__ = mock.Mock(return_value=False)
        resp.raise_for_status = mock.Mock()
        resp.iter_content = mock.Mock(return_value=iter([b"x" * 1024]))
        with mock.patch("app.downloaders.douyin_downloader.requests.get", return_value=resp) as m_get:
            return dl.download("https://v.douyin.com/abc/", output_dir=td), m_get

    def test_fetch_failure_keeps_cause_chain(self):
        """请求失败抛 ValueError 且保留原始异常链（#124 B4）——旧写法丢链且输出元组。"""
        from app.downloaders.douyin_downloader import DouyinDownloader

        dl = DouyinDownloader()
        dl.extract_video_id = mock.Mock(return_value="v123")
        dl.gen_real_msToken = mock.Mock(return_value="tok")
        original = ConnectionError("dns down")
        with mock.patch("app.downloaders.douyin_downloader.requests.get", side_effect=original):
            with self.assertRaises(ValueError) as ctx:
                dl.fetch_video_info("https://v.douyin.com/abc/")
        self.assertIs(ctx.exception.__cause__, original)
        # 不再是元组 repr（旧写法 str() 输出 ('请求失败:', ...)）
        self.assertEqual(str(ctx.exception), "请求失败: dns down")

    def test_audio_request_uses_instance_headers_with_cookie(self):
        """音频下载请求必须带注入用户 cookie 的实例 headers（#124 B5）。"""
        from app.downloaders.douyin_downloader import DouyinDownloader

        # cfm 是模块级单例（import 时已构造），必须 patch 模块属性而非类
        with mock.patch("app.downloaders.douyin_downloader.cfm") as m_cfm:
            m_cfm.get.return_value = "SESSDATA=abc"
            DouyinDownloader()  # 构造副作用：构造时读取 cfm.get 注入 headers
            with tempfile.TemporaryDirectory() as td:
                _, m_get = self._download(td, self._detail())
            _, kwargs = m_get.call_args
            self.assertEqual(kwargs["headers"].get("Cookie"), "SESSDATA=abc")

    def test_duration_millis_normalized_to_seconds(self):
        """duration 单位毫秒需归一为秒（#124 B6）——15300ms 应为 15.3s。"""
        from app.downloaders.douyin_downloader import DouyinDownloader

        DouyinDownloader()  # 构造副作用：同上，确保 headers 注入后请求链路一致
        with tempfile.TemporaryDirectory() as td:
            result, _ = self._download(td, self._detail(duration_ms=15300))
        self.assertEqual(result.duration, 15.3)


class KuaishouStaleMp3Test(unittest.TestCase):
    """快手零字节/半成品 mp3 不被 exists 当成功产物（#124 B1）。"""

    def _photo_info(self):
        return {
            "id": "ph1", "caption": "标题", "duration": 10,
            "coverUrl": "https://x/c.jpg", "photoUrl": "https://x/v.mp4",
        }

    def test_zero_byte_mp3_not_trusted(self):
        from app.downloaders.kuaishou_downloader import KuaiShouDownloader

        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "ph1.mp3"), "wb") as f:
                f.write(b"")  # 0 字节：ffmpeg 中断残留
            dl = KuaiShouDownloader()
            video_raw = {"visionVideoDetail": {"photo": self._photo_info()}, "tags": []}
            with mock.patch("app.downloaders.kuaishou_downloader.KuaiShou") as m_ks:
                m_ks.return_value.run.return_value = video_raw
                with mock.patch("requests.get") as m_get:
                    resp = mock.Mock()
                    resp.status_code = 200
                    resp.__enter__ = mock.Mock(return_value=resp)
                    resp.__exit__ = mock.Mock(return_value=False)
                    resp.iter_content = mock.Mock(return_value=iter([b"x" * 10]))
                    m_get.return_value = resp
                    with mock.patch("app.downloaders.kuaishou_downloader.subprocess.run") as m_ffmpeg:
                        result = dl.download("https://v.kuaishou.com/x", output_dir=td)
            # 0 字节缓存不命中 → 重新转 mp3（ffmpeg 被调起）；预删已清掉残留半成品
            m_ffmpeg.assert_called_once()
            self.assertEqual(result.file_path, os.path.join(td, "ph1.mp3"))


class KuaishouTitleCleanTest(unittest.TestCase):
    """快手标题清洗统一：正常分支与 skip_download 分支同款（#124 B7）。"""

    def _photo_info(self):
        return {
            "id": "ph1", "caption": "标题\n带 换行", "duration": 10,
            "coverUrl": "https://x/c.jpg", "photoUrl": "https://x/v.mp4",
        }

    def test_normal_branch_title_cleaned(self):
        from app.downloaders.kuaishou_downloader import KuaiShouDownloader

        dl = KuaiShouDownloader()
        video_raw = {"visionVideoDetail": {"photo": self._photo_info()}, "tags": []}
        with mock.patch("app.downloaders.kuaishou_downloader.KuaiShou") as m_ks:
            m_ks.return_value.run.return_value = video_raw
            with mock.patch("requests.get") as m_get:
                resp = mock.Mock()
                resp.status_code = 200
                resp.__enter__ = mock.Mock(return_value=resp)
                resp.__exit__ = mock.Mock(return_value=False)
                resp.iter_content = mock.Mock(return_value=iter([b"x"]))
                m_get.return_value = resp
                with mock.patch("app.downloaders.kuaishou_downloader.subprocess.run"):
                    with tempfile.TemporaryDirectory() as td:
                        result = dl.download("https://v.kuaishou.com/x", output_dir=td)
        # 换行/空格被清洗，不再把原始 caption 透传给 DB/prompt
        self.assertNotIn("\n", result.title)
        self.assertEqual(result.title, "标题带_换行")


class KuaishouDownloadVideoTest(unittest.TestCase):
    """download_video 只下载 mp4、不白做 ffmpeg 转码（#125 B2）。"""

    def _photo_info(self):
        return {
            "id": "ph1", "caption": "标题", "duration": 10,
            "coverUrl": "https://x/c.jpg", "photoUrl": "https://x/v.mp4",
        }

    def test_no_mp3_no_ffmpeg(self):
        from app.downloaders.kuaishou_downloader import KuaiShouDownloader

        dl = KuaiShouDownloader()
        video_raw = {"visionVideoDetail": {"photo": self._photo_info()}, "tags": []}
        with mock.patch("app.downloaders.kuaishou_downloader.KuaiShou") as m_ks:
            m_ks.return_value.run.return_value = video_raw
            with mock.patch("requests.get") as m_get:
                resp = mock.Mock()
                resp.status_code = 200
                resp.__enter__ = mock.Mock(return_value=resp)
                resp.__exit__ = mock.Mock(return_value=False)
                resp.iter_content = mock.Mock(return_value=iter([b"x"]))
                m_get.return_value = resp
                with mock.patch("app.downloaders.kuaishou_downloader.subprocess.run") as m_ff:
                    with tempfile.TemporaryDirectory() as td:
                        path = dl.download_video("https://v.kuaishou.com/x", output_dir=td)
                        self.assertEqual(path, os.path.join(td, "ph1.mp4"))
                        self.assertTrue(os.path.exists(path))
                        m_ff.assert_not_called()  # 只下视频：ffmpeg 转 mp3 一步都不许跑
                        self.assertFalse(os.path.exists(os.path.join(td, "ph1.mp3")))


class KuaishouFfmpegFailureTest(unittest.TestCase):
    """ffmpeg 转换失败带退出码 + 保留原始异常链（#125 B11）。"""

    def _photo_info(self):
        return {
            "id": "ph1", "caption": "标题", "duration": 10,
            "coverUrl": "https://x/c.jpg", "photoUrl": "https://x/v.mp4",
        }

    def _run(self, exc):
        from app.downloaders.kuaishou_downloader import KuaiShouDownloader

        dl = KuaiShouDownloader()
        video_raw = {"visionVideoDetail": {"photo": self._photo_info()}, "tags": []}
        with mock.patch("app.downloaders.kuaishou_downloader.KuaiShou") as m_ks:
            m_ks.return_value.run.return_value = video_raw
            with mock.patch("requests.get") as m_get:
                resp = mock.Mock()
                resp.status_code = 200
                resp.__enter__ = mock.Mock(return_value=resp)
                resp.__exit__ = mock.Mock(return_value=False)
                resp.iter_content = mock.Mock(return_value=iter([b"x"]))
                m_get.return_value = resp
                with mock.patch(
                    "app.downloaders.kuaishou_downloader.subprocess.run", side_effect=exc
                ):
                    with tempfile.TemporaryDirectory() as td:
                        dl.download("https://v.kuaishou.com/x", output_dir=td)

    def test_called_process_error_has_returncode(self):
        with self.assertRaises(Exception) as ctx:
            self._run(subprocess.CalledProcessError(2, ["ffmpeg"]))
        self.assertIn("退出码 2", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, subprocess.CalledProcessError)

    def test_timeout_has_no_returncode(self):
        with self.assertRaises(Exception) as ctx:
            self._run(subprocess.TimeoutExpired("ffmpeg", 600))
        self.assertIn("超时", str(ctx.exception))
        self.assertIsInstance(ctx.exception.__cause__, subprocess.TimeoutExpired)


class YtdlpRetryFileNotFoundTest(unittest.TestCase):
    """ytdlp_retry 对 FileNotFoundError 立即抛出，不做指数退避空等（#125 B7）。"""

    def test_file_not_found_raises_immediately(self):
        from app.downloaders.common import ytdlp_retry

        exc = FileNotFoundError("yt-dlp 未安装或路径不存在")
        with mock.patch("app.downloaders.common.time.sleep") as m_sleep:
            with self.assertRaises(FileNotFoundError):
                ytdlp_retry(mock.Mock(side_effect=exc), attempts=3, base_delay=1.5)
        m_sleep.assert_not_called()  # 不是瞬时错误：一次也不退避

    def test_connection_error_retries_then_raises(self):
        from app.downloaders.common import ytdlp_retry

        exc = ConnectionError("socket closed")
        with mock.patch("app.downloaders.common.time.sleep"):
            with self.assertRaises(ConnectionError):
                ytdlp_retry(mock.Mock(side_effect=exc), attempts=3, base_delay=0.1)


class YoutubeSubtitleSessionTest(unittest.TestCase):
    """代理 Session 显式 close（#125 B16）：不再把连接池泄漏到 GC。"""

    def _fetch_with_proxy(self):
        from app.downloaders.youtube_subtitle import YouTubeSubtitleFetcher

        with mock.patch(
            "app.downloaders.youtube_subtitle.ProxyConfigManager"
        ) as m_pcm:
            m_pcm.return_value.get_proxy_url.return_value = "http://127.0.0.1:7890"
            # import requests 在 __init__ 内部（局部 import），模块顶层无 requests
            # 符号 → patch 全局 requests.Session
            with mock.patch("requests.Session") as m_sess:
                return YouTubeSubtitleFetcher(), m_sess

    def test_proxy_session_created_and_closeable(self):
        fetcher, m_sess = self._fetch_with_proxy()
        session = m_sess.return_value
        session.proxies = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
        fetcher.close()
        session.close.assert_called_once()

    def test_close_idempotent_and_gc_safe(self):
        fetcher, _ = self._fetch_with_proxy()
        fetcher.close()
        fetcher.close()  # 二次 close 不崩
        del fetcher  # __del__ 兜底不崩

    def test_no_proxy_no_session(self):
        from app.downloaders.youtube_subtitle import YouTubeSubtitleFetcher

        with mock.patch(
            "app.downloaders.youtube_subtitle.ProxyConfigManager"
        ) as m_pcm:
            m_pcm.return_value.get_proxy_url.return_value = None
            fetcher = YouTubeSubtitleFetcher()
            fetcher.close()  # 无 session 时 close 是 no-op
            self.assertFalse(hasattr(fetcher, "_session"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
