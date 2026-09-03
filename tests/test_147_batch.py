"""第 23 轮 #147：开放项收口（DNS 钉死 / diarize 残留 / ffmpeg 取消 / DAO / 下载态）。"""
import json
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from app.exceptions.task import TaskCancelledError
from app.utils import url_safety as us
from videonote_mcp import cli, server


def _addrinfo(ip: str, port: int = 0):
    return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))]


class DnsPinTest(unittest.TestCase):
    def setUp(self):
        self._orig_unpinned = us._unpinned_getaddrinfo

    def tearDown(self):
        us._unpinned_getaddrinfo = self._orig_unpinned
    def test_pin_rejects_private_resolve(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("127.0.0.1")):
            with self.assertRaises(ValueError) as ctx:
                with us.pin_public_host("http://evil.example/a"):
                    pass
        self.assertIn("SSRF", str(ctx.exception))

    def test_pin_keeps_validated_ip_when_dns_rebinds(self):
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("8.8.8.8")):
            with us.pin_public_host("http://evil.example/path"):
                us._unpinned_getaddrinfo = mock.Mock(return_value=_addrinfo("169.254.169.254"))
                result = socket.getaddrinfo("evil.example", 443, type=socket.SOCK_STREAM)
        self.assertEqual(result[0][4], ("8.8.8.8", 443))
        us._unpinned_getaddrinfo.assert_not_called()

    def test_fake_ip_literal_is_pinnable(self):
        with us.pin_public_host("http://198.18.0.1/x"):
            result = socket.getaddrinfo("198.18.0.1", 80, type=socket.SOCK_STREAM)
        self.assertEqual(result[0][4], ("198.18.0.1", 80))

    def test_session_send_fresh_resolve_blocks_private(self):
        req = __import__("requests").Request("GET", "http://rebind.example/").prepare()
        with mock.patch("socket.getaddrinfo", return_value=_addrinfo("169.254.169.254")):
            with mock.patch(
                "requests.adapters.HTTPAdapter.send",
                side_effect=AssertionError("不应发出请求"),
            ):
                with self.assertRaises(ValueError) as ctx:
                    us.PublicOnlySession().send(req)
        self.assertIn("SSRF", str(ctx.exception))


class DiarizeNormalizeCleanupTest(unittest.TestCase):
    def test_normalize_failure_cleans_partial_wav(self):
        src = server.DATA_DIR / "dia_src.wav"
        leftover = server.DATA_DIR / "dia_src_16k.wav"
        src.write_bytes(b"x")
        leftover.unlink(missing_ok=True)

        def _boom(path, out_dir=None):
            Path(path).with_name(Path(path).stem + "_16k.wav").write_bytes(b"partial")
            raise RuntimeError("ffmpeg failed")

        try:
            with mock.patch(
                "app.transcriber.audio_preprocess.normalize_to_wav", side_effect=_boom
            ):
                resp = json.loads(server.process_media(action="diarize", audio_file=str(src)))
            self.assertFalse(resp["ok"])
            self.assertFalse(leftover.exists())
        finally:
            src.unlink(missing_ok=True)
            leftover.unlink(missing_ok=True)


class FfmpegCancelTest(unittest.TestCase):
    class _HangProc:
        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

        returncode = -15

    def test_local_convert_honors_cancel(self):
        ev = threading.Event()
        ev.set()
        from app.downloaders.local_downloader import LocalDownloader

        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "a.mp4"
            out = Path(td) / "a.mp3"
            src.write_bytes(b"x")
            with mock.patch(
                "app.downloaders.common.subprocess.Popen", return_value=self._HangProc()
            ):
                with self.assertRaises(TaskCancelledError):
                    LocalDownloader().convert_to_mp3(str(src), str(out), cancel_event=ev)

    def test_kuaishou_ffmpeg_honors_cancel(self):
        from app.downloaders.kuaishou_downloader import KuaiShouDownloader

        ev = threading.Event()
        ev.set()
        photo = {
            "id": "ph1",
            "caption": "t",
            "duration": 1,
            "coverUrl": "https://x/c.jpg",
            "photoUrl": "https://x/v.mp4",
        }
        video_raw = {"visionVideoDetail": {"photo": photo}, "tags": []}
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("app.downloaders.kuaishou_downloader.KuaiShou") as m_ks, \
                 mock.patch("app.downloaders.kuaishou_downloader.stream_download"), \
                 mock.patch(
                     "app.downloaders.common.subprocess.Popen", return_value=self._HangProc()
                 ):
                m_ks.return_value.run.return_value = video_raw
                with self.assertRaises(TaskCancelledError):
                    KuaiShouDownloader().download(
                        "https://v.kuaishou.com/x", output_dir=td, cancel_event=ev
                    )


