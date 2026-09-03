"""协作式取消在流水线边界的契约测试。"""
import tempfile
import threading
import unittest
from unittest import mock

from app.exceptions.task import TaskCancelledError
from app.services import pipeline
from app.utils.video_reader import VideoReader


class PipelineCancellationContractTest(unittest.TestCase):
    def test_fetch_subtitles_checks_before_downloader(self):
        event = threading.Event()
        event.set()
        with mock.patch.object(pipeline, "get_downloader") as get_downloader:
            with self.assertRaises(TaskCancelledError):
                pipeline.fetch_subtitles("https://example.com/video", "generic", event)
        get_downloader.assert_not_called()

    def test_transcribe_audio_checks_before_transcriber(self):
        event = threading.Event()
        event.set()
        with tempfile.NamedTemporaryFile(suffix=".mp3") as media:
            transcriber = mock.Mock()
            with self.assertRaises(TaskCancelledError):
                pipeline.transcribe_audio(media.name, transcriber=transcriber, cancel_event=event)
        transcriber.transcript.assert_not_called()

    def test_preprocess_propagates_cancel_between_chunks(self):
        event = threading.Event()
        transcriber = mock.Mock()

        def transcribe(*, file_path, cancel_event=None):
            event.set()
            raise TaskCancelledError("cancelled")

        transcriber.transcript.side_effect = transcribe
        with mock.patch.object(pipeline, "chunk_duration_guess", return_value=1.0), \
             mock.patch(
                 "app.transcriber.audio_preprocess.normalize_to_wav",
                 return_value="normalized.wav",
             ), \
             mock.patch(
                 "app.transcriber.audio_preprocess.chunk_if_long",
                 return_value=["part-1", "part-2"],
             ):
            with self.assertRaises(TaskCancelledError):
                pipeline._transcribe_with_preprocess(
                    "source.mp3", transcriber, cancel_event=event
                )
        transcriber.transcript.assert_called_once()

    def test_video_reader_checks_before_frame_work(self):
        event = threading.Event()
        event.set()
        reader = VideoReader("/missing.mp4", cancel_event=event)
        with self.assertRaises(TaskCancelledError):
            reader.run()


class FfmpegCancellationContractTest(unittest.TestCase):
    def test_ffmpeg_checks_event_before_starting_process(self):
        from app.downloaders.common import run_ffmpeg_cancellable

        event = threading.Event()
        event.set()
        with mock.patch("app.downloaders.common.subprocess.Popen") as popen:
            with self.assertRaises(TaskCancelledError):
                run_ffmpeg_cancellable(["ffmpeg", "-version"], cancel_event=event)
        popen.assert_not_called()

    def test_stream_download_removes_partial_output_on_cancel(self):
        from pathlib import Path

        from app.downloaders import common

        event = threading.Event()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.__exit__.return_value = None

        def iter_content(_chunk_size):
            event.set()
            yield b"partial"

        response.iter_content.side_effect = iter_content
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "audio.mp3"
            with mock.patch.object(common, "_follow_redirects_public", return_value=response), \
                 mock.patch.object(common, "assert_public_http_url"):
                with self.assertRaises(TaskCancelledError):
                    common.stream_download(
                        "https://example.com/audio.mp3",
                        str(output),
                        cancel_event=event,
                    )
            self.assertFalse(output.exists())


class CommentsCancellationContractTest(unittest.TestCase):
    def test_fetch_comments_danmaku_checks_before_network(self):
        event = threading.Event()
        event.set()
        with mock.patch("app.downloaders.bilibili_comment.BilibiliCommentFetcher") as fetcher_cls:
            with self.assertRaises(TaskCancelledError):
                pipeline.fetch_comments_danmaku(
                    "https://www.bilibili.com/video/BV1xx", cancel_event=event
                )
        fetcher_cls.assert_not_called()

    def test_fetch_comments_danmaku_does_not_swallow_cancel(self):
        event = threading.Event()
        fetcher_cls = mock.Mock()
        inst = fetcher_cls.return_value
        inst.fetch_danmaku.side_effect = TaskCancelledError("任务已取消")
        with mock.patch("app.downloaders.bilibili_comment.BilibiliCommentFetcher", fetcher_cls):
            with self.assertRaises(TaskCancelledError):
                pipeline.fetch_comments_danmaku(
                    "https://www.bilibili.com/video/BV1xx", cancel_event=event
                )
        inst.fetch_comments.assert_not_called()


if __name__ == "__main__":
    unittest.main()
