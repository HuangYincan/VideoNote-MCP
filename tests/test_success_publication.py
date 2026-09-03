"""SUCCESS 发布顺序回归测试。

MCP worker 负责把 NoteGenerator 的结果和任务 manifest 落盘；只有两者完成后
才应把 SUCCESS 暴露给轮询方。
"""
import json
import shutil
import uuid
from pathlib import Path
from unittest import mock

import pytest

import videonote_mcp.server as server
from app.models.audio_model import AudioDownloadResult
from app.models.notes_model import NoteResult
from app.models.transcriber_model import TranscriptResult
from app.services.note import NoteGenerator
from app.utils.task_manifest import manifest_path


def _audio_meta():
    return AudioDownloadResult(
        file_path="fixture-success.mp3",
        title="测试视频",
        duration=3.0,
        cover_url=None,
        platform="local",
        video_id="local-success",
        raw_info={},
    )


def _transcript():
    return TranscriptResult(language="zh", full_text="测试文本", segments=[])


def _note_result(material=False):
    audio = _audio_meta()
    transcript = _transcript()
    if material:
        return NoteResult(
            markdown="",
            transcript=transcript,
            audio_meta=audio,
            material={
                "title": audio.title,
                "transcript": {
                    "language": transcript.language,
                    "full_text": transcript.full_text,
                    "segments": [],
                },
                "frames": [],
                "comments_danmaku": None,
                "video_path": None,
                "audio_path": audio.file_path,
            },
        )
    return NoteResult(markdown="# 测试\n正文", transcript=transcript, audio_meta=audio)


@pytest.mark.parametrize("material", [False, True], ids=["normal", "material"])
def test_run_note_task_publishes_success_after_result_and_manifest(material):
    """普通任务和 material 任务都必须 result -> manifest -> SUCCESS。"""
    task_id = f"success-order-{uuid.uuid4().hex}"
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    events = []
    strict_manifest_calls = []
    real_atomic_write = server._atomic_write_json
    real_record_paths = server.record_task_paths
    real_write_status = server._write_status

    def atomic_write(path, payload):
        real_atomic_write(path, payload)
        if Path(path).name == "result.json":
            events.append("result")

    def record_paths(tid, paths, **kwargs):
        strict_manifest_calls.append(kwargs.get("strict"))
        real_record_paths(tid, paths, **kwargs)
        if tid == task_id and any(Path(p).name == "result.json" for p in paths):
            events.append("manifest")

    def write_status(tid, status, message=None, **kwargs):
        value = status.value if hasattr(status, "value") else str(status)
        if tid == task_id and value == "SUCCESS":
            assert (task_dir / "result.json").is_file()
            assert manifest_path(task_id).is_file()
            events.append("SUCCESS")
        return real_write_status(tid, status, message, **kwargs)

    try:
        with mock.patch.object(server, "NoteGenerator") as generator_cls, \
             mock.patch.object(server, "_atomic_write_json", side_effect=atomic_write), \
             mock.patch.object(server, "record_task_paths", side_effect=record_paths), \
             mock.patch.object(server, "_write_status", side_effect=write_status), \
             mock.patch.object(server, "_auto_export_transcript"):
            generator_cls.return_value.generate.return_value = _note_result(material)
            server._run_note_task(task_id)

        assert events[-3:] == ["result", "manifest", "SUCCESS"]
        assert strict_manifest_calls == [True]
        persisted_manifest = json.loads(manifest_path(task_id).read_text(encoding="utf-8"))
        assert str(task_dir / "result.json") in persisted_manifest["paths"]
        assert str(task_dir / "status.json") in persisted_manifest["paths"]
        assert events.index("SUCCESS") > events.index("manifest")
        generator_cls.return_value.generate.assert_called_once()
        assert generator_cls.return_value.generate.call_args.kwargs["publish_success"] is False
        payload = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
        if material:
            assert payload["kind"] == "material"
        else:
            assert "markdown" in payload
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        with server._tasks_lock:
            server._status_memory.pop(task_id, None)


