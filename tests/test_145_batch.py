"""第 21 轮全库扫描 #145：安全 / 正确性 / 性能收口回归。"""
import json
import tempfile
import unittest
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest import mock

from app.models.audio_model import AudioDownloadResult, safe_audio_download_result_dict
from app.utils.url_safety import PublicOnlySession, public_replay_url
from videonote_mcp import cli, server


class PublicReplayUrlTest(unittest.TestCase):
    def test_keeps_youtube_watch_v(self):
        self.assertEqual(
            public_replay_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=12"),
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=12",
        )

    def test_strips_signed_query_and_userinfo(self):
        self.assertEqual(
            public_replay_url(
                "https://user:pass@cdn.example/v.mp4?token=secret&sig=abc&id=1"
            ),
            "https://cdn.example/v.mp4?id=1",
        )


class CoverUrlCacheTest(unittest.TestCase):
    def test_safe_audio_strips_cover_query(self):
        cached = safe_audio_download_result_dict(
            AudioDownloadResult(
                file_path="/tmp/a.mp3",
                title="t",
                duration=1,
                cover_url="https://cdn.example/cover.jpg?token=secret&x-expires=1",
                platform="douyin",
                video_id="1",
                raw_info={"tags": ["a"]},
            )
        )
        self.assertEqual(cached["cover_url"], "https://cdn.example/cover.jpg")
        self.assertNotIn("secret", json.dumps(cached))


class DouyinExtractVideoIdTest(unittest.TestCase):
    def test_head_failure_still_parses_path_id(self):
        from app.downloaders.douyin_downloader import DouyinDownloader

        dl = DouyinDownloader()
        with mock.patch(
            "app.downloaders.douyin_downloader.public_head",
            side_effect=TimeoutError("slow"),
        ):
            self.assertEqual(
                dl.extract_video_id("https://www.douyin.com/video/7234567890123456789"),
                "7234567890123456789",
            )


class BcutUploadSsrfTest(unittest.TestCase):
    def test_session_is_public_only(self):
        from app.transcriber.bcut import BcutTranscriber

        self.assertIsInstance(BcutTranscriber().session, PublicOnlySession)

    def test_private_upload_url_blocked(self):
        from app.transcriber.bcut import BcutTranscriber

        tr = BcutTranscriber()
        tr._BcutTranscriber__per_size = 4
        tr._BcutTranscriber__clips = 1
        tr._BcutTranscriber__upload_urls = ["http://169.254.169.254/part"]
        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            f.write(b"data")
            f.flush()
            with self.assertRaises(ValueError) as cm:
                tr._BcutTranscriber__upload_part(f.name)
        self.assertIn("SSRF", str(cm.exception))


class KuaishouPublicSessionTest(unittest.TestCase):
    def test_temp_cookies_use_public_get(self):
        from app.downloaders.kuaishou_helper.kuaishou import KuaiShou

        with mock.patch(
            "app.downloaders.kuaishou_helper.kuaishou._get_cfm"
        ) as cfm, mock.patch(
            "app.downloaders.kuaishou_helper.kuaishou.public_get"
        ) as g:
            cfm.return_value.get.return_value = None
            resp = mock.Mock()
            resp.cookies.get_dict.return_value = {"did": "1"}
            g.return_value = resp
            self.assertEqual(KuaiShou().get_temp_cookies(), "did=1")
            g.assert_called_once()


