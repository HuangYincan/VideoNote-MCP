"""Setup task browsing with real disk artifacts; no network, ASR or LLM calls."""
import json
from unittest import mock

import pytest

from videonote_mcp import cli
from videonote_mcp.task_details import read_task_details, transcript_text


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def task(tmp_path, monkeypatch):
    root = tmp_path / "note_results"
    monkeypatch.setenv("NOTE_OUTPUT_DIR", str(root))
    record = {"task_id": "demo", "title": "视频学习笔记", "platform": "youtube",
              "status": "PENDING", "created_at": "2026-10-14T12:30:00"}
    directory = root / record["task_id"]
    write_json(directory / "status.json", {"status": "SUCCESS", "message": "完成"})
    write_json(directory / "source.json", {"kind": "note", "platform": "youtube",
                                          "input": "https://www.youtube.com/watch?v=abcdefghijk"})
    (directory / "gen").mkdir()
    (directory / "gen" / "note.md").write_text("# 学习笔记\n\n这是完整笔记。", encoding="utf-8")
    write_json(directory / "gen" / "transcript.json", {
        "language": "zh", "full_text": "视频原文", "segments": [{"start": 62, "end": 65, "text": "视频原文"}],
    })
    monkeypatch.setattr("app.db.video_task_dao.list_tasks", lambda: [record])
    monkeypatch.setattr(cli, "_show_header", lambda *_: None)
    monkeypatch.setattr(cli, "_press_any_key", lambda: None)
    return record, directory, root


class ScriptedPrompts:
    def __init__(self, answers):
        self.answers = iter(answers)
        self.choices = []

    def select(self, message, choices, keybindings):
        self.choices.append(choices)
        answer = next(self.answers)
        if isinstance(answer, BaseException):
            raise answer
        assert answer in [choice["value"] for choice in choices]

        class Result:
            def execute(self):
                return answer

        return Result()


def test_details_prefer_disk_status_and_note_over_index_and_result(task):
    record, directory, root = task
    write_json(directory / "result.json", {"markdown": "旧的笔记", "transcript": {"full_text": "旧转写"}})
    before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    detail = read_task_details(record, root)
    assert detail["status"] == "SUCCESS" and not detail["status_from_index"]
    assert "完整笔记" in detail["markdown"]
    assert detail["transcript"]["full_text"] == "视频原文"
    assert detail["source"].endswith("v=abcdefghijk")
    assert detail["note_paths"] == [directory / "gen" / "note.md"]
    assert before == {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}


def test_browse_note_transcript_files_and_return_to_list(task, capsys):
    _, directory, _ = task
    (directory / "raw").mkdir()
    (directory / "raw" / "video.mp4").write_bytes(b"test")
    inq = ScriptedPrompts(["demo", "note", "back", "transcript", "back", "files", "back", "back", "back"])
    cli._wizard_data_list(inq)
    output = capsys.readouterr().out
    assert "生成笔记" in output and "原始来源" in output
    assert "这是完整笔记。" in output
    assert "[00:01:02 → 00:01:05] 视频原文" in output
    assert str(directory / "raw" / "video.mp4") in output
    assert "[已完成]" in inq.choices[0][0]["name"]  # Index still says PENDING.
    assert [choice["value"] for choice in inq.choices[-1]] == ["demo", "refresh", "back"]


def test_material_task_explains_agent_note_and_shows_comments(task, capsys):
    record, directory, _ = task
    (directory / "gen" / "note.md").unlink()
    write_json(directory / "source.json", {"kind": "material", "input": "/input/lecture.mp4", "platform": "local"})
    write_json(directory / "result.json", {"kind": "material", "comments_danmaku": "观众评论", "audio_path": "/missing/audio.wav"})
    inq = ScriptedPrompts(["comments", "back", "files", "back", "back"])
    cli._wizard_task_detail(inq, record)
    output = capsys.readouterr().out
    assert "供 Agent 撰写笔记" in output and "观众评论" in output
    assert "/input/lecture.mp4" in output
    assert "（文件缺失或已清理）" in output
    assert "note" not in [choice["value"] for choice in inq.choices[0]]


