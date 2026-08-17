"""pipeline 步骤层（app/services/pipeline.py）的单元测试。

不碰真实网络 / 转写引擎 / LLM / DB，全部用 mock + 临时目录。

运行：
    cd <repo>
    .venv/bin/python tests/test_pipeline_steps.py
"""
import base64
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services import pipeline


def _real_transcript() -> TranscriptResult:
    return TranscriptResult(
        language="zh",
        full_text="hello world",
        segments=[TranscriptSegment(start=0, end=5, text="hello")],
    )


class FetchSubtitlesTest(unittest.TestCase):
    def test_no_subtitles_returns_none(self):
        fake = mock.Mock()
        fake.download_subtitles.return_value = None
        with mock.patch.object(pipeline, "get_downloader", return_value=fake):
            result = pipeline.fetch_subtitles("https://www.bilibili.com/video/BV1xx", "bilibili")
        self.assertIsNone(result)

    def test_with_subtitles_returns_asdict(self):
        fake = mock.Mock()
        fake.download_subtitles.return_value = _real_transcript()
        with mock.patch.object(pipeline, "get_downloader", return_value=fake):
            result = pipeline.fetch_subtitles("https://www.bilibili.com/video/BV1xx", "bilibili")
        self.assertIsNotNone(result)
        self.assertEqual(result["full_text"], "hello world")
        self.assertEqual(result["segments"][0]["text"], "hello")
        self.assertEqual(result["language"], "zh")

    def test_downloader_exception_returns_none(self):
        fake = mock.Mock()
        fake.download_subtitles.side_effect = RuntimeError("boom")
        with mock.patch.object(pipeline, "get_downloader", return_value=fake):
            result = pipeline.fetch_subtitles("https://youtu.be/abc", "youtube")
        self.assertIsNone(result)


class TranscribeAudioTest(unittest.TestCase):
    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            pipeline.transcribe_audio("/no/such/file.mp3")

    def test_transcribes_and_returns_asdict(self):
        fake_transcriber = mock.Mock()
        fake_transcriber.transcript.return_value = _real_transcript()
        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            result = pipeline.transcribe_audio(f.name, transcriber=fake_transcriber)
        self.assertEqual(result["full_text"], "hello world")
        fake_transcriber.transcript.assert_called_once()
        # 未传 transcriber 时按配置构建
        with mock.patch.object(pipeline, "build_transcriber", return_value=fake_transcriber):
            with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
                result2 = pipeline.transcribe_audio(f.name)
        self.assertEqual(result2["language"], "zh")


class ExtractFramesTest(unittest.TestCase):
    def test_persists_frames_and_returns_file_uris(self):
        fake_reader = mock.Mock()
        fake_reader.run.return_value = [
            "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffA").decode(),
            "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffB").decode(),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "v.mp4"
            video.write_bytes(b"fake")
            save_dir = Path(tmp) / "frames"
            with mock.patch.object(pipeline, "VideoReader", return_value=fake_reader):
                frames = pipeline.extract_frames(str(video), video_interval=3, save_dir=save_dir)
            self.assertEqual(len(frames), 2)
            for uri in frames:
                self.assertTrue(uri.startswith("file://"))
                self.assertTrue(Path(uri[len("file://"):]).exists())

    def test_missing_video_raises(self):
        with self.assertRaises(FileNotFoundError):
            pipeline.extract_frames("/no/such/video.mp4")

    def test_skips_non_data_uri_frames(self):
        fake_reader = mock.Mock()
        fake_reader.run.return_value = ["not-a-data-uri"]
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "v.mp4"
            video.write_bytes(b"fake")
            with mock.patch.object(pipeline, "VideoReader", return_value=fake_reader):
                frames = pipeline.extract_frames(str(video), save_dir=Path(tmp) / "f")
        self.assertEqual(frames, [])


class FetchCommentsDanmakuTest(unittest.TestCase):
    def _patch_fetcher(self, danmaku_ok=True, comments_ok=True):
        fetcher_cls = mock.Mock()
        inst = fetcher_cls.return_value
        inst.fetch_danmaku.return_value = {"ok": danmaku_ok, "danmaku_summary": "【高密度】模型讲解" if danmaku_ok else None}
        inst.fetch_comments.return_value = {
            "ok": comments_ok,
            "comments": [{"user": "u1", "likes": 3, "content": "补充一个点"}],
        } if comments_ok else {"ok": False, "error": "no sessdata"}
        return mock.patch("app.downloaders.bilibili_comment.BilibiliCommentFetcher", fetcher_cls)

    def test_merges_danmaku_and_comments(self):
        with self._patch_fetcher():
            result = pipeline.fetch_comments_danmaku("https://www.bilibili.com/video/BV1xx", comments_limit=5)
        self.assertIsNotNone(result)
        self.assertIn("【弹幕】", result)
        self.assertIn("【热门评论】", result)
        self.assertIn("u1(3赞)", result)

    def test_all_fail_returns_none(self):
        with self._patch_fetcher(danmaku_ok=False, comments_ok=False):
            result = pipeline.fetch_comments_danmaku("https://www.bilibili.com/video/BV1xx")
        self.assertIsNone(result)


