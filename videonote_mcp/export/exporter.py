"""导出编排 —— 把 transcript 渲染为多种格式并落盘。

入口 `export_transcript(source, formats, out_dir)`：
  - source: 转写结果（TranscriptResult / dict，含 segments 字段）；
  - formats: 需要导出的格式列表（srt / vtt / json）；
  - out_dir: 输出目录，缺省 NOTE_OUTPUT_DIR/{task_id}/（task_id 为空时用时间戳名）。

返回 `{fmt: file://绝对路径}`（失败格式不在结果里，错误在 errors dict）。
落盘后把产物路径记入 task manifest（供 cleanup_note 清理）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Union

from app.models.transcriber_model import TranscriptResult
from app.services.note import NOTE_OUTPUT_DIR
from app.utils.task_manifest import record_task_paths

from .json import to_json
from .srt import to_srt
from .vtt import to_vtt

logger = logging.getLogger(__name__)

_FORMAT_EXT = {"srt": "srt", "vtt": "vtt", "json": "json"}


def _segments(source) -> List:
    if isinstance(source, TranscriptResult):
        return list(source.segments or [])
    if isinstance(source, dict):
        return list(source.get("segments") or [])
    return list(getattr(source, "segments", None) or [])


def export_transcript(
    source: Union[TranscriptResult, dict],
    formats: Optional[List[str]] = None,
    out_dir: Optional[Union[str, Path]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, str]:
    """渲染并落盘指定格式，返回 `{fmt: file://path}`；失败格式跳过并记日志。"""
    formats = formats or ["srt"]
    valid = [f for f in formats if f in _FORMAT_EXT]
    unknown = set(formats) - set(_FORMAT_EXT)
    if unknown:
        logger.warning(f"导出忽略未知格式: {sorted(unknown)}（支持 {sorted(_FORMAT_EXT)}）")

    if out_dir is None:
        task = task_id or "export"
        out_dir = NOTE_OUTPUT_DIR / task
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    segments = _segments(source)
    rendered: Dict[str, str] = {}

    if "json" in valid:
        rendered["json"] = to_json(source)
    for fmt in ("srt", "vtt"):
        if fmt not in valid:
            continue
        renderer = to_srt if fmt == "srt" else to_vtt
        rendered[fmt] = renderer(segments)

    written: Dict[str, str] = {}
    errors: Dict[str, str] = {}
    for fmt, content in rendered.items():
        path = out_dir / f"transcript.{_FORMAT_EXT[fmt]}"
        try:
            path.write_text(content, encoding="utf-8")
            written[fmt] = path.as_uri()
        except OSError as exc:
            errors[fmt] = str(exc)
            logger.error(f"导出 {fmt} 落盘失败: {exc}")

    if written:
        record_task_paths(task_id or "export", [str(out_dir / f"transcript.{_FORMAT_EXT[f]}") for f in written])
    if errors:
        logger.error(f"导出完成但部分失败: {errors}")
        written["_errors"] = errors  # type: ignore[assignment]
    return written