class ProviderDaoReraiseTest(unittest.TestCase):
    def test_insert_reraises_commit_failure(self):
        from app.db.provider_dao import insert_provider

        db = mock.Mock()
        db.commit.side_effect = RuntimeError("disk full")
        with mock.patch("app.db.provider_dao.get_db", return_value=iter([db])):
            with mock.patch("videonote_mcp.crypto.encrypt_value", side_effect=lambda v: v):
                with self.assertRaises(RuntimeError):
                    insert_provider("id", "n", "k", "https://example.com", "l", "custom")
        db.close.assert_called()

    def test_update_reraises_commit_failure(self):
        from app.db.provider_dao import update_provider

        db = mock.Mock()
        row = mock.Mock()
        db.query.return_value.filter_by.return_value.first.return_value = row
        db.commit.side_effect = RuntimeError("locked")
        with mock.patch("app.db.provider_dao.get_db", return_value=iter([db])):
            with self.assertRaises(RuntimeError):
                update_provider("id", name="x")
        db.close.assert_called()


class DownloadStateWiringTest(unittest.TestCase):
    def tearDown(self):
        from app.transcriber import model_download_state as dl

        dl._status.clear()
        dl._errors.clear()

    def test_cli_download_marks_done(self):
        from app.transcriber import model_download_state as dl

        with mock.patch(
            "app.transcriber.whisper_models.resolve_whisper_model",
            return_value="Systran/faster-whisper-tiny",
        ), mock.patch(
            "app.transcriber.whisper_models.resolve_whisper_revision",
            return_value="rev",
        ), mock.patch(
            "app.transcriber.whisper_models.is_local_target", return_value=False
        ), mock.patch(
            "app.utils.path_helper.get_model_dir", return_value="/tmp/models"
        ), mock.patch(
            "huggingface_hub.snapshot_download"
        ), mock.patch(
            "faster_whisper.WhisperModel"
        ):
            cli._download_whisper("tiny")
        self.assertEqual(dl.get_status("tiny"), dl.DONE)

    def test_cli_download_refuses_when_already_downloading(self):
        from app.transcriber import model_download_state as dl

        self.assertTrue(dl.try_mark("tiny"))
        with self.assertRaises(RuntimeError) as ctx:
            cli._download_whisper("tiny")
        self.assertIn("正在下载", str(ctx.exception))

    def test_cli_download_marks_failed(self):
        from app.transcriber import model_download_state as dl

        with mock.patch(
            "app.transcriber.whisper_models.resolve_whisper_model",
            return_value="Systran/faster-whisper-tiny",
        ), mock.patch(
            "app.transcriber.whisper_models.resolve_whisper_revision",
            return_value="rev",
        ), mock.patch(
            "app.transcriber.whisper_models.is_local_target", return_value=False
        ), mock.patch(
            "app.utils.path_helper.get_model_dir", return_value="/tmp/models"
        ), mock.patch(
            "huggingface_hub.snapshot_download", side_effect=RuntimeError("hf down")
        ):
            with self.assertRaises(RuntimeError):
                cli._download_whisper("tiny")
        self.assertEqual(dl.get_status("tiny"), dl.FAILED)


class DeadCodeRemovedTest(unittest.TestCase):
    def test_unused_write_apis_gone(self):
        from app.db import model_dao
        from app.enmus.task_status_enums import TaskStatus
        from app.gpt.base import GPT
        from app.gpt.universal_gpt import UniversalGPT

        self.assertFalse(hasattr(model_dao, "delete_model"))
        self.assertFalse(hasattr(TaskStatus, "description"))
        self.assertFalse(hasattr(GPT, "list_models"))
        self.assertFalse(hasattr(UniversalGPT, "list_models"))


class MlxHolderCloseTest(unittest.TestCase):
    def test_close_clears_matching_holder(self):
        from app.transcriber.mlx_whisper_transcriber import MLXWhisperTranscriber

        holder = mock.Mock()
        holder.model_path = "/models/x"
        holder.model = object()
        transcribe_mod = mock.Mock(ModelHolder=holder)
        t = MLXWhisperTranscriber.__new__(MLXWhisperTranscriber)
        t.model_path = "/models/x"
        with mock.patch.dict(
            sys.modules,
            {"mlx_whisper": mock.Mock(), "mlx_whisper.transcribe": transcribe_mod},
        ):
            t.close()
        self.assertIsNone(holder.model)
        self.assertIsNone(holder.model_path)
        self.assertIsNone(t.model_path)
