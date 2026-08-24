"""第 16 轮全库扫描批次 2 测试（docs/05 B1-B4）。

- B1 取消可打断进行中的下载/转码：yt-dlp progress hook、stream_download、ffmpeg 提取
- B2 预处理分块转写部分失败 → truncated 透传（对象字段 / 笔记标注）
- B3 note_cache promote 并发不再共用固定 .tmp 路径
- B4 抖音/快手直连下载：连接/读分离超时 + 指数退避重试 + 取消中断
"""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import requests

from app.downloaders.common import stream_download, ytdlp_cancel_hook
from app.exceptions.task import TaskCancelledError
from app.services import note_cache
from app.services.note import _extract_audio_from_video


def _ok_response(chunks=(b"x" * 1024,)):
    resp = mock.Mock()
    resp.__enter__ = mock.Mock(return_value=resp)
    resp.__exit__ = mock.Mock(return_value=False)
    resp.raise_for_status = mock.Mock()
    resp.iter_content = mock.Mock(return_value=iter(chunks))
    return resp


class YtdlpCancelHookTest(unittest.TestCase):
    """B1：yt-dlp progress_hooks 在取消时抛 TaskCancelledError。"""

    def test_raises_when_event_set(self):
        ev = threading.Event()
        hook = ytdlp_cancel_hook(ev)
        hook({"status": "downloading"})  # 未 set：不抛
        ev.set()
        with self.assertRaises(TaskCancelledError):
            hook({"status": "downloading"})

    def test_no_event_is_noop(self):
        ytdlp_cancel_hook(None)({"status": "finished"})  # 不抛


