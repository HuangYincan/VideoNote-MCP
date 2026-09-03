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


if __name__ == "__main__":
    unittest.main()
