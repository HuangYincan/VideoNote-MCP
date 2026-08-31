"""第 19 轮安全结果边界回归测试（#143 A1）。"""

import json
import shutil
import tempfile
from unittest import mock

from app.downloaders.bilibili_downloader import BilibiliDownloader
from app.models.audio_model import AudioDownloadResult, safe_audio_download_result_dict
from app.models.notes_model import NoteResult
from videonote_mcp import server


class _YdlContext:
    def __init__(self, info):
        self.info = info

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, *args, **kwargs):
        return self.info


def test_bilibili_downloader_does_not_keep_raw_yt_dlp_info():
    info = {
        "id": "BV1safe",
        "title": "安全测试",
        "duration": 3,
        "tags": ["tag", 42],
        "url": "https://cdn.example/video?token=secret",
        "http_headers": {"Cookie": "SESSDATA=secret"},
        "formats": [{"url": "https://cdn.example/format?sig=secret"}],
    }
    with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
        BilibiliDownloader, "_write_netscape_cookie_file", return_value=None
    ), mock.patch(
        "app.downloaders.bilibili_downloader.yt_dlp.YoutubeDL",
        return_value=_YdlContext(info),
    ):
        result = BilibiliDownloader().download(
            "https://www.bilibili.com/video/BV1safe",
            output_dir=tmp,
            skip_download=True,
        )

    assert result.raw_info == {"tags": ["tag"]}
    assert "secret" not in json.dumps(result.raw_info)

    cached = safe_audio_download_result_dict(
        AudioDownloadResult(
            file_path="/tmp/a.mp3",
            title="t",
            duration=1,
            cover_url=None,
            platform="bilibili",
            video_id="BV1safe",
            raw_info=info,
        )
    )
    assert cached["raw_info"] == {"tags": ["tag"]}
    assert "secret" not in json.dumps(cached)


def test_run_note_task_projects_audio_metadata_before_persisting():
    task_id = "public_audio_meta"
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    audio = AudioDownloadResult(
        file_path="/tmp/a.mp3",
        title="测试视频",
        duration=3,
        cover_url="https://cdn.example/cover?token=secret",
        platform="bilibili",
        video_id="BV1safe",
        raw_info={
            "url": "https://cdn.example/audio?sig=secret",
            "http_headers": {"Cookie": "SESSDATA=secret"},
            "tags": ["tag"],
        },
    )
    result = NoteResult(markdown="# note", transcript=None, audio_meta=audio)
    try:
        with mock.patch.object(server, "NoteGenerator") as generator, mock.patch.object(
            server, "_auto_export_transcript"
        ), mock.patch.object(server, "record_task_paths"):
            generator.return_value.generate.return_value = result
            server._run_note_task(task_id)

        payload = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
        public_meta = payload["audio_meta"]
        assert public_meta == {
            "file_path": "/tmp/a.mp3",
            "title": "测试视频",
            "duration": 3,
            "platform": "bilibili",
            "video_id": "BV1safe",
            "video_path": None,
        }
        assert "secret" not in json.dumps(payload)
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


def test_task_status_projects_legacy_audio_metadata():
    task_id = "legacy_audio_meta"
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        server._write_status(task_id, "SUCCESS", message="完成")
        (task_dir / "result.json").write_text(
            json.dumps(
                {
                    "title": "旧任务",
                    "markdown": "# note",
                    "note_dir": str(task_dir),
                    "audio_meta": {
                        "file_path": "/tmp/a.mp3",
                        "title": "旧任务",
                        "duration": 1,
                        "platform": "bilibili",
                        "video_id": "BV1safe",
                        "raw_info": {
                            "url": "https://cdn.example/a?token=secret",
                            "http_headers": {"Authorization": "Bearer secret"},
                        },
                        "unknown": "not public",
                    },
                }
            ),
            encoding="utf-8",
        )
        payload = json.loads(server._task_status(task_id))
        assert payload["result"]["audio_meta"] == {
            "file_path": "/tmp/a.mp3",
            "title": "旧任务",
            "duration": 1,
            "platform": "bilibili",
            "video_id": "BV1safe",
        }
        assert "secret" not in json.dumps(payload)
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
