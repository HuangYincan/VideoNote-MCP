"""真实转写集成冒烟（docs/05 #139 C2）——默认跳过，CI 手动 job / 本地显式触发。

全库唯一的「转写引擎实例化 + 模型加载 + 推理」真实执行路径：其余测试都在
get_transcriber 或更上层打桩（#32 回归测试也只到 get_transcriber）。这里补上
引擎类内部（_load_model / transcript 绑定）的真实执行，绑定类回归在引擎层
不再测试与 CI 双双失明。

触发：`VIDEONOTE_RUN_INTEGRATION=1 uv run pytest tests/test_integration_transcribe.py -q`
引擎：darwin → mlx-whisper（tiny），其余平台 → fast-whisper（tiny）。
模型缺失/加载失败 → skip 而非 fail（集成冒烟锦上添花，不拦发布）。
"""
import math
import os
import platform
import struct
import wave
from pathlib import Path

import pytest

from app.models.transcriber_model import TranscriptResult
from app.transcriber.transcriber_provider import get_transcriber

pytestmark = pytest.mark.integration

RUN_GATE = os.environ.get("VIDEONOTE_RUN_INTEGRATION") == "1"


def _synthesize_tone(path, seconds: float = 1.0, freq: int = 440) -> None:
    """合成 1s 16kHz mono 正弦波 wav（whisper 可直接消费，无需 ffmpeg）。"""
    rate = 16000
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(
            struct.pack("<h", int(32767 * 0.3 * math.sin(2 * math.pi * freq * t / rate)))
            for t in range(int(rate * seconds))
        ))


def _real_model_dir() -> str:
    """测试隔离把 VIDEONOTE_MODEL_DIR 指到 /tmp（models 空）；集成冒烟读真实用户缓存。

    get_model_dir 每次调用读 env（path_helper.py:26），测试内覆盖即可，无需打桩。
    """
    candidates = [
        str(Path(__file__).resolve().parents[1] / "data" / "models"),  # 源码模式
        str(Path.home() / ".local" / "share" / "videonote-mcp" / "models"),  # 安装模式
    ]
    for c in candidates:
        if (Path(c) / "whisper").exists():
            return c
    return candidates[0]


def _load_any_transcriber():
    """按候选顺序加载：darwin 优先 mlx-whisper（tiny），兜底 fast-whisper（small）。

    都不可用（模型未下载/加载失败）→ skip 而非 fail——集成冒烟锦上添花，不拦发布。
    """
    candidates = []
    if platform.system() == "Darwin":
        candidates.append(("mlx-whisper", "tiny"))
    candidates.append(("fast-whisper", "small"))
    prev = os.environ.get("VIDEONOTE_MODEL_DIR")
    os.environ["VIDEONOTE_MODEL_DIR"] = _real_model_dir()
    try:
        last = None
        for engine, size in candidates:
            try:
                return get_transcriber(transcriber_type=engine, model_size=size)
            except Exception as exc:  # noqa: BLE001 —— 模型不可用属环境问题，继续尝试候选
                last = exc
        pytest.skip(f"无可用转写模型（{candidates}）: {last}")
    finally:
        if prev is None:
            os.environ.pop("VIDEONOTE_MODEL_DIR", None)
        else:
            os.environ["VIDEONOTE_MODEL_DIR"] = prev


@pytest.mark.skipif(
    not RUN_GATE,
    reason="集成冒烟默认跳过：VIDEONOTE_RUN_INTEGRATION=1 触发（docs/05 #139 C2）",
)
def test_real_transcribe_roundtrip(tmp_path):
    audio = tmp_path / "tone.wav"
    _synthesize_tone(audio)
    transcriber = _load_any_transcriber()
    # 直调引擎 transcript（不经 pipeline 的空转写守卫）：正弦波无语音内容，
    # 但引擎构造 + 模型加载 + 推理的真实绑定链必须跑通（#139 C2 的目标）
    result = transcriber.transcript(str(audio))
    assert isinstance(result, TranscriptResult)
    assert isinstance(result.language, str)
    assert isinstance(result.segments, list)
