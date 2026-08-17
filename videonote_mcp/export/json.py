"""Transcript JSON 导出（纯函数，零依赖）。

输出结构化 `{language, full_text, segments: [{start, end, text, speaker?}]}`，与
`{task_id}.json` 的 transcript 字段同构，可直接被下游程序消费。

与 asdict(TranscriptResult) 的差异：这里显式规整字段并做缺省兜底，
保证无论输入是 dict、TranscriptResult 还是原始 dict 列表，输出结构一致。
"""
import json
from typing import List, Optional, Union

from app.models.transcriber_model import TranscriptResult


def _seg_to_dict(seg) -> dict:
    if isinstance(seg, dict):
        speaker = seg.get("speaker")
        out = {
            "start": round(float(seg.get("start", 0)), 3),
            "end": round(float(seg.get("end", 0)), 3),
            "text": str(seg.get("text", "")),
        }
    else:
        speaker = getattr(seg, "speaker", None)
        out = {"start": round(float(seg.start), 3), "end": round(float(seg.end), 3), "text": str(seg.text)}
    if speaker:
        # 说话人标注必须保留：导出 json 常落在 gen/transcript.json（转写缓存路径），
        # 丢失 speaker 会永久覆盖带说话人标注的缓存（#122 A2，会议纪要数据损坏）
        out["speaker"] = speaker
    return out


def _to_segments(segments: Optional[List]) -> List[dict]:
    if not segments:
        return []
    return [_seg_to_dict(s) for s in segments]


def to_json(transcript: Optional[Union[TranscriptResult, dict]]) -> str:
    """把 TranscriptResult / dict 渲染为缩进 JSON 字符串。

    缺字段时安全兜底（language 可为 null，full_text 可为空）。
    """
    if transcript is None:
        return json.dumps(
            {"language": None, "full_text": "", "segments": []}, ensure_ascii=False, indent=2
        )
    if isinstance(transcript, TranscriptResult):
        language = transcript.language
        full_text = transcript.full_text or ""
        segments = _to_segments(transcript.segments)
    elif isinstance(transcript, dict):
        language = transcript.get("language")
        full_text = transcript.get("full_text") or ""
        segments = _to_segments(transcript.get("segments"))
    else:
        # 未知类型 —— 尝试按 dict 处理，失败兜底空结构
        try:
            language = getattr(transcript, "language", None)
            full_text = getattr(transcript, "full_text", "") or ""
            segments = _to_segments(getattr(transcript, "segments", []))
        except Exception:
            return json.dumps(
                {"language": None, "full_text": "", "segments": []}, ensure_ascii=False, indent=2
            )
    return json.dumps(
        {"language": language, "full_text": full_text, "segments": segments},
        ensure_ascii=False,
        indent=2,
    )
