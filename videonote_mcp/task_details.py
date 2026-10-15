"""Read-only task views for setup, without importing the MCP or ASR runtime."""
from __future__ import annotations

import json
import re
from pathlib import Path

from videonote_mcp.task_artifacts import read_transcript, validate_task_id

STATUS_LABELS = {
    "PENDING": "排队中", "PARSING": "解析来源", "DOWNLOADING": "下载中",
    "TRANSCRIBING": "转写中", "SUMMARIZING": "生成笔记", "FORMATTING": "排版中",
    "SAVING": "保存中", "SUCCESS": "已完成", "FAILED": "失败",
    "CANCELLED": "已取消", "UNKNOWN": "状态未知",
}
KIND_LABELS = {"note": "生成笔记", "material": "准备素材", "transcript": "转写", "download": "下载媒体"}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "状态未知")


def _read_object(path: Path, warnings: list[str]) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        pass
    warnings.append(f"{path.name} 无法读取或内容损坏，已显示其余可用信息。")
    return {}


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _source_from_note(markdown: str) -> str:
    for line in markdown.splitlines()[:10]:
        match = re.match(r"^\s*>?\s*来源链接[：:]\s*(.+)$", line)
        if match:
            return match[1].strip()
    return ""


def _legacy_source(platform: str, video_id: str) -> str:
    # Only reconstruct well-known identifiers; never turn a placeholder task ID into a URL.
    if platform == "bilibili" and re.fullmatch(r"BV[A-Za-z0-9]+(?:_p\d+)?", video_id):
        return "https://www.bilibili.com/video/" + video_id.replace("_p", "?p=")
    if platform == "youtube" and re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        return "https://www.youtube.com/watch?v=" + video_id
    return ""


def read_task_details(task: dict, output_root: Path) -> dict:
    """Combine index, canonical artifacts and recorded portable notes, tolerating partial tasks.

    Only known artifact fields are projected. Raw downloader metadata is never shown.
    Missing files are expected during execution and after keeping notes while cleaning.
    """
    from app.utils.local_paths import coerce_local_path
    from app.utils.url_safety import public_replay_url

    task_id = validate_task_id(task.get("task_id"))
    task_dir = output_root / task_id
    warnings: list[str] = []
    status = _read_object(task_dir / "status.json", warnings)
    source = _read_object(task_dir / "source.json", warnings)
    result = _read_object(task_dir / "result.json", warnings)
    audio = _read_object(task_dir / "gen" / "audio.json", warnings)
    if not audio and isinstance(result.get("audio_meta"), dict):
        audio = result["audio_meta"]
    manifest = _read_object(task_dir / "manifest.json", warnings)

    note_paths = [task_dir / "gen" / "note.md"]
    for directory in (result.get("portable_note_dir"), result.get("note_dir"), task.get("note_dir")):
        if isinstance(directory, str) and directory:
            note_paths.append(coerce_local_path(directory) / "note.md")
    paths = manifest.get("paths")
    for value in paths if isinstance(paths, list) else []:
        if isinstance(value, str) and value:
            path = coerce_local_path(value)
            if path.name == "note.md":
                note_paths.append(path)
    note_paths = list(dict.fromkeys(note_paths))
    markdown = ""
    existing_notes = []
    for path in note_paths:
        try:
            if path.is_file():
                existing_notes.append(path)
                if not markdown:
                    markdown = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            warnings.append(f"笔记无法读取：{path}")
    if not markdown:
        markdown = _text(result.get("markdown"))

    transcript_read = read_transcript(task_dir)
    transcript = transcript_read.transcript or {}
    if transcript_read.cache_error:
        warnings.append(transcript_read.cache_error)

    platform = _text(source.get("platform") or audio.get("platform") or task.get("platform"))
    video_id = _text(audio.get("video_id") or task.get("video_id"))
    origin = _text(source.get("input")) or _source_from_note(markdown)
    origin_hint = ""
    if not origin:
        origin = _legacy_source(platform, video_id)
        if origin:
            origin_hint = "根据平台与视频 ID 定位（旧任务未记录提交链接）"
    # The replay sanitizer preserves v/p but removes credentials from historical links.
    if origin.startswith(("http://", "https://")):
        origin = public_replay_url(origin)

    kind = _text(source.get("kind") or result.get("kind"))
    if not kind and (markdown or existing_notes):
        kind = "note"

    files = set(existing_notes)
    try:
        for sub in ("raw", "gen"):
            directory = task_dir / sub
            if directory.is_dir():
                files.update(path for path in directory.rglob("*") if path.is_file())
    except OSError:
        warnings.append("部分任务文件无法列出。")
    # Include manifest-recorded exports outside gen/, but do not recursively scan arbitrary directories.
    for value in paths if isinstance(paths, list) else []:
        if isinstance(value, str) and value:
            path = coerce_local_path(value)
            try:
                if path.is_file() and path.name not in {"status.json", "result.json", "manifest.json", "source.json"}:
                    files.add(path)
            except OSError:
                pass

    def media_path(*values) -> str:
        return next((_text(value) for value in values if _text(value)), "")

    return {
        "task_id": task_id, "task_dir": task_dir,
        "title": _text(task.get("title") or result.get("title") or audio.get("title")) or "（无标题）",
        "status": _text(status.get("status")) or _text(task.get("status")) or "UNKNOWN",
        "status_from_index": not bool(_text(status.get("status"))),
        "message": _text(status.get("message")), "created_at": _text(task.get("created_at")),
        "kind": kind, "platform": platform, "video_id": video_id,
        "source": origin, "source_hint": origin_hint, "duration": audio.get("duration"),
        "markdown": markdown, "note_paths": existing_notes,
        "transcript": transcript, "has_transcript": transcript_read.transcript is not None,
        "audio_path": media_path(result.get("audio_path"), audio.get("file_path")),
        "video_path": media_path(result.get("video_path"), audio.get("video_path")),
        "comments": _text(result.get("comments_danmaku")),
        "files": sorted(files, key=str), "warnings": warnings,
    }


def transcript_text(transcript: dict) -> str:
    """Display timestamped segments when available, otherwise the plain transcript."""
    def timestamp(value) -> str:
        try:
            seconds = max(0, int(float(value)))
        except (TypeError, ValueError, OverflowError):
            return "?"
        return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"

    lines = []
    for segment in transcript.get("segments") or []:
        if not isinstance(segment, dict):
            continue
        speaker = _text(segment.get("speaker"))
        lines.append(f"[{timestamp(segment.get('start'))} → {timestamp(segment.get('end'))}] "
                     f"{speaker + ': ' if speaker else ''}{_text(segment.get('text'))}")
    return "\n".join(lines) or _text(transcript.get("full_text"))
