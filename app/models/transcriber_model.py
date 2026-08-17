
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class TranscriptSegment:
    start: float               # 开始时间（秒）
    end: float                 # 结束时间（秒）
    text: str                  # 该段文字
    speaker: Optional[str] = None   # 说话人标签（pyannote 分离后填充，默认 None 向后兼容）

@dataclass
class TranscriptResult:
    language: Optional[str]         # 检测语言（如 "zh"、"en"）
    full_text: str                  # 完整合并后的文本（用于摘要）
    segments: List[TranscriptSegment]  # 分段结构，适合前端显示时间轴字幕等
    raw: Optional[dict] = None      # 原始响应数据，便于调试或平台特性处理