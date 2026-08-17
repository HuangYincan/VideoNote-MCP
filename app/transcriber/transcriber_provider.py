import os
import platform
import threading
from enum import Enum

from app.transcriber.bcut import BcutTranscriber
from app.transcriber.groq import GroqTranscriber
from app.transcriber.kuaishou import KuaishouTranscriber
from app.transcriber.whisper import WhisperTranscriber
from app.utils.logger import get_logger

logger = get_logger(__name__)

class TranscriberType(str, Enum):
    FAST_WHISPER = "fast-whisper"
    MLX_WHISPER = "mlx-whisper"
    BCUT = "bcut"
    KUAISHOU = "kuaishou"
    GROQ = "groq"
    FUNASR = "funasr"

# 在 Apple 平台尝试导入 MLX Whisper（不再依赖环境变量，支持前端动态切换）
MLX_WHISPER_AVAILABLE = False
if platform.system() == "Darwin":
    try:
        from app.transcriber.mlx_whisper_transcriber import MLXWhisperTranscriber
        MLX_WHISPER_AVAILABLE = True
        logger.info("MLX Whisper 可用，已导入")
    except ImportError:
        logger.warning("MLX Whisper 导入失败，可能未安装 mlx_whisper")

logger.info('初始化转录服务提供器')

# 转录器单例缓存
_transcribers = {
    TranscriberType.FAST_WHISPER: None,
    TranscriberType.MLX_WHISPER: None,
    TranscriberType.BCUT: None,
    TranscriberType.KUAISHOU: None,
    TranscriberType.GROQ: None,
    TranscriberType.FUNASR: None,
}

# 构造单例的锁：并发首个任务同时首次加载 whisper 模型时，只允许一个线程真正构造
_cache_lock = threading.Lock()

# 公共实例初始化函数
def _init_transcriber(key: TranscriberType, cls, *args, **kwargs):
    # 已存在实例且模型尺寸不同 → 重建（否则切模型尺寸后拿到的仍是首次构造的实例，
    # set_transcriber 配置的 large-v3 永远不会生效）。模型在构造时即加载完毕。
    want_size = kwargs.get("model_size")
    existing = _transcribers[key]
    need_build = existing is None or (
        want_size is not None and getattr(existing, "model_size", None) != want_size
    )
    if need_build:
        # 双重检查：防止两个并发任务同时首次构造/重建（whisper 模型加载很重）
        with _cache_lock:
            existing = _transcribers[key]
            need_build = existing is None or (
                want_size is not None and getattr(existing, "model_size", None) != want_size
            )
            if need_build:
                logger.info(f'创建 {cls.__name__} 实例: {key} (model_size={want_size})')
                try:
                    # 替换旧实例前防御性释放（whisper 大模型约 3GB，双驻留会撑爆内存）
                    old = _transcribers.get(key)
                    if old is not None:
                        close = getattr(old, "close", None)
                        if callable(close):
                            try:
                                # 持旧实例转写锁再 close：whisper close() 置 self.model=None，
                                # 若另一线程正持旧实例转写（with self._lock 内 transcribe），
                                # 无锁置空会让进行中调用读空模型异常退出（#129 B7，窄窗口）
                                lock = getattr(old, "_lock", None)
                                if lock is not None:
                                    with lock:
                                        close()
                                else:
                                    close()
                            except Exception as exc:
                                logger.warning(f'释放旧 {cls.__name__} 实例失败: {exc}')
                    _transcribers[key] = cls(*args, **kwargs)
                    logger.info(f'{cls.__name__} 创建成功')
                except Exception as e:
                    logger.error(f"{cls.__name__} 创建失败: {e}")
                    raise
    return _transcribers[key]

# 各类型获取方法
def get_groq_transcriber():
    return _init_transcriber(TranscriberType.GROQ, GroqTranscriber)

def get_whisper_transcriber(model_size="small", device="cuda"):
    return _init_transcriber(TranscriberType.FAST_WHISPER, WhisperTranscriber, model_size=model_size, device=device)

def get_bcut_transcriber():
    # bcut 有请求级状态（task_id/上传分片/download_url），并发任务必须各用各的实例
    return BcutTranscriber()

def get_kuaishou_transcriber():
    # 快手转写器内部有请求级状态，同样不能共享单例
    return KuaishouTranscriber()

def get_mlx_whisper_transcriber(model_size="small"):
    if not MLX_WHISPER_AVAILABLE:
        logger.warning("MLX Whisper 不可用，请确保在 Apple 平台且已安装 mlx_whisper")
        raise ImportError("MLX Whisper 不可用")
    return _init_transcriber(TranscriberType.MLX_WHISPER, MLXWhisperTranscriber, model_size=model_size)

def get_funasr_transcriber(device="cpu"):
    from app.transcriber.funasr_transcriber import FunASRTranscriber
    return _init_transcriber(TranscriberType.FUNASR, FunASRTranscriber, device=device)

# 通用入口
def get_transcriber(transcriber_type="fast-whisper", model_size=None, device="cuda"):
    """
    获取指定类型的转录器实例

    参数:
        transcriber_type: 支持 "fast-whisper", "mlx-whisper", "bcut", "kuaishou", "groq"
        model_size: 模型大小，适用于 whisper 类；显式传入优先于环境变量
            （WHISPER_MODEL_SIZE 仅作兜底，避免 setup/set_transcriber 配置的
            模型尺寸被环境变量覆盖——那是此前模型切换不生效的根因）
        device: 设备类型（如 cuda / cpu），仅 whisper 使用

    返回:
        对应类型的转录器实例
    """
    logger.info(f'请求转录器类型: {transcriber_type}')

    try:
        transcriber_enum = TranscriberType(transcriber_type)
    except ValueError:
        logger.warning(f'未知转录器类型 "{transcriber_type}"，默认使用 fast-whisper')
        transcriber_enum = TranscriberType.FAST_WHISPER

    whisper_model_size = model_size or os.environ.get("WHISPER_MODEL_SIZE") or "small"

    if transcriber_enum == TranscriberType.FAST_WHISPER:
        return get_whisper_transcriber(whisper_model_size, device=device)

    elif transcriber_enum == TranscriberType.MLX_WHISPER:
        if not MLX_WHISPER_AVAILABLE:
            raise RuntimeError(
                "MLX Whisper 不可用：需要 macOS 平台并安装 mlx_whisper 包。请用 "
                "`uv tool install --from git+https://github.com/HuangYincan/VideoNote-MCP videonote --with mlx-whisper`"
                "（或 `uvx --from ... --with mlx-whisper`）安装；或切换转写引擎 `videonote transcriber set groq` / fast-whisper"
            )
        return get_mlx_whisper_transcriber(whisper_model_size)

    elif transcriber_enum == TranscriberType.BCUT:
        return get_bcut_transcriber()

    elif transcriber_enum == TranscriberType.KUAISHOU:
        return get_kuaishou_transcriber()

    elif transcriber_enum == TranscriberType.GROQ:
        return get_groq_transcriber()

    elif transcriber_enum == TranscriberType.FUNASR:
        return get_funasr_transcriber(device=device)

    # fallback
    logger.warning(f'未识别转录器类型 "{transcriber_type}"，使用 fast-whisper 作为默认')
    return get_whisper_transcriber(whisper_model_size, device=device)
