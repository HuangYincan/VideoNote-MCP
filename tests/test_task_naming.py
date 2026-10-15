"""Agent naming writes only the display title and is consistent across task views."""
import json
import uuid
from unittest import mock

import pytest

from videonote_mcp import guidance, server
from videonote_mcp.task_details import read_task_details


@pytest.fixture
def named_task(tmp_path, monkeypatch):
    from app.db.video_task_dao import delete_task, insert_video_task

    task_id = uuid.uuid4().hex
    monkeypatch.setattr(server, "NOTE_OUTPUT_DIR", tmp_path)
    directory = tmp_path / task_id
    (directory / "gen").mkdir(parents=True)
    (directory / "status.json").write_text('{"status":"SUCCESS"}', encoding="utf-8")
    result = {"kind": "material", "title": "原视频标题", "transcript": {"full_text": "自注意力机制"}}
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    (directory / "source.json").write_text(json.dumps({
        "kind": "material", "platform": "youtube", "input": "https://youtube.com/watch?v=abcdefghijk",
    }), encoding="utf-8")
    insert_video_task("abcdefghijk", "youtube", task_id, title="原视频标题", status="SUCCESS",
                      summary="素材简介", note_dir=str(directory))
    yield task_id, directory, tmp_path
    delete_task(task_id)


def test_agent_name_updates_all_views_and_preserves_artifacts(named_task):
    task_id, directory, root = named_task
    before_files = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    before = next(item for item in json.loads(server.list_tasks()) if item["task_id"] == task_id)
    title = "Transformer 自注意力机制讲解"
    renamed = json.loads(server.task(task_id, action="rename", title=f"  {title}  "))
    assert renamed == {"ok": True, "task_id": task_id, "title": title}
    after = next(item for item in json.loads(server.list_tasks()) if item["task_id"] == task_id)
    assert after == dict(before, title=title)
    assert json.loads(server.task(task_id))["result"]["title"] == title
    assert read_task_details(after, root)["title"] == title
    assert before_files == {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    assert json.loads(server.task(task_id, action="transcript"))["full_text"] == "自注意力机制"
    # Names can be revised and reused; the immutable task ID disambiguates them.
    assert json.loads(server.task(task_id, action="rename", title="自注意力：原理与应用"))["ok"]


@pytest.mark.parametrize("title", ["", " \n\t ", "a" * 121, "多行\n标题", "含\x1b控制字符", "含\x7f控制字符", None])
def test_invalid_names_do_not_change_index(named_task, title):
    from app.db.video_task_dao import get_task_title

    task_id, _, _ = named_task
    with pytest.raises(ValueError, match="任务名"):
        server.task(task_id, action="rename", title=title)
    assert get_task_title(task_id) == "原视频标题"


@pytest.mark.parametrize("status", ["PENDING", "TRANSCRIBING", "SAVING", "FAILED", "CANCELLED", "UNKNOWN"])
def test_unfinished_or_unsuccessful_tasks_cannot_be_renamed(named_task, status):
    from app.db.video_task_dao import get_task_title

    task_id, directory, _ = named_task
    (directory / "status.json").write_text(json.dumps({"status": status}), encoding="utf-8")
    result = json.loads(server.task(task_id, action="rename", title="新名称"))
    assert not result["ok"] and result["status"] == status
    assert get_task_title(task_id) == "原视频标题"


def test_deleted_index_is_not_recreated_by_rename(named_task):
    from app.db.video_task_dao import delete_task, get_task_title

    task_id, _, _ = named_task
    delete_task(task_id)
    result = json.loads(server.task(task_id, action="rename", title="新名称"))
    assert not result["ok"] and result["status"] == "NOT_FOUND"
    assert get_task_title(task_id) is None


def test_unknown_task_and_path_traversal_cannot_create_index(named_task):
    from app.db.video_task_dao import get_task_title

    result = json.loads(server.task("missing-task", action="rename", title="新名称"))
    assert not result["ok"] and get_task_title("missing-task") is None
    with pytest.raises(ValueError, match="非法 task_id"):
        server.task("../outside", action="rename", title="新名称")


def test_database_failure_is_reported_without_success(named_task):
    task_id, _, _ = named_task
    with mock.patch("app.db.video_task_dao.update_task_title", side_effect=RuntimeError("read only database")):
        with pytest.raises(ValueError, match="保存任务名失败"):
            server.task(task_id, action="rename", title="新名称")


def test_guidance_and_material_result_require_naming_after_writing(named_task):
    task_id, _, _ = named_task
    assert "写完笔记后必须" in guidance.SERVER_INSTRUCTIONS
    assert "action='rename'" in guidance.SERVER_INSTRUCTIONS
    assert 'action="rename"' in server.prepare_note_material.__doc__
    result = json.loads(server.task(task_id))
    assert "写完后" in result["next_steps"][0] and "笔记标题" in result["next_steps"][0]
    assert task_id in result["next_steps"][0] and "action='rename'" in result["next_steps"][0]


def test_legacy_task_without_index_still_returns_its_original_title(named_task):
    from app.db.video_task_dao import delete_task

    task_id, _, _ = named_task
    delete_task(task_id)
    assert json.loads(server.task(task_id))["result"]["title"] == "原视频标题"