class SummarizeMaterialTest(unittest.TestCase):
    def _material(self, frames=None):
        return {
            "title": "测试标题",
            "transcript": {
                "language": "zh",
                "full_text": "hello world",
                "segments": [{"start": 0, "end": 5, "text": "hello"}],
            },
            "frames": frames or [],
            "comments_danmaku": "【弹幕】…",
            "video_path": None,
            "audio_path": None,
        }

    def test_returns_markdown_and_builds_gpt_source(self):
        fake_gpt = mock.Mock()
        fake_gpt.summarize.return_value = "# 笔记\n内容"
        result = pipeline.summarize_material(self._material(), fake_gpt, style="detailed")
        self.assertEqual(result, "# 笔记\n内容")
        _, kwargs = fake_gpt.summarize.call_args
        source = kwargs.get("source") or fake_gpt.summarize.call_args[0][0]
        self.assertEqual(source.title, "测试标题")
        self.assertEqual(source.comments_danmaku, "【弹幕】…")
        self.assertEqual(source.style, "detailed")
        self.assertEqual(len(source.segment), 1)
        self.assertEqual(source.segment[0].text, "hello")

    def test_data_uri_frames_pass_through(self):
        data_uri = "data:image/jpeg;base64," + base64.b64encode(b"img").decode()
        fake_gpt = mock.Mock()
        fake_gpt.summarize.return_value = "x"
        pipeline.summarize_material(self._material(frames=[data_uri]), fake_gpt)
        source = fake_gpt.summarize.call_args[0][0]
        self.assertEqual(source.video_img_urls, [data_uri])

    def test_file_uri_frames_converted_to_base64(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "frame_1.jpg"
            p.write_bytes(b"\xff\xd8\xffimage")
            fake_gpt = mock.Mock()
            fake_gpt.summarize.return_value = "x"
            pipeline.summarize_material(self._material(frames=[p.as_uri()]), fake_gpt)
            source = fake_gpt.summarize.call_args[0][0]
            self.assertEqual(len(source.video_img_urls), 1)
            self.assertTrue(source.video_img_urls[0].startswith("data:image/jpeg;base64,"))

    def test_empty_material_is_safe(self):
        fake_gpt = mock.Mock()
        fake_gpt.summarize.return_value = "x"
        result = pipeline.summarize_material({}, fake_gpt)
        self.assertEqual(result, "x")


class TestYoutubeCookie(unittest.TestCase):
    """docs/05 #34：YouTube 下载器吃 setup ③ 填的 youtube cookie（Netscape 文件）。"""

    def _dl(self, cookie, tmp_name):
        import app.downloaders.youtube_downloader as mod

        with mock.patch.object(mod.CookieConfigManager, "get", return_value=cookie):
            fake = mock.Mock()
            fake.name = tmp_name
            fake.close = mock.Mock()
            with mock.patch("app.downloaders.youtube_downloader.tempfile") as m_tmp:
                m_tmp.NamedTemporaryFile.return_value = fake
                dl = mod.YoutubeDownloader()
        return dl, fake

    def test_no_cookie_no_file(self):
        import app.downloaders.youtube_downloader as mod

        with mock.patch.object(mod.CookieConfigManager, "get", return_value=""):
            dl = mod.YoutubeDownloader()
        self.assertIsNone(dl._cookiefile)

    def test_cookie_writes_netscape(self):
        tmp = tempfile.mktemp(suffix=".txt")
        dl, fake = self._dl("LOGIN_INFO=abc; SID=def", tmp)
        self.assertEqual(dl._cookiefile, tmp)
        content = fake.writelines.call_args[0][0]
        self.assertTrue(any(".youtube.com" in line and "LOGIN_INFO" in line for line in content))
        self.assertTrue(any(".youtube.com" in line and "SID" in line for line in content))
        dl._cleanup_cookie_file()
        self.assertFalse(Path(tmp).exists())

    def test_download_injects_cookiefile(self):
        import app.downloaders.youtube_downloader as mod

        tmp = tempfile.mktemp(suffix=".txt")
        dl, _ = self._dl("SID=x", tmp)
        ydl = mock.Mock()
        ydl.__enter__ = mock.Mock(return_value=ydl)
        ydl.__exit__ = mock.Mock(return_value=False)
        ydl.extract_info.return_value = {"id": "vid1", "title": "t", "duration": 1, "ext": "m4a"}
        captured = {}

        def _fake_ydl(opts):
            captured.update(opts)
            return ydl

        with mock.patch("app.downloaders.youtube_downloader.yt_dlp.YoutubeDL", side_effect=_fake_ydl), \
             mock.patch.object(mod, "get_data_dir", return_value=tempfile.gettempdir()):
            dl.download("https://youtu.be/vid1")
        self.assertEqual(captured["cookiefile"], tmp)
        dl._cleanup_cookie_file()


if __name__ == "__main__":
    unittest.main(verbosity=2)