def test_failed_and_cleaned_tasks_remain_browsable(task, capsys):
    record, directory, _ = task
    (directory / "gen" / "note.md").unlink()
    (directory / "gen" / "transcript.json").unlink()
    write_json(directory / "status.json", {"status": "FAILED", "message": "下载失败"})
    cli._wizard_task_detail(ScriptedPrompts(["files", "back", "back"]), record)
    output = capsys.readouterr().out
    assert "下载失败" in output and "未生成可用笔记" in output
    assert "暂无可用文件" in output
    assert "https://www.youtube.com/watch?v=abcdefghijk" in output


def test_refresh_reads_new_status_and_new_note(task, capsys):
    record, directory, _ = task
    (directory / "gen" / "note.md").unlink()
    write_json(directory / "status.json", {"status": "TRANSCRIBING"})

    class RefreshPrompts(ScriptedPrompts):
        def select(self, **kwargs):
            prompt = super().select(**kwargs)
            if len(self.choices) == 1:
                write_json(directory / "status.json", {"status": "SUCCESS"})
                (directory / "gen" / "note.md").write_text("# 刷新后的笔记", encoding="utf-8")
            return prompt

    cli._wizard_task_detail(RefreshPrompts(["refresh", "note", "back", "back"]), record)
    output = capsys.readouterr().out
    assert "转写中" in output and "已完成" in output and "刷新后的笔记" in output


def test_long_content_pagination_and_back(task, capsys, monkeypatch):
    monkeypatch.setattr("shutil.get_terminal_size", lambda *_: (80, 24))
    content = "\n".join(f"正文第 {index} 行" for index in range(30))
    cli._task_preview(ScriptedPrompts(["next", "next", "prev", "back"]), "笔记预览", content)
    output = capsys.readouterr().out
    assert "正文第 0 行" in output and "正文第 29 行" in output


def test_left_key_cancels_detail_then_returns_to_task_list(task):
    inq = ScriptedPrompts(["demo", KeyboardInterrupt(), "back"])
    cli._wizard_data_list(inq)
    assert inq.choices[-1][0]["value"] == "demo"


@pytest.mark.parametrize("key", ["\x03", "\x1b[D"])
def test_real_interrupt_keys_return_from_details(task, key):
    from InquirerPy import inquirer
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    record, _, _ = task
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        pipe.send_text(key)
        cli._wizard_task_detail(inquirer, record)


def test_empty_task_list_returns_without_prompt(task, capsys, monkeypatch):
    monkeypatch.setattr("app.db.video_task_dao.list_tasks", lambda: [])
    cli._wizard_data_list(ScriptedPrompts([]))
    assert "暂无任务记录" in capsys.readouterr().out


def test_plain_text_browsing_supports_details(task, capsys, monkeypatch):
    answers = iter(["1", "1", "0", "0", "0"])
    monkeypatch.setattr(cli, "_ask", lambda *_args, **_kwargs: next(answers))
    cli._wizard_data_list(None)
    assert "这是完整笔记。" in capsys.readouterr().out


def test_corrupt_artifacts_fall_back_without_repair(task):
    record, directory, root = task
    (directory / "status.json").write_text("[]", encoding="utf-8")
    (directory / "gen" / "transcript.json").write_text("broken", encoding="utf-8")
    write_json(directory / "result.json", {"transcript": {"full_text": "备用转写"}})
    detail = read_task_details(record, root)
    assert detail["status"] == "PENDING" and detail["status_from_index"]
    assert detail["transcript"]["full_text"] == "备用转写"
    assert len(detail["warnings"]) == 2
    assert (directory / "gen" / "transcript.json").read_text() == "broken"


