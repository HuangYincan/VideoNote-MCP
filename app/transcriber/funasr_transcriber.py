"""FunASR Paraformer-zh 中文转写引擎（可选重依赖）。

中文场景优于 faster-whisper：Paraformer-zh（WER ~8.4%）+ fsmn-vad（自动 VAD）+
ct-punc（自动标点），一个 AutoModel pipeline 端到端输出**已带标点**的中文文本 + 段落时间轴。

依赖较重（torch + funasr + modelscope），因此**惰性加载**：模块顶层不 import funasr，
构造函数里才 import，失败抛 RuntimeError 带安装指引（复用 mlx-whisper / pyannote 的可选依赖模式）。

使用：
    from app.transcriber.funasr_transcriber import FunASRTranscriber
    tr = FunASRTranscriber(device="cpu")
    result = tr.transcript("audio.wav")   # -> TranscriptResult（中文，带标点）
"""
from __future__ import annotations

import logging
import threading
from typing import List

from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.transcriber.base import Transcriber
from app.utils.path_helper import get_model_dir

logger = logging.getLogger(__name__)

_INSTALL_HINT = (
    "FunASR 转写需要 funasr + torch。请用 "
    "`uv tool install --from git+https://github.com/HuangYincan/VideoNote-MCP videonote "
    "--with funasr --with torch`（或 `uvx --from ... --with funasr --with torch`）安装；"
    "或切换转写引擎 `videonote transcriber set fast-whisper` / groq。"
)


class FunASRTranscriber(Transcriber):
    """FunASR Paraformer-zh（中文 ASR + VAD + 标点）。"""

    def __init__(self, device: str = "cpu"):
        # cuda 探测回退（#127 B1）：transcriber_provider 默认传 device="cuda"，
        # 无 CUDA 机器（Mac 全系/无 N 卡 Linux）构造 AutoModel 即崩——与 whisper 同款兜底
        self.device = FunASRTranscriber._resolve_device(device or "cpu")
        # 共享单例上的转写锁：funasr 模型并发调用需串行化
        self._lock = threading.Lock()
        self._model = None
        # 模型下载缓存目录：<数据目录>/models/funasr
        self._cache_dir = get_model_dir("funasr")

    @staticmethod
    def _resolve_device(requested: str) -> str:
        """请求 cuda 但探测不到 → 回退 cpu（其余设备名按原样透传）。"""
        if requested == "cuda":
            try:
                from app.utils.env_checker import is_cuda_available

                if is_cuda_available():
                    return "cuda"
                logger.warning("FunASR 请求 cuda 但 CUDA 不可用，回退 cpu")
            except ImportError:
                logger.warning("FunASR 请求 cuda 但 torch 不可用，回退 cpu")
            return "cpu"
        return requested

    def close(self) -> None:
        """释放模型引用（#127 B3）：切换引擎时 transcriber_provider 调 close 触发 GC。"""
        self._model = None

    def _ensure_model(self):
        """惰性加载 AutoModel（VAD + ASR + 标点 一个 pipeline）。"""
        if self._model is not None:
            return self._model
        try:
            from funasr import AutoModel
        except ImportError:
            raise RuntimeError(_INSTALL_HINT)
        self._model = AutoModel(
            model="paraformer-zh",
            vad_model="fsmn-vad",
            punc_model="ct-punc",
            device=self.device,
            model_cache_dir=self._cache_dir,
            disable_update=True,
        )
        return self._model

    def transcript(self, file_path: str) -> TranscriptResult:
        with self._lock:
            model = self._ensure_model()
            try:
                results = model.generate(input=file_path, batch_size_s=300)
            except Exception as exc:  # noqa: BLE001 —— funasr 内部异常透传
                raise RuntimeError(f"FunASR 转写失败: {exc}")

            if not results:
                # 空结果可能是静音（合法），也可能是引擎异常——留痕供排查（#118）
                logger.warning("funasr 未返回识别结果（静音/无语音，或引擎异常），返回空转写")
                return TranscriptResult(language="zh", full_text="", segments=[])

            res = results[0]
            text = (res.get("text") or "").strip()
            sentence_info = res.get("sentence_info") or []

            segments: List[TranscriptSegment] = []
            if sentence_info:
                for seg in sentence_info:
                    start_ms = int(seg.get("start") or 0)
                    end_ms = int(seg.get("end") or start_ms)
                    seg_text = (seg.get("text") or "").strip()
                    if not seg_text:
                        continue
                    segments.append(
                        TranscriptSegment(
                            start=round(start_ms / 1000.0, 3),
                            end=round(end_ms / 1000.0, 3),
                            text=seg_text,
                        )
                    )
            if not segments and text:
                # 无 sentence_info（如极短音频）：整段作为单段
                segments.append(TranscriptSegment(start=0.0, end=0.0, text=text))

            return TranscriptResult(
                language="zh",
                full_text=text,
                segments=segments,
                raw={"engine": "funasr", "sentence_info": sentence_info},
            )
