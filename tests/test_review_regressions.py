"""#148 全库审查回归：状态、任务生命周期、取消传播与 SQLite 一致性。"""
import json
import shutil
import uuid
from unittest import mock

import pytest

from app.db.engine import SessionLocal, get_engine
from app.db.init_db import init_db
from app.db.model_dao import insert_model
from app.db.models.models import Model
from app.db.models.providers import Provider
from app.db.provider_dao import delete_provider
from app.enmus.note_enums import DownloadQuality
from app.enmus.task_status_enums import TaskStatus
from app.exceptions.task import TaskCancelledError
from app.services.note import NoteGenerator
from app.utils.task_manifest import manifest_path
from videonote_mcp import server


def _bare_generator() -> NoteGenerator:
    """构造只用于单步测试的 NoteGenerator，避免加载真实转写模型。"""
    generator = object.__new__(NoteGenerator)
    generator._update_status = mock.Mock()
    generator._handle_exception = mock.Mock()
    generator.transcriber = object()
    generator.transcriber_type = "test"
    generator.model_size = "small"
    generator.video_path = None
    generator.video_img_urls = []
    return generator


@pytest.mark.parametrize("payload", [[], "not-a-status-object", 42])
def test_task_status_non_mapping_json_root_is_unknown(payload):
    """合法但非 object 的 status.json 不能触发 dict.get()，也不能伪装成 PENDING。"""
    task_id = f"non-mapping-status-{uuid.uuid4().hex}"
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    try:
        (task_dir / "status.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )
        with server._tasks_lock:
            server._status_memory.pop(task_id, None)

        result = json.loads(server.task(task_id))

        assert result["status"] == "UNKNOWN"
        assert result["stage"] == "状态未知"
        assert "无法确认" in result["message"]
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        with server._tasks_lock:
            server._status_memory.pop(task_id, None)


@pytest.mark.parametrize("strict", [False, True], ids=["best-effort", "strict"])
def test_write_status_file_failure_does_not_update_sqlite(strict):
    """状态文件失败时，SQLite 不能先行；strict 只改变是否重新抛错。"""
    task_id = f"status-write-failure-{uuid.uuid4().hex}"
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    try:
        with mock.patch(
            "app.utils.json_store._write_bytes_with_mode",
            side_effect=OSError("disk full"),
        ), mock.patch("app.db.video_task_dao.update_task_status") as update_status:
            if strict:
                with pytest.raises(OSError, match="disk full"):
                    server._write_status(
                        task_id, TaskStatus.PENDING, message="排队中", strict=True
                    )
            else:
                assert (
                    server._write_status(
                        task_id, TaskStatus.PENDING, message="排队中"
                    )
                    is False
                )
            update_status.assert_not_called()
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        with server._tasks_lock:
            server._status_memory.pop(task_id, None)


@pytest.mark.parametrize("entrypoint", ["generate", "material"])
def test_task_submit_failure_rolls_back_task_files_and_index(entrypoint, monkeypatch):
    """索引/状态已创建但 submit 失败时，不留下目录、manifest 或全局索引。"""
    submitted_ids = []

    class FailingPool:
        def submit(self, _fn, task_id, *_args, **_kwargs):
            submitted_ids.append(task_id)
            raise RuntimeError("executor unavailable")

    monkeypatch.setattr(server, "_pool", FailingPool())
    monkeypatch.setattr(server, "_MAX_WORKERS", 10_000)
    monkeypatch.setattr(server, "_guard_remote_url", lambda *_args: None)
    monkeypatch.setattr(server, "get_app_config", dict)

    kwargs = {
        "video_url": "https://example.com/video",
        "platform": "generic",
        "video_understanding": False,
        "video_interval": 0,
        "include_comments": False,
        "comments_limit": 20,
        "grid_size": [],
    }
    if entrypoint == "generate":
        kwargs.update(
            {
                "provider_id": "review-provider",
                "model_name": "review-model",
                "format": [],
                "style": "detailed",
                "screenshot": False,
            }
        )
        monkeypatch.setattr(
            server.ProviderService,
            "get_provider_by_id",
            lambda _provider_id: {"api_key": "review-key"},
        )

    with pytest.raises(RuntimeError, match="executor unavailable"):
        if entrypoint == "generate":
            server.generate_note(**kwargs)
        else:
            server.prepare_note_material(**kwargs)

    assert len(submitted_ids) == 1
    task_id = submitted_ids[0]
    assert not (server.NOTE_OUTPUT_DIR / task_id).exists()
    assert not manifest_path(task_id).exists()

    from app.db.video_task_dao import list_tasks

    assert task_id not in {row["task_id"] for row in list_tasks()}
    with server._tasks_lock:
        assert task_id not in server._task_futures
        assert task_id not in server._task_events
        assert task_id not in server._status_memory


def test_submit_failure_preserves_original_error_when_cleanup_reports_failures(monkeypatch):
    """Cleanup diagnostics must be attached without replacing executor failure."""
    submitted_ids = []

    class FailingPool:
        def submit(self, _fn, task_id, *_args, **_kwargs):
            submitted_ids.append(task_id)
            raise RuntimeError("executor unavailable")

    monkeypatch.setattr(server, "_pool", FailingPool())
    monkeypatch.setattr(server, "_MAX_WORKERS", 10_000)
    monkeypatch.setattr(server, "_guard_remote_url", lambda *_args: None)
    monkeypatch.setattr(server, "get_app_config", dict)
    try:
        with mock.patch(
            "app.utils.task_manifest._delete_all",
            return_value={
                "deleted": [],
                "missing": [],
                "errors": [{"path": "task-dir", "error": "permission denied"}],
            },
        ), mock.patch(
            "app.db.video_task_dao.delete_task",
            side_effect=RuntimeError("index unavailable"),
        ):
            with pytest.raises(RuntimeError, match="executor unavailable") as caught:
                server.prepare_note_material(
                    "https://example.com/video",
                    platform="generic",
                    video_understanding=False,
                    video_interval=0,
                    include_comments=False,
                    comments_limit=20,
                    grid_size=[],
                )
        notes = "\n".join(caught.value.__notes__ or [])
        assert "index unavailable" in notes
        assert "permission denied" in notes
    finally:
        if submitted_ids:
            shutil.rmtree(server.NOTE_OUTPUT_DIR / submitted_ids[0], ignore_errors=True)


def test_cleanup_task_files_reports_index_failure_after_removing_files(monkeypatch):
    """Per-task cleanup exposes DAO failure while still deleting task files."""
    task_id = f"cleanup-index-{uuid.uuid4().hex}"
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "result.json").write_text("{}", encoding="utf-8")
    (task_dir / "manifest.json").write_text("{}", encoding="utf-8")
    try:
        with mock.patch("app.db.video_task_dao.delete_task", side_effect=RuntimeError("index unavailable")):
            result = server.cleanup_task_files(task_id, include_note=True)
        assert result["index_error"] == "index unavailable"
        assert not task_dir.exists()
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)