def test_portable_note_survives_missing_canonical_artifacts(task, tmp_path):
    record, directory, root = task
    (directory / "gen" / "note.md").unlink()
    portable = tmp_path / "我的笔记" / "note.md"
    portable.parent.mkdir()
    portable.write_text("> 来源链接：https://user:password@youtube.com/watch?v=abcdefghijk&token=secret\n\n# 便携笔记", encoding="utf-8")
    write_json(directory / "manifest.json", {"paths": [portable.as_uri()]})
    (directory / "source.json").unlink()
    detail = read_task_details(record, root)
    assert detail["note_paths"] == [portable]
    assert detail["kind"] == "note"
    assert detail["source"] == "https://youtube.com/watch?v=abcdefghijk"


def test_legacy_material_source_reconstructed_only_for_valid_identifier(task):
    record, directory, root = task
    (directory / "source.json").unlink()
    (directory / "gen" / "note.md").unlink()
    record.update(platform="bilibili", video_id="BV1xx_p3")
    detail = read_task_details(record, root)
    assert detail["source"] == "https://www.bilibili.com/video/BV1xx?p=3"
    assert "旧任务" in detail["source_hint"]
    record["video_id"] = "demo"
    assert read_task_details(record, root)["source"] == ""


def test_invalid_task_id_rejected_before_read(task):
    record, _, root = task
    record["task_id"] = "../../outside"
    with pytest.raises(ValueError, match="非法 task_id"):
        read_task_details(record, root)


def test_transcript_handles_empty_and_malformed_segments():
    assert transcript_text({"full_text": "全文", "segments": []}) == "全文"
    assert transcript_text({"full_text": "", "segments": [None, {"start": "bad", "end": None, "text": "文本"}]}) == "[? → ?] 文本"


@pytest.mark.parametrize("material", [False, True])
def test_submission_persists_source_before_worker_without_url_credentials(tmp_path, monkeypatch, material):
    from videonote_mcp import server

    monkeypatch.setattr(server, "NOTE_OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(server, "_task_events", {})
    monkeypatch.setattr(server, "_task_futures", {})
    monkeypatch.setattr(server, "_batch_ctx", mock.Mock(bypass_guard=False))

    def submit(*args, **kwargs):
        data = json.loads((tmp_path / "queued" / "source.json").read_text())
        assert data == {"kind": "material" if material else "note", "platform": "youtube",
                        "input": "https://youtube.com/watch?v=abcdefghijk&p=2"}
        raise RuntimeError("executor unavailable")

    with mock.patch.object(server._pool, "submit", side_effect=submit):
        with pytest.raises(RuntimeError, match="executor unavailable"):
            server._submit_registered_task("queued", mock.Mock(), platform="youtube", material_only=material,
                                           video_url="https://user:password@youtube.com/watch?v=abcdefghijk&p=2&token=secret#private")
    assert not server._task_events and not server._task_futures


def test_source_write_failure_rolls_back_task_submission(tmp_path, monkeypatch):
    from videonote_mcp import server

    monkeypatch.setattr(server, "NOTE_OUTPUT_DIR", tmp_path)
    monkeypatch.setenv("NOTE_OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(tmp_path))
    task_id = "sourcefailure"
    write_json(tmp_path / task_id / "status.json", {"status": "PENDING"})
    monkeypatch.setattr(server.uuid, "uuid4", lambda: mock.Mock(hex=task_id))
    with mock.patch.object(server, "_index_step_task"), mock.patch.object(server, "_write_status"), \
         mock.patch.object(server, "_atomic_write_json", side_effect=OSError("disk full")), \
         mock.patch.object(server._pool, "submit") as submit:
        with pytest.raises(OSError, match="disk full"):
            server.prepare_note_material("https://youtube.com/watch?v=abcdefghijk", platform="youtube")
    submit.assert_not_called()
    assert not (tmp_path / task_id).exists()
