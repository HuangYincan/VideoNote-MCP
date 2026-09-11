"""Shared filesystem/artifact logic stays independent of the MCP process runtime."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.utils.local_paths import LocalPathPolicy, coerce_local_path
from videonote_mcp.task_artifacts import (
    read_task_status,
    read_transcript,
    validate_task_id,
)


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("value", ["../secret", "a/b", "a\\b", "", "a" * 65, "视频", None])
def test_task_id_rejected_before_path_join(value):
    with pytest.raises(ValueError, match="非法 task_id"):
        validate_task_id(value)


def test_task_id_is_normalized():
    assert validate_task_id(" task-01_ab ") == "task-01_ab"


@pytest.mark.parametrize("value", [None, [], {}, {"status": None}, {"status": 1}, {"status": " "}])
def test_unusable_status_is_unknown(tmp_path, value):
    _write(tmp_path / "status.json", value)
    assert read_task_status(tmp_path) == "UNKNOWN"


@pytest.mark.parametrize("status", ["SUCCESS", "FAILED", "CANCELLED", "SUMMARIZING"])
def test_reader_preserves_status(tmp_path, status):
    _write(tmp_path / "status.json", {"status": status})
    assert read_task_status(tmp_path) == status


def test_missing_and_corrupt_status(tmp_path):
    assert read_task_status(tmp_path) == "UNKNOWN"
    (tmp_path / "status.json").write_bytes(b"\xff")
    assert read_task_status(tmp_path) == "UNKNOWN"


@pytest.mark.parametrize("bad_cache", [None, {}, [], "text", 42, {"segments": "bad"}])
def test_unusable_cache_falls_back_consistently(tmp_path, bad_cache):
    _write(tmp_path / "gen" / "transcript.json", bad_cache)
    transcript = {"full_text": "来自结果", "segments": []}
    _write(tmp_path / "result.json", {"transcript": transcript})
    result = read_transcript(tmp_path)
    assert result.transcript == transcript
    assert result.cache_error
    assert result.error is None


def test_canonical_cache_wins_and_preserves_raw(tmp_path):
    canonical = {"full_text": "缓存", "segments": [], "raw": {"source": 1}}
    _write(tmp_path / "gen" / "transcript.json", canonical)
    _write(tmp_path / "result.json", {"transcript": {"full_text": "旧结果"}})
    assert read_transcript(tmp_path).transcript == canonical


def test_legitimate_empty_transcript_is_not_missing(tmp_path):
    transcript = {"full_text": "", "segments": []}
    _write(tmp_path / "gen" / "transcript.json", transcript)
    assert read_transcript(tmp_path).transcript == transcript


def test_corrupt_cache_and_result_diagnostics(tmp_path):
    (tmp_path / "gen").mkdir()
    (tmp_path / "gen" / "transcript.json").write_text("{broken", encoding="utf-8")
    result = read_transcript(tmp_path)
    assert result.transcript is None
    assert "转写缓存损坏" in result.cache_error
    assert "找不到任务" in result.error
    (tmp_path / "result.json").write_bytes(b"\xff")
    result = read_transcript(tmp_path)
    assert "结果文件损坏" in result.error


@pytest.mark.parametrize("value", [None, [], "bad", {}])
def test_result_shape_errors_are_reported(tmp_path, value):
    _write(tmp_path / "result.json", value)
    result = read_transcript(tmp_path)
    assert result.transcript is None
    assert result.error


def test_file_uri_and_home_normalization(tmp_path, monkeypatch):
    path = tmp_path / "我的 视频.mp4"
    assert coerce_local_path(path.as_uri()) == path
    monkeypatch.setenv("HOME", str(tmp_path))
    assert coerce_local_path("~/视频.mp4") == tmp_path / "视频.mp4"


def test_local_policy_checks_resolved_paths(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    link = data / "link.mp4"
    link.symlink_to(outside)
    policy = LocalPathPolicy(data)
    assert policy.contains(data / "new.mp4")
    assert not policy.contains(link)
    with pytest.raises(ValueError, match="VIDEONOTE_ALLOW_EXTERNAL_PATHS"):
        policy.guard(link, "本地视频路径")
    LocalPathPolicy(data, allow_external=True).guard(outside, "本地视频路径")


def test_shared_modules_and_local_inspection_do_not_boot_mcp(tmp_path):
    """Run outside pytest's already imported server; block heavyweight imports altogether."""
    data = tmp_path / "data"
    data.mkdir()
    source = data / "我的 视频.mp4"
    source.write_bytes(b"not downloaded")
    repo = Path(__file__).resolve().parents[1]
    script = '''
import builtins, importlib.abc, json, sys
from pathlib import Path
blocked = ("videonote_mcp.server", "videonote_mcp.cli", "app.db", "app.services.note",
           "app.services.pipeline", "app.transcriber.transcriber_provider")
class Guard(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise AssertionError("Unexpected runtime dependency: " + fullname)
sys.meta_path.insert(0, Guard())
original_print, original_stdout = builtins.print, sys.stdout
from app.services.inspect import inspect_video
from videonote_mcp.task_artifacts import read_transcript
from videonote_mcp.export import export_transcript
result = inspect_video(Path(sys.argv[1]).as_uri())
assert result["ok"], result
written = export_transcript({"full_text": "offline", "segments": []}, formats=["json"],
                            out_dir=Path(sys.argv[2]), task_id="isolated")
assert "json" in written, written
assert builtins.print is original_print and sys.stdout is original_stdout
print(json.dumps({"ok": True}))
'''
    env = dict(os.environ, PYTHONPATH=str(repo), VIDEONOTE_DATA_DIR=str(data),
               NOTE_OUTPUT_DIR=str(data / "notes"), VIDEONOTE_CONFIG_DIR=str(data / "config"),
               DATABASE_URL=f"sqlite:///{data / 'must-not-exist.db'}")
    result = subprocess.run([sys.executable, "-c", script, str(source), str(data / "export")],
                            env=env, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"ok": True}
    assert not (data / "must-not-exist.db").exists()
    assert not (data / "logs" / "mcp_stderr.log").exists()