class StreamDownloadRetryTest(unittest.TestCase):
    """B4：瞬时错误退避重试；业务错误不重试；attempts 校验；取消中断（B1）。"""

    def test_transient_error_retried_then_succeeds(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "v.mp4"
            with mock.patch(
                "requests.get",
                side_effect=[requests.exceptions.ConnectionError("boom"), _ok_response()],
            ) as m_get:
                stream_download("https://example.com/v.mp4", str(out), attempts=3, base_delay=0)
            self.assertTrue(out.exists())
            self.assertEqual(m_get.call_count, 2)  # 第 1 次瞬时失败 + 第 2 次成功

    def test_http_404_not_retried(self):
        bad = mock.Mock()
        bad.__enter__ = mock.Mock(return_value=bad)
        bad.__exit__ = mock.Mock(return_value=False)
        bad.raise_for_status = mock.Mock(
            side_effect=requests.exceptions.HTTPError("404", response=mock.Mock(status_code=404))
        )
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("requests.get", side_effect=[bad]) as m_get:
                with self.assertRaises(requests.exceptions.HTTPError):
                    stream_download("https://example.com/v.mp4", str(Path(td) / "v.mp4"), attempts=3)
        self.assertEqual(m_get.call_count, 1)  # 业务错误重试无意义

    def test_http_503_retried_then_fails_after_exhaustion(self):
        bad = mock.Mock()
        bad.__enter__ = mock.Mock(return_value=bad)
        bad.__exit__ = mock.Mock(return_value=False)
        bad.raise_for_status = mock.Mock(
            side_effect=requests.exceptions.HTTPError("503", response=mock.Mock(status_code=503))
        )
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("requests.get", side_effect=[bad, bad, bad]) as m_get:
                with self.assertRaises(requests.exceptions.HTTPError):
                    stream_download(
                        "https://example.com/v.mp4", str(Path(td) / "v.mp4"), attempts=3, base_delay=0
                    )
        self.assertEqual(m_get.call_count, 3)

    def test_attempts_non_positive_rejected(self):
        with self.assertRaises(ValueError):
            stream_download("https://example.com/v.mp4", "/tmp/x.mp4", attempts=0)

    def test_private_resource_url_blocked(self):
        """#140：API 返回的资源 URL（url_list/photoUrl）在 stream_download 入口
        直接被 SSRF 防护拦截——入口 URL 校验覆盖不到的点。"""
        with self.assertRaises(ValueError) as cm:
            stream_download("http://169.254.169.254/latest/meta-data/", "/tmp/x.mp4")
        self.assertIn("SSRF", str(cm.exception))

    @staticmethod
    def _redirect_resp(location: str):
        resp = mock.Mock()
        resp.status_code = 302
        resp.headers = {"Location": location}
        resp.close = mock.Mock()
        return resp

    def test_redirect_to_private_blocked_per_hop(self):
        """#140 复扫 A1：302 → 内网/云元数据 的第二跳必须在**发出前**被拦截——
        裸 requests.get 跟随重定向曾把公网入口打到 169.254.169.254。"""
        calls = []

        def _fake_get(url, **kwargs):
            calls.append(url)
            return self._redirect_resp("http://169.254.169.254/latest/meta-data/")

        with mock.patch("requests.get", side_effect=_fake_get):
            with self.assertRaises(ValueError) as cm:
                stream_download("https://example.com/a.mp3", "/tmp/x.mp4")
        self.assertIn("SSRF", str(cm.exception))
        self.assertEqual(calls, ["https://example.com/a.mp3"])  # 第二跳从未发出

    def test_public_redirect_chain_followed(self):
        """公网 CDN 跳链（302 → 另一公网 CDN）照常下载，每跳先校验后请求。"""
        calls = []

        def _fake_get(url, **kwargs):
            calls.append(url)
            if len(calls) == 1:
                return self._redirect_resp("https://cdn.example.com/b.mp3")
            return _ok_response()

        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "v.mp4"
            with mock.patch("requests.get", side_effect=_fake_get):
                stream_download("https://example.com/a.mp3", str(out))
            self.assertTrue(out.exists())
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1], "https://cdn.example.com/b.mp3")

    def test_redirect_loop_capped(self):
        """自跳转（Location 指回自己）不无限循环：超过 _MAX_REDIRECTS 抛 TooManyRedirects。"""
        calls = []

        def _fake_get(url, **kwargs):
            calls.append(url)
            return self._redirect_resp(url)

        with mock.patch("requests.get", side_effect=_fake_get):
            with self.assertRaises(requests.exceptions.TooManyRedirects):
                stream_download("https://example.com/a.mp3", "/tmp/x.mp4")
        # 第一次请求 + 10 次跳点（redirects>10 时抛）——共 11 次 get 后中止
        self.assertEqual(len(calls), 11)

    def test_cancel_interrupts_download_loop(self):
        ev = threading.Event()

        def _chunks():
            yield b"a"
            ev.set()
            yield b"b"  # 下一轮循环体检查到取消

        resp = _ok_response()
        resp.iter_content = mock.Mock(return_value=_chunks())
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("requests.get", return_value=resp):
                with self.assertRaises(TaskCancelledError):
                    stream_download(
                        "https://example.com/v.mp4", str(Path(td) / "v.mp4"), cancel_event=ev
                    )


