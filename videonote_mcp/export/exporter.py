"""导出编排 —— 把 transcript 渲染为多种格式并落盘。

入口 `export_transcript(source, formats, out_dir)`：
  - source: 转写结果（TranscriptResult / dict，含 segments 字段）；
  - formats: 需要导出的格式列表（srt / vtt / json）；
  - out_dir: 输出目录，缺省 NOTE_OUTPUT_DIR/{task_id}/（task_id 为空时用时间戳名）。

返回 `{fmt: file://绝对路径}`（失败格式不在结果里，错误在 errors dict）。
落盘后把产物路径记入 task manifest（供 MCP cleanup 工具清理）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Union

from app.models.transcriber_model import TranscriptResult
from app.services.note import NOTE_OUTPUT_DIR
from app.utils.json_store import write_text_atomic
from app.utils.task_manifest import record_task_paths

from .json import to_json
from .srt import to_srt
from .vtt import to_vtt

logger = logging.getLogger(__name__)

_FORMAT_EXT = {"srt": "srt", "vtt": "vtt", "json": "json"}
# 导出文件名：json 用 transcript.export.json——note.py 的转写缓存规范来源就是
# gen/transcript.json（server 的 _load_task_transcript / export fallback 都读它）。
# 自动导出/工具缺省 out_dir 指向 gen/ 时，若也写 transcript.json 会把带 raw 等
# 完整字段的缓存覆盖成轻量导出 JSON（#122 A2 数据损坏）。
_FORMAT_FILENAME = {"srt": "transcript.srt", "vtt": "transcript.vtt", "json": "transcript.export.json"}


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
    # 显式传 [] 表示「不导出任何格式」——不能用 `formats or ["srt"]`（空列表被当
    # falsy 重解释成默认 srt，调用方以为自己空选择生效却多出一个文件，#122 A4）
    if formats is None:
        formats = ["srt"]
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
        path = out_dir / _FORMAT_FILENAME[fmt]
        try:
            # 原子写（docs/05 第 16 轮 B10）：进程中断不留下截断的 .srt/.vtt/.json
            write_text_atomic(path, content)
            written[fmt] = path.as_uri()
        except OSError as exc:
            errors[fmt] = str(exc)
            logger.error(f"导出 {fmt} 落盘失败: {exc}")

    if written:
        record_task_paths(task_id or "export", [str(out_dir / _FORMAT_FILENAME[f]) for f in written])
    if errors:
        logger.error(f"导出完成但部分失败: {errors}")
        written["_errors"] = errors  # type: ignore[assignment]
    return written