@pytest.fixture
def isolated_task(tmp_path, monkeypatch):
    import videonote_mcp.server as server

    data = tmp_path / "data"
    notes = data / "note_results"
    task_dir = notes / "parity-task"
    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(data))
    monkeypatch.setenv("NOTE_OUTPUT_DIR", str(notes))
    monkeypatch.delenv("VIDEONOTE_ALLOW_EXTERNAL_PATHS", raising=False)
    monkeypatch.setattr(server, "DATA_DIR", data)
    monkeypatch.setattr(server, "NOTE_OUTPUT_DIR", notes)
    return server, task_dir


@pytest.mark.parametrize("cache", [None, {}, [], "invalid", {"segments": "invalid"}])
def test_cli_mcp_and_resource_share_cache_fallback(isolated_task, cache, capsys):
    from videonote_mcp import cli

    server, task_dir = isolated_task
    transcript = {"full_text": "共享的回退结果", "segments": []}
    _write(task_dir / "status.json", {"status": "SUCCESS"})
    _write(task_dir / "gen" / "transcript.json", cache)
    _write(task_dir / "result.json", {"transcript": transcript})
    assert server._load_task_transcript(task_dir.name) == transcript
    result = json.loads(server.process_media(task_id=task_dir.name, formats=["json"]))
    assert result["ok"], result
    assert json.loads(coerce_local_path(result["formats"]["json"]).read_text())["full_text"] == transcript["full_text"]
    cli._export_cli(["export", task_dir.name, "--format", "json"])
    output = capsys.readouterr()
    assert "✓ 已导出" in output.out + output.err
    assert json.loads((task_dir / "gen" / "transcript.export.json").read_text())["full_text"] == transcript["full_text"]
    # Never repair/overwrite the canonical cache during a read or export.
    assert json.loads((task_dir / "gen" / "transcript.json").read_text()) == cache


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED", "SUMMARIZING"])
def test_all_entrypoints_reject_unfinished_task(isolated_task, status):
    from videonote_mcp import cli

    server, task_dir = isolated_task
    _write(task_dir / "status.json", {"status": status})
    _write(task_dir / "gen" / "transcript.json", {"full_text": "not published", "segments": []})
    assert server._load_task_transcript(task_dir.name) is None
    result = json.loads(server.process_media(task_id=task_dir.name, formats=["json"]))
    assert not result["ok"] and status in result["error"]
    with pytest.raises(SystemExit) as error:
        cli._export_cli(["export", task_dir.name, "--format", "json"])
    assert error.value.code == 1
    assert not (task_dir / "gen" / "transcript.export.json").exists()


