"""说话人分离（pyannote-audio 3.x，可选 extras）。

依赖很重（torch + pyannote + HF_TOKEN + 模型授权），因此只在用户于 setup 明确启用、
且安装了 `pyannote.audio` 时才可用。未安装时调用返回带安装指引的 RuntimeError
（复用 mlx-whisper 的可选依赖模式）。

典型用法：
    from app.services.diarization import diarize_audio, assign_speakers
    turns = diarize_audio(wav_path, hf_token=..., num_speakers=2)   # [{start, end, speaker}]
    segments = assign_speakers(transcript_segments, turns)           # 给每段填 speaker

注意：pyannote 的 gated 模型（speaker-diarization-3.1）需要用户先在
huggingface.co 同意模型授权条款，并用本人 HF_TOKEN 调用。
"""
from __future__ import annotations

import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

_DIARIZATION_MODEL = "pyannote/speaker-diarization-3.1"
_INSTALL_HINT = (
    "说话人分离需要 pyannote.audio（含 torch）。请用 "
    "`uv tool install --from git+https://github.com/HuangYincan/VideoNote-MCP videonote "
    "--with pyannote.audio --with torch`（或 `uvx --from ... --with pyannote.audio --with torch`）"
    "安装；并在 huggingface.co 同意 pyannote/speaker-diarization-3.1 模型授权，"
    "设置 HF_TOKEN（`videonote transcriber diarization on` 时会引导）。"
)


def diarize_audio(
    wav_path: str,
    hf_token: Optional[str] = None,
    num_speakers: Optional[int] = None,
) -> List[dict]:
    """对 16kHz mono wav 做说话人分离，返回 [{start, end, speaker}] turns。

    - wav_path: 需已归一化为 wav（见 app/transcriber/audio_preprocess.normalize_to_wav）；
    - hf_token: HuggingFace token（缺省取环境变量 HUGGINGFACE_HUB_TOKEN）；
    - num_speakers: 说话人数提示（可选，缺省自动检测）。
    未安装 pyannote → RuntimeError 带安装指引。
    """
    if not os.path.exists(wav_path):
        raise FileNotFoundError(f"文件不存在: {wav_path}")
    try:
        from pyannote.audio import Pipeline
    except ImportError:
        raise RuntimeError(_INSTALL_HINT)

    token = hf_token or os.environ.get("HUGGINGFACE_HUB_TOKEN") or ""
    if not token:
        try:
            from videonote_mcp.config import get_app_config

            token = (get_app_config().get("hf_token") or "").strip()
        except Exception:
            token = ""
    if not token:
        raise RuntimeError(
            "说话人分离需要 HF_TOKEN（HuggingFace token）。请设置环境变量 "
            "HUGGINGFACE_HUB_TOKEN，或跑 `videonote setup` 在向导里保存。"
        )

    try:
        pipeline = Pipeline.from_pretrained(_DIARIZATION_MODEL, token=token)
    except Exception as exc:  # noqa: BLE001 —— 模型加载/授权失败
        raise RuntimeError(
            f"pyannote 模型加载失败（可能需要先在 huggingface.co 同意模型授权）: {exc}"
        )

    if num_speakers is not None and (not isinstance(num_speakers, int) or num_speakers < 1):
        logger.warning(f"num_speakers={num_speakers!r} 无效（需 ≥1 的整数），回退自动检测")
        num_speakers = None
    kwargs = {"num_speakers": num_speakers} if num_speakers else {}
    diarization = pipeline(wav_path, **kwargs)

    turns: List[dict] = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        turns.append(
            {
                "start": round(float(turn.start), 3),
                "end": round(float(turn.end), 3),
                "speaker": str(speaker),
            }
        )
    return turns


def assign_speakers(
    segments: List,
    turns: List[dict],
) -> List:
    """把说话人 turns 与转写段按时间重叠对齐，给每段填 speaker 字段。

    返回新的 segment 列表（不改原对象）。每段取与其重叠时长最大的 speaker turn；
    无重叠时 speaker 保持 None。
    """
    result = []
    for seg in segments:
        speaker = _best_speaker(seg.start, seg.end, turns)
        result.append(_with_speaker(seg, speaker))
    return result


def _best_speaker(start: float, end: float, turns: List[dict]) -> Optional[str]:
    best_speaker = None
    best_overlap = 0.0
    for t in turns:
        ov = min(end, t["end"]) - max(start, t["start"])
        if ov > best_overlap:
            best_overlap = ov
            best_speaker = t["speaker"]
    return best_speaker if best_overlap > 0 else None


def _with_speaker(seg, speaker: Optional[str]):
    from app.models.transcriber_model import TranscriptSegment

    if isinstance(seg, TranscriptSegment):
        return TranscriptSegment(
            start=seg.start, end=seg.end, text=seg.text, speaker=speaker
        )
    # dict 形态（asdict 结果）：补 speaker 键
    d = dict(seg)
    d["speaker"] = speaker
    return d
