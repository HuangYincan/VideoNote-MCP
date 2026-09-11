"""Read task artifacts without starting the MCP runtime or writing any files.

The transport adapters own status admission: transcript Resources require SUCCESS,
while exports also support legacy tasks whose status is UNKNOWN. This module owns
identifier validation, disk status decoding and canonical-cache/result fallback.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def validate_task_id(task_id: str) -> str:
    """Validate before joining a user-supplied identifier to the task output root."""
    value = str(task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(value):
        raise ValueError(f"非法 task_id（只允许字母数字/下划线/连字符，最长 64）: {task_id!r}")
    return value


def read_task_status(task_dir: Path) -> str:
    """Unreadable, missing or malformed status JSON is UNKNOWN, never SUCCESS."""
    try:
        data = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
        status = data.get("status") if isinstance(data, dict) else None
        return status if isinstance(status, str) and status.strip() else "UNKNOWN"
    except (OSError, ValueError):
        return "UNKNOWN"


@dataclass(frozen=True)
class TranscriptRead:
    transcript: dict | None
    cache_error: str | None = None
    error: str | None = None


def _usable_transcript(value) -> bool:
    """Check the container, not ASR content; an intentionally empty transcript is valid."""
    if not isinstance(value, dict) or not ("segments" in value or "full_text" in value):
        return False
    if "segments" in value and not isinstance(value["segments"], list):
        return False
    return "full_text" not in value or isinstance(value["full_text"], str)


def read_transcript(task_dir: Path) -> TranscriptRead:
    """Prefer gen/transcript.json; unusable/missing cache falls back to result.json.

    Preserve all fields (including raw/truncated). Diagnostics never include JSON
    contents. No status admission, repair, logging, writes, or runtime imports here.
    """
    cache_error = None
    cache = task_dir / "gen" / "transcript.json"
    try:
        transcript = json.loads(cache.read_text(encoding="utf-8"))
        if _usable_transcript(transcript):
            return TranscriptRead(transcript)
        cache_error = f"转写缓存损坏（{cache}）：不是有效的转写对象"
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        cache_error = f"转写缓存损坏（{cache}）：无法读取 JSON 转写对象"

    result_path = task_dir / "result.json"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return TranscriptRead(None, cache_error, f"找不到任务 {task_dir.name} 的结果文件（{result_path}），任务可能未成功")
    except (OSError, ValueError):
        return TranscriptRead(None, cache_error, f"任务 {task_dir.name} 的结果文件损坏：无法读取 JSON 对象")
    if not isinstance(result, dict):
        return TranscriptRead(None, cache_error, f"任务 {task_dir.name} 的结果文件损坏：不是 JSON 对象")
    transcript = result.get("transcript")
    if not _usable_transcript(transcript):
        return TranscriptRead(None, cache_error, f"任务 {task_dir.name} 没有转写结果（可能未到转写阶段，或转写对象无效）")
    return TranscriptRead(transcript, cache_error)