def test_get_transcript_propagates_cancellation_without_fallback(tmp_path):
    generator = _bare_generator()
    fallback = mock.Mock()
    generator._transcribe_audio = fallback

    with mock.patch(
        "app.services.note.pipeline.fetch_subtitles",
        side_effect=TaskCancelledError("cancelled"),
    ), pytest.raises(TaskCancelledError):
        generator._get_transcript(
            downloader=mock.Mock(),
            video_url="https://example.com/video",
            audio_file="/tmp/audio.mp3",
            transcript_cache_file=tmp_path / "task" / "gen" / "transcript.json",
            status_phase=TaskStatus.TRANSCRIBING,
            task_id="transcript-cancel",
        )

    fallback.assert_not_called()
    generator._handle_exception.assert_not_called()


def test_transcribe_audio_propagates_cancellation_without_failure_handler(tmp_path):
    generator = _bare_generator()
    cache_file = tmp_path / "task" / "gen" / "transcript.json"

    with mock.patch(
        "app.services.note.pipeline.transcribe_audio",
        side_effect=TaskCancelledError("cancelled"),
    ), pytest.raises(TaskCancelledError):
        generator._transcribe_audio(
            audio_file="/tmp/audio.mp3",
            transcript_cache_file=cache_file,
            status_phase=TaskStatus.TRANSCRIBING,
        )

    generator._handle_exception.assert_not_called()


def test_download_media_propagates_cancellation_without_fallback(tmp_path):
    task_id = f"download-cancel-{uuid.uuid4().hex}"
    task_dir = server.NOTE_OUTPUT_DIR / task_id
    cache_file = task_dir / "gen" / "audio.json"
    generator = _bare_generator()
    downloader = mock.Mock()
    downloader.download.side_effect = TaskCancelledError("cancelled")

    try:
        with pytest.raises(TaskCancelledError):
            generator._download_media(
                downloader=downloader,
                video_url="https://example.com/video",
                quality=DownloadQuality.medium,
                audio_cache_file=cache_file,
                status_phase=TaskStatus.DOWNLOADING,
                platform="generic",
                output_path=None,
                screenshot=False,
                video_understanding=False,
                video_interval=0,
                grid_size=[],
                skip_download=True,
            )
        generator._handle_exception.assert_not_called()
    finally:
        shutil.rmtree(task_dir, ignore_errors=True)
        manifest_path(task_id).unlink(missing_ok=True)


def test_insert_model_rejects_unknown_provider():
    provider_id = f"missing-provider-{uuid.uuid4().hex}"
    with pytest.raises(ValueError, match="供应商不存在"):
        insert_model(provider_id, "review-model")


def test_delete_provider_removes_associated_models():
    provider_id = f"delete-provider-{uuid.uuid4().hex}"
    db = SessionLocal()
    db.add(
        Provider(
            id=provider_id,
            name="待删除供应商",
            logo="",
            type="test",
            api_key="",
            base_url="",
            enabled=0,
        )
    )
    db.commit()
    db.close()

    try:
        insert_model(provider_id, "associated-model")
        delete_provider(provider_id)

        check = SessionLocal()
        try:
            assert check.query(Provider).filter_by(id=provider_id).first() is None
            assert check.query(Model).filter_by(provider_id=provider_id).first() is None
        finally:
            check.close()
    finally:
        cleanup = SessionLocal()
        try:
            for model in cleanup.query(Model).filter_by(provider_id=provider_id).all():
                cleanup.delete(model)
            provider = cleanup.query(Provider).filter_by(id=provider_id).first()
            if provider is not None:
                cleanup.delete(provider)
            cleanup.commit()
        finally:
            cleanup.close()


def test_sqlite_connections_enable_foreign_keys():
    with get_engine().connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1


def test_video_tasks_created_at_index_exists():
    init_db()
    with get_engine().connect() as connection:
        indexes = connection.exec_driver_sql("PRAGMA index_list(video_tasks)").all()
    assert any(row[1] == "ix_video_tasks_created_at" for row in indexes)