class InspectYtdlpReplayTest(unittest.TestCase):
    def test_generic_entry_strips_signed_query(self):
        from app.services import inspect as inspect_mod

        info = {
            "id": "x1",
            "title": "one",
            "duration": 3,
            "webpage_url": "https://cdn.example/watch?id=x1&token=secret",
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
            out = inspect_mod.inspect_video("https://example.com/v", platform="generic")
        self.assertTrue(out["ok"])
        self.assertEqual(out["entries"][0]["url"], "https://cdn.example/watch?id=x1")
        self.assertNotIn("secret", out["entries"][0]["url"])


class InspectLocalBoundaryTest(unittest.TestCase):
    def test_outside_existing_file_rejected(self):
        f = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        f.write(b"x")
        f.close()
        try:
            out = json.loads(server.inspect_video(f.name))
        finally:
            Path(f.name).unlink(missing_ok=True)
        self.assertFalse(out["ok"])
        self.assertIn("VIDEONOTE_ALLOW_EXTERNAL_PATHS", out["error"])


class BilibiliLoginUrlTest(unittest.TestCase):
    def test_sessdata_from_official_query(self):
        url = "https://passport.bilibili.com/x/passport-login/web/crossDomain?SESSDATA=abc"
        self.assertEqual(cli._bilibili_sessdata_from_login_url(url, {}), "abc")

    def test_rejects_non_official_host(self):
        with self.assertRaises(ValueError) as cm:
            cli._bilibili_sessdata_from_login_url("http://169.254.169.254/x", {})
        self.assertIn("不是 B 站官方", str(cm.exception))

    def test_allows_biligame_host_in_query(self):
        url = "https://passport.biligame.com/x/passport-login/web/crossDomain?SESSDATA=from-game"
        self.assertEqual(cli._bilibili_sessdata_from_login_url(url, {}), "from-game")


class ProviderLookupErrorTest(unittest.TestCase):
    def test_explicit_provider_db_error_not_missing(self):
        with mock.patch.object(
            server.ProviderService,
            "get_provider_by_id",
            side_effect=RuntimeError("database is locked"),
        ):
            with self.assertRaises(ValueError) as cm:
                server.generate_note(
                    "https://example.com/v",
                    platform="generic",
                    provider_id="openai",
                )
        self.assertIn("读取供应商失败", str(cm.exception))
        self.assertNotIn("供应商不存在", str(cm.exception))


class ListTasksIndexErrorTest(unittest.TestCase):
    def test_dao_failure_raises_instead_of_empty(self):
        from app.db.video_task_dao import list_tasks as dao_list

        with mock.patch("app.db.video_task_dao.get_db", side_effect=RuntimeError("locked")):
            with self.assertRaises(RuntimeError):
                dao_list()

    def test_mcp_list_tasks_surfaces_index_error(self):
        with mock.patch(
            "app.db.video_task_dao.list_tasks",
            side_effect=RuntimeError("locked"),
        ):
            with self.assertRaises(ValueError) as cm:
                server.list_tasks()
        self.assertIn("读取任务索引失败", str(cm.exception))


class WriteStatusIndexWarningTest(unittest.TestCase):
    def test_index_sync_failure_is_logged(self):
        with mock.patch(
            "app.db.video_task_dao.update_task_status",
            side_effect=RuntimeError("locked"),
        ), mock.patch.object(server.logger, "warning") as warn:
            server._write_status("t-index-fail", "CANCELLED", message="已取消")
        warn.assert_called()
        self.assertIn("同步任务索引失败", warn.call_args[0][0])


class WhisperRetryOnceTest(unittest.TestCase):
    def test_non_cache_error_does_not_rebuild(self):
        from app.transcriber.whisper import WhisperTranscriber

        calls = []

        def _build(*_a, **_k):
            calls.append(1)
            raise ConnectionError("timeout")

        with mock.patch.object(WhisperTranscriber, "_build_model", side_effect=_build), mock.patch(
            "app.transcriber.whisper.get_model_dir", return_value="/tmp"
        ):
            with self.assertRaises(ConnectionError):
                WhisperTranscriber(model_size="tiny", device="cpu")
        self.assertEqual(len(calls), 1)

    def test_cache_error_purges_and_retries(self):
        from huggingface_hub.utils import LocalEntryNotFoundError

        from app.transcriber.whisper import WhisperTranscriber

        model = mock.Mock()
        calls = []

        def _build(*_a, **_k):
            calls.append(1)
            if len(calls) == 1:
                raise LocalEntryNotFoundError("incomplete")
            return model

        with mock.patch.object(WhisperTranscriber, "_build_model", side_effect=_build), mock.patch(
            "app.transcriber.whisper.get_model_dir", return_value="/tmp"
        ), mock.patch.object(WhisperTranscriber, "_purge_cache") as purge:
            tr = WhisperTranscriber(model_size="tiny", device="cpu")
        self.assertIs(tr.model, model)
        self.assertEqual(len(calls), 2)
        purge.assert_called_once()


class XiaohongshuDownloaderCloseTest(unittest.TestCase):
    def test_owned_auth_closed(self):
        from app.downloaders.xiaohongshu_downloader import XiaohongshuDownloader

        auth = mock.Mock()
        dl = XiaohongshuDownloader()
        dl._auth = auth
        dl._owns_auth = True
        dl.close()
        auth.close.assert_called_once()
        self.assertIsNone(dl._auth)


class DeadCodeRemovedTest(unittest.TestCase):
    def test_round20_deferred_symbols_gone(self):
        import app.services.pipeline as pipeline
        import app.services.provider as provider
        import app.transcriber.audio_preprocess as preprocess
        import app.utils.model_status as model_status

        self.assertFalse(hasattr(pipeline, "get_gpt"))
        self.assertFalse(hasattr(provider.ProviderService, "get_provider_by_id_safe"))
        self.assertFalse(hasattr(model_status, "mlx_repo_revision"))
        self.assertFalse(hasattr(preprocess, "denoise"))


class AppLogRotationTest(unittest.TestCase):
    def test_app_log_uses_rotating_handler(self):
        import app.utils.logger as logmod

        logmod.get_logger("test_145_rotation")
        self.assertIsInstance(logmod._file_handler, RotatingFileHandler)


class LazyWhisperImportTest(unittest.TestCase):
    def test_provider_module_does_not_bind_whisper_class(self):
        import app.transcriber.transcriber_provider as provider

        self.assertNotIn("WhisperTranscriber", provider.__dict__)


def test_public_get_used_for_douyin_detail():
    from app.downloaders.douyin_downloader import DouyinDownloader

    dl = DouyinDownloader()
    dl.extract_video_id = mock.Mock(return_value="v123")
    dl.gen_real_msToken = mock.Mock(return_value="tok")
    resp = mock.Mock()
    resp.json.return_value = {"aweme_detail": {"aweme_id": "v123"}}
    with mock.patch("app.downloaders.douyin_downloader.public_get", return_value=resp) as g:
        out = dl.fetch_video_info("https://www.douyin.com/video/v123")
    assert out["aweme_detail"]["aweme_id"] == "v123"
    g.assert_called_once()
