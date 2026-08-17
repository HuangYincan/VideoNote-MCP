import os
import platform
import threading
from pathlib import Path

from huggingface_hub import snapshot_download

from app.decorators.timeit import timeit
from app.events import transcription_finished
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.transcriber.base import Transcriber
from app.utils.logger import get_logger
from app.utils.path_helper import get_model_dir

logger = get_logger(__name__)


# mlx-community 上的 Whisper 仓库命名不统一：常规版本是 'whisper-{size}-mlx'，
# turbo 例外没有 -mlx 后缀。直接拼 'mlx-community/whisper-{size}' 会 404。
# 已用 https://huggingface.co/api/models?author=mlx-community&search=whisper 核对过。
MLX_MODEL_MAP = {
    "tiny": "mlx-community/whisper-tiny-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "large-v1": "mlx-community/whisper-large-v1-mlx",
    "large-v2": "mlx-community/whisper-large-v2-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}


def resolve_mlx_repo_id(model_size: str) -> str:
    if model_size not in MLX_MODEL_MAP:
        raise ValueError(
            f"不支持的 MLX Whisper 模型大小: {model_size}。"
            f"可选: {', '.join(MLX_MODEL_MAP.keys())}"
        )
    return MLX_MODEL_MAP[model_size]


class MLXWhisperTranscriber(Transcriber):
    def __init__(
            self,
            model_size: str = "base"
    ):
        # 检查平台
        if platform.system() != "Darwin":
            raise RuntimeError("MLX Whisper 仅支持 Apple 平台")

        # 注意：不做 TRANSCRIBER_TYPE 环境变量检查。引擎切换是写 config JSON
        #（TranscriberConfigManager / set_transcriber），不写环境变量；此处由
        # get_transcriber 按类型路由构造，env 检查只会让配置好的 mlx-whisper 构造即报错。

        self.model_size = model_size
        self.model_name = resolve_mlx_repo_id(model_size)
        self.model_path = None

        # 共享单例上的转写锁：mlx_whisper.transcribe 每次调用都会加载模型，并发调用需串行化
        self._lock = threading.Lock()
        
        # 设置模型路径
        model_dir = get_model_dir("mlx-whisper")
        self.model_path = os.path.join(model_dir, self.model_name)
        # 用 config.json 而非目录存在作为「下载完成」的判据，
        # 同 fast-whisper 的 model.bin：避免半成品目录把后续下载吞掉
        config_file = Path(self.model_path) / "config.json"
        if not config_file.exists():
            if Path(self.model_path).exists():
                logger.warning(
                    f"MLX 模型目录 {self.model_path} 存在但 config.json 缺失（上次下载未完成），重新下载"
                )
            else:
                logger.info(f"模型 {self.model_name} 不存在，开始下载...")
            snapshot_download(
                self.model_name,
                local_dir=self.model_path,
                local_dir_use_symlinks=False,
            )
            logger.info("模型下载完成")
        
        logger.info(f"初始化 MLX Whisper 转录器，模型：{self.model_name}")

    @timeit
    def transcript(self, file_path: str) -> TranscriptResult:
        with self._lock:
            # 惰性导入：mlx-whisper 是可选 extras（pyproject `mlx`），未装时给安装提示
            # 而非 ModuleNotFoundError 天书（与 diarization 的可选依赖模式同款）。
            # 同时保证 MLX_MODEL_MAP 等元数据在未装时仍可 import（#126 C4 前置校验）。
            try:
                import mlx_whisper
            except ImportError:
                raise RuntimeError(
                    "mlx-whisper 未安装：请用 `uvx --with mlx-whisper --from "
                    "git+https://github.com/HuangYincan/VideoNote-MCP videonote ...` 安装"
                )
            try:
                # 使用 MLX Whisper 进行转录
                # 必须传本地模型目录（__init__ 已 snapshot_download 到 model_path）：
                # 传 repo_id 会走默认 HF cache 重新加载/下载，自定义目录白占空间、离线必失败
                local_path = self.model_path if (self.model_path and os.path.exists(self.model_path)) else self.model_name
                result = mlx_whisper.transcribe(
                    file_path,
                    path_or_hf_repo=local_path
                )

                # 转换为标准格式
                segments = []

                for segment in result["segments"]:
                    text = segment["text"].strip()
                    segments.append(TranscriptSegment(
                        start=segment["start"],
                        end=segment["end"],
                        text=text
                    ))

                transcript_result = TranscriptResult(
                    language=result.get("language", "unknown"),
full_text=" ".join(seg.text for seg in segments).strip(),
                    segments=segments,
                    raw=result
                )

                # self.on_finish(file_path, transcript_result)
                return transcript_result

            except Exception as e:
                logger.error(f"MLX Whisper 转写失败：{e}")
                raise e

    def on_finish(self, video_path: str, result: TranscriptResult) -> None:
        logger.info("MLX Whisper 转写完成")
        transcription_finished.send({
            "file_path": video_path,
        }) 