def test_legacy_export_admission_is_not_resource_admission(isolated_task):
    from videonote_mcp import cli

    server, task_dir = isolated_task
    _write(task_dir / "result.json", {"transcript": {"full_text": "legacy", "segments": []}})
    assert server._load_task_transcript(task_dir.name) is None
    assert json.loads(server.process_media(task_id=task_dir.name, formats=["json"]))["ok"]
    cli._export_cli(["export", task_dir.name, "--format", "json"])


def test_explicit_cli_output_does_not_weaken_mcp_permission(isolated_task, tmp_path):
    from videonote_mcp import cli

    server, task_dir = isolated_task
    _write(task_dir / "status.json", {"status": "SUCCESS"})
    _write(task_dir / "gen" / "transcript.json", {"full_text": "ok", "segments": []})
    outside = tmp_path / "桌面 导出"
    with pytest.raises(ValueError, match="VIDEONOTE_ALLOW_EXTERNAL_PATHS"):
        server.process_media(task_id=task_dir.name, formats=["json"], out_dir=outside.as_uri())
    assert not outside.exists()
    cli._export_cli(["export", task_dir.name, "--format", "json", "--out-dir", outside.as_uri()])
    assert (outside / "transcript.export.json").is_file()


def test_server_injects_policy_without_using_environment_root(isolated_task, monkeypatch, tmp_path):
    server, task_dir = isolated_task
    task_dir.mkdir(parents=True)
    source = task_dir / "视频.mp4"
    source.write_bytes(b"x")
    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(tmp_path / "other"))
    assert json.loads(server.inspect_video(source.as_uri()))["ok"]
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"x")
    assert not json.loads(server.inspect_video(outside.as_uri()))["ok"]
    monkeypatch.setenv("VIDEONOTE_ALLOW_EXTERNAL_PATHS", "true")
    assert json.loads(server.inspect_video(outside.as_uri()))["ok"]


@pytest.mark.parametrize("flag, allowed", [("false", False), ("garbage", False), ("${setting}", False), ("YES", True)])
def test_standalone_inspection_keeps_explicit_environment_permission(tmp_path, monkeypatch, flag, allowed):
    from app.services.inspect import inspect_video

    monkeypatch.setenv("VIDEONOTE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("VIDEONOTE_ALLOW_EXTERNAL_PATHS", flag)
    path = tmp_path / "source.mp4"
    path.write_bytes(b"x")
    assert inspect_video(path.as_uri())["ok"] is allowed


def test_relative_export_output_returns_absolute_uri(tmp_path, monkeypatch):
    from videonote_mcp.export import export_transcript

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOTE_OUTPUT_DIR", str(tmp_path / "notes"))
    result = export_transcript({"segments": []}, formats=["json"], out_dir="relative", task_id="relative")
    assert result["json"] == (tmp_path / "relative" / "transcript.export.json").as_uri()


def test_default_export_directory_reads_current_configuration(tmp_path, monkeypatch):
    from videonote_mcp.export import export_transcript

    for directory in ("first", "second"):
        notes = tmp_path / directory
        monkeypatch.setenv("NOTE_OUTPUT_DIR", str(notes))
        result = export_transcript({"segments": []}, formats=["json"], task_id="configured")
        assert coerce_local_path(result["json"]).parent == notes / "configured"