def test_run_note_task_manifest_failure_does_not_publish_success():
    """最终 manifest 严格写入失败时，任务必须 FAILED 而不是 SUCCESS。"""
    task_id = f"manifest-failure-{uuid.uuid4().hex}"
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    statuses = []

    def write_status(tid, status, message=None, **kwargs):
        value = status.value if hasattr(status, "value") else str(status)
        if tid == task_id:
            statuses.append(value)
        return real_write_status(tid, status, message, **kwargs)

    real_write_status = server._write_status
    try:
        with mock.patch.object(server, "NoteGenerator") as generator_cls, \
             mock.patch.object(server, "record_task_paths", side_effect=OSError("disk full")), \
             mock.patch.object(server, "_write_status", side_effect=write_status), \
             mock.patch.object(server, "_auto_export_transcript"):
            generator_cls.return_value.generate.return_value = _note_result()
            server._run_note_task(task_id)

        assert "SUCCESS" not in statuses
        assert statuses[-1] == "FAILED"
        assert json.loads((task_dir / "status.json").read_text(encoding="utf-8"))["status"] == "FAILED"
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        with server._tasks_lock:
            server._status_memory.pop(task_id, None)


def test_run_note_task_late_cancellation_does_not_publish_success():
    """生成器返回后若取消，worker 不应发布 SUCCESS。"""
    import threading

    task_id = f"late-cancel-{uuid.uuid4().hex}"
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    cancel_event = threading.Event()

    def generate(**_kwargs):
        cancel_event.set()
        return _note_result()

    try:
        with mock.patch.object(server, "NoteGenerator") as generator_cls, \
             mock.patch.object(server, "_auto_export_transcript"):
            generator_cls.return_value.generate.side_effect = generate
            server._run_note_task(task_id, cancel_event)

        status = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
        assert status["status"] == "CANCELLED"
        assert not (task_dir / "result.json").exists()
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        with server._tasks_lock:
            server._status_memory.pop(task_id, None)


@pytest.mark.parametrize("material_only", [False, True], ids=["normal", "material"])
def test_generate_publish_success_false_does_not_publish_success(material_only):
    """publish_success=False 只返回结果，不让 NoteGenerator 发布 SUCCESS。"""
    task_id = f"generate-no-success-{uuid.uuid4().hex}"
    generator = NoteGenerator()
    audio = _audio_meta()
    transcript = _transcript()
    task_dir = server.NOTE_OUTPUT_DIR / task_id

    try:
        with mock.patch("app.services.note._new_downloader") as get_downloader, \
             mock.patch.object(generator, "_get_gpt", return_value=mock.Mock()), \
             mock.patch.object(generator, "_download_media", return_value=audio), \
             mock.patch.object(generator, "_get_transcript", return_value=transcript), \
             mock.patch.object(generator, "_save_metadata") as save_metadata, \
             mock.patch.object(generator, "_build_note_material", return_value={
                 "title": audio.title,
                 "transcript": {
                     "language": transcript.language,
                     "full_text": transcript.full_text,
                     "segments": [],
                 },
                 "frames": [],
                 "comments_danmaku": None,
                 "video_path": None,
                 "audio_path": audio.file_path,
             }), \
             mock.patch.object(generator, "_summarize_text", return_value="# 测试\n正文"), \
             mock.patch("app.services.note.note_cache.promote_transcript"), \
             mock.patch("app.services.note.note_cache.promote_media"):
            get_downloader.return_value.download_subtitles.return_value = transcript
            result = generator.generate(
                video_url="https://example.com/video",
                platform="local",
                task_id=task_id,
                material_only=material_only,
                publish_success=False,
            )

        assert result is not None
        assert json.loads((task_dir / "status.json").read_text(encoding="utf-8"))["status"] != "SUCCESS"
        assert save_metadata.call_args.kwargs["status"] == ""
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
