import shutil
import threading
from pathlib import Path

from faster_whisper import WhisperModel

from app.decorators.timeit import timeit
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.transcriber.base import Transcriber
from app.transcriber.whisper_models import (
    hf_cache_dirname,
    is_local_target,
    resolve_whisper_model,
    resolve_whisper_revision,
)
from app.utils.env_checker import is_cuda_available, is_torch_installed
from app.utils.logger import get_logger
from app.utils.path_helper import get_model_dir

'''
 Size of the model to use (tiny, tiny.en, base, base.en, small, small.en, distil-small.en, medium, medium.en, distil-medium.en, large-v1, large-v2, large-v3, large, distil-large-v2, distil-large-v3, large-v3-turbo, or turbo
'''
logger=get_logger(__name__)

# 历史遗留：之前用 modelscope 下载到自定义目录然后把路径传给 WhisperModel。
# 但 faster-whisper 1.1.1 的 download_model（utils.py:76）逻辑是：
# 只要 size_or_id 里含 "/" 就当 HF repo_id 处理，没有「本地目录直接返回」分支。
# 我们传 /app/models/whisper/whisper-tiny 进去 → 被当成不存在的 HF repo →
# 在线请求失败 → fallback local_files_only=True → HF cache 找不到（因为是
# modelscope 目录布局不是 HF）→ LocalEntryNotFoundError，误导说"离线模式"。
# 解法：彻底让 faster-whisper 自己处理下载——传 size name，配 download_root
# 作为 HF cache 根目录，HF_ENDPOINT 已经在 Dockerfile 里指到 hf-mirror.com，
# 国内能用。删掉 modelscope 那一套，避免布局不匹配。
class WhisperTranscriber(Transcriber):
    def __init__(
            self,
            model_size: str = "small",
            device: str = 'cpu',
            compute_type: str = None,
            cpu_threads: int = 1,
    ):
        if device == 'cpu' or device is None:
            self.device = 'cpu'
        else:
            self.device = "cuda" if WhisperTranscriber.is_cuda() else "cpu"
            if device == 'cuda' and self.device == 'cpu':
                logger.info('没有 cuda 使用 cpu进行计算')

        self.compute_type = compute_type or ("float16" if self.device == "cuda" else "int8")
        self.model_size = model_size

        # 共享单例上的转写锁：模型加载前就建好，即使加载失败锁也始终存在
        self._lock = threading.Lock()

        model_dir = get_model_dir("whisper")
        try:
            self.model = self._build_model(model_size, model_dir)
        except Exception as e:
            if WhisperTranscriber._is_cache_error(e):
                # 自愈：损坏 / 截断 / 半成品 cache → 删掉对应 HF cache 重下一次
                logger.warning(f"加载 whisper-{model_size} 失败（cache 损坏）：{e}；清理 cache 后重新下载")
                WhisperTranscriber._purge_cache(model_dir, model_size)
            else:
                # 网络瞬时故障/404/参数错误不 purge：删掉只会丢失可断点续传的
                # 半截下载，等再次加载时自然重试（#124 B18）
                logger.warning(f"加载 whisper-{model_size} 失败（非 cache 损坏，不清理）: {e}")
            self.model = self._build_model(model_size, model_dir)

    def _build_model(self, model_size: str, model_dir: str) -> WhisperModel:
        # resolve 把模型名映射成可加载标识：内置 size→Systran repo_id、自定义映射、
        # 直通的 repo_id 或本地路径。faster-whisper 对本地目录走 os.path.isdir 分支，
        # 对 repo_id 走 download_model(cache_dir=download_root)，两者都吃 model_size_or_path。
        # revision 固定到 BUILTIN_WHISPER_REVISIONS（#142 A2）：同尺寸跨时间下载一致；
        # 自定义/直通/本地路径返回 None（版本由使用者自管，faster-whisper>=1.2.0 才支持）。
        target = resolve_whisper_model(model_size)
        revision = resolve_whisper_revision(model_size)
        return WhisperModel(
            model_size_or_path=target,
            device=self.device,
            compute_type=self.compute_type,
            download_root=model_dir,
            revision=revision,
        )

    @staticmethod
    def _is_cache_error(exc: Exception) -> bool:
        """加载失败是否属 cache 损坏类（才值得删了重下，#124 B18）。

        - LocalEntryNotFoundError：HF cache 目录存在但快照不完整/校验失败——删掉重下；
        - 其余 OSError：本地文件系统错误（截断/权限/磁盘），几乎都关联 cache 状态。
        网络层错误不 purge——删了只会丢掉可断点续传的半截下载。注意内建
        ConnectionError/TimeoutError（含 socket 超时）也是 OSError 子类，必须显式排除。
        404（EntryNotFoundError）、参数错误同样不 purge。
        本地路径模型的 FileNotFoundError 也是 OSError 子类，会误判为 cache
        错误——但 _purge_cache 对 is_local_target 直接返回不删，无数据风险。
        """
        try:
            from huggingface_hub.utils import LocalEntryNotFoundError
        except ImportError:  # huggingface_hub 版本差异：退化为仅本地文件错误
            LocalEntryNotFoundError = ()
        if isinstance(exc, LocalEntryNotFoundError):
            return True
        if not isinstance(exc, OSError):
            return False
        # OSError 中的网络类（socket 超时 / 连接重置 / 对端断开）属瞬时故障
        if isinstance(exc, (ConnectionError, TimeoutError, BrokenPipeError)):
            return False
        return True

    @staticmethod
    def _purge_cache(model_dir: str, model_size: str) -> None:
        """加载失败时清掉对应 HF cache 的 snapshot 目录，强制下次重下。

        关键：本地路径模型**绝不删**——那是用户自己的文件，删了就是数据丢失；
        只清 HF cache 布局 <model_dir>/models--{org}--{name}/（含历史 modelscope 目录）。
        """
        try:
            target = resolve_whisper_model(model_size)
        except Exception:
            target = model_size
        if is_local_target(target):
            logger.warning(
                f"模型 {model_size} 指向本地路径 {target}，加载失败不清理用户文件，请检查该目录是否完整"
            )
            return
        candidates = [
            Path(model_dir) / hf_cache_dirname(target),       # HF cache: models--org--name
            Path(model_dir) / f"whisper-{model_size}",        # 历史 modelscope 目录，顺手清掉
        ]
        for path in candidates:
            if path.exists():
                logger.info(f"清理损坏 cache: {path}")
                shutil.rmtree(path, ignore_errors=True)
    @staticmethod
    def is_cuda() -> bool:
        try:
            if is_cuda_available():
                logger.info(" CUDA 可用，使用 GPU")
                return True
            elif is_torch_installed():
                logger.info(" 只装了 torch，但没有 CUDA，用 CPU")
                return False
            else:
                logger.warning(" 还没有安装 torch，请先安装")
                return False

        except ImportError:
            return False

    @timeit
    def transcript(self, file_path: str) -> TranscriptResult:
        # fast-whisper 模型非线程安全：共享单例上串行化转写（正确性优先于该步骤并行度）。
        # 锁须覆盖 transcribe 调用和 segments 生成器迭代（生成器同样读取共享模型）。
        with self._lock:
            try:

                segments_raw, info = self.model.transcribe(file_path)

                segments = []

                for seg in segments_raw:
                    text = seg.text.strip()
                    segments.append(TranscriptSegment(
                        start=seg.start,
                        end=seg.end,
                        text=text
                    ))

                result = TranscriptResult(
                    language=info.language,
full_text=" ".join(seg.text for seg in segments).strip(),
                    segments=segments,
                    raw=info
                )
                return result
            except Exception as e:
                # 抛给调用方（note._transcribe_audio 捕获并写入 FAILED 状态）；不要返回 None，
                # 否则上层 asdict(None) 会报误导性的 TypeError
                logger.error(f"转写失败：{e}")
                raise


    def close(self) -> None:
        """释放底层模型引用（#127 B3）：切换模型尺寸时 transcriber_provider 调
        close 让旧 large-v3（~3GB）尽快 GC，不再双驻留撑内存。"""
        self.model = None