class ExtractAudioCancelTest(unittest.TestCase):
    """B1：ffmpeg 提取音频时取消 → terminate 子进程而非等 600s 超时。"""

    def test_cancel_terminates_process(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "in.mp4"
            src.write_bytes(b"fake")
            holder: dict = {}

            def _fake_popen(*_args, **_kwargs):
                proc = mock.Mock()
                proc.poll = mock.Mock(return_value=None)
                proc.terminate = mock.Mock()
                proc.wait = mock.Mock(return_value=0)
                holder["proc"] = proc
                return proc

            ev = threading.Event()
            ev.set()  # 已取消
            with mock.patch("subprocess.Popen", side_effect=_fake_popen):
                with self.assertRaises(TaskCancelledError):
                    _extract_audio_from_video(str(src), td, cancel_event=ev)
            # side_effect 返回值才是真实 proc（mock.return_value 是默认子 mock）
            holder["proc"].terminate.assert_called_once()


class TruncatedPropagationTest(unittest.TestCase):
    """B2：truncated 从 dict 透传到对象与笔记标注。"""

    def test_transcript_result_dict_carries_truncated(self):
        from dataclasses import asdict

        from app.models.transcriber_model import TranscriptResult

        d = asdict(TranscriptResult(language="zh", full_text="hi", segments=[], truncated=True))
        self.assertTrue(d["truncated"])
        # 默认（未截断）也要能序列化（result.json 契约）
        d2 = asdict(TranscriptResult(language="zh", full_text="hi", segments=[]))
        self.assertFalse(d2["truncated"])

    def test_summarize_appends_incomplete_note(self):
        from app.models.transcriber_model import TranscriptResult
        from app.services.note import NoteGenerator

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "gen" / "note.md"
            cache.parent.mkdir(parents=True)
            gen = NoteGenerator.__new__(NoteGenerator)  # 绕过 __init__ 的模块副作用
            gen._update_status = mock.Mock()
            gen.video_img_urls = []
            gen.video_path = None
            audio = mock.Mock(title="T", file_path="/x.mp3", raw_info={})
            tr = TranscriptResult(language="zh", full_text="hi", segments=[], truncated=True)
            with mock.patch(
                "app.services.pipeline.summarize_material", return_value="# 标题\n正文"
            ):
                out = gen._summarize_text(
                    audio_meta=audio,
                    transcript=tr,
                    gpt=mock.Mock(),
                    markdown_cache_file=cache,
                    link=False,
                    screenshot=False,
                    formats=[],
                    style=None,
                    extras=None,
                    video_img_urls=[],
                    comments_danmaku=None,
                    cancel_event=None,
                )
            self.assertIn("转写不完整", out)
            self.assertIn("转写不完整", cache.read_text(encoding="utf-8"))

    def test_summarize_untruncated_no_marker(self):
        from app.models.transcriber_model import TranscriptResult
        from app.services.note import NoteGenerator

        with tempfile.TemporaryDirectory() as td:
            cache = Path(td) / "gen" / "note.md"
            cache.parent.mkdir(parents=True)
            gen = NoteGenerator.__new__(NoteGenerator)
            gen._update_status = mock.Mock()
            gen.video_img_urls = []
            gen.video_path = None
            audio = mock.Mock(title="T", file_path="/x.mp3", raw_info={})
            tr = TranscriptResult(language="zh", full_text="hi", segments=[], truncated=False)
            with mock.patch(
                "app.services.pipeline.summarize_material", return_value="# 标题\n正文"
            ):
                out = gen._summarize_text(
                    audio_meta=audio,
                    transcript=tr,
                    gpt=mock.Mock(),
                    markdown_cache_file=cache,
                    link=False,
                    screenshot=False,
                    formats=[],
                    style=None,
                    extras=None,
                    video_img_urls=[],
                    comments_danmaku=None,
                    cancel_event=None,
                )
            self.assertNotIn("转写不完整", out)


class NoteCachePromoteUniqueTmpTest(unittest.TestCase):
    """B3：并发 promote 同一 ident 不再共用固定 <dst>.tmp（各自唯一后缀，replace 原子）。"""

    def test_promote_leaves_no_shared_tmp(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with mock.patch.object(note_cache, "cache_root", return_value=root):
                src = root / "src.json"
                src.write_text(
                    json.dumps(
                        {"segments": [{"start": 0, "end": 1, "text": "hi"}], "full_text": "hi"}
                    ),
                    encoding="utf-8",
                )
                # 两次 promote 同一 ident（模拟两个并发任务完成同一视频转写）
                note_cache.promote_transcript("bilibili", "https://www.bilibili.com/video/BV1", "BV1", "fast-whisper:base", src)
                note_cache.promote_transcript("bilibili", "https://www.bilibili.com/video/BV1", "BV1", "fast-whisper:base", src)
            self.assertEqual(list(root.rglob("*.tmp")), [])  # 无 tmp 残留
            cached = list(root.rglob("transcript_*.json"))
            self.assertEqual(len(cached), 1)  # 同一 ident 单条目，内容完整
            data = json.loads(cached[0].read_text(encoding="utf-8"))
            self.assertEqual(data["full_text"], "hi")


if __name__ == "__main__":
    unittest.main()
