import shutil
import threading
from pathlib import Path

from faster_whisper import WhisperModel

from app.decorators.timeit import timeit
from app.exceptions.task import TaskCancelledError, check_cancel
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.transcriber.base import Transcriber
from app.transcriber.whisper_models import (
    hf_cache_dirname,
    is_local_target,
    resolve_whisper_model,
    resolve_whisper_revision,
)
from app.utils.env_checker import (
    is_ct2_cuda_available,
    is_cuda_available,
    is_torch_installed,
)
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

        # 设备哨兵：把最终解析结果打一行结构化日志。
        # 没有它，「实际跑在 GPU 还是 CPU」只能靠推断——而静默回落（以为在 GPU、其实
        # 在 CPU）是最难发现的失败模式：任务照常成功、只是耗时没变，health_check 也不
        # 暴露 device。这行日志是唯一的直接证据，也便于 grep 验收。
        #
        # torch_installed 只是诊断信息，探测本身绝不允许影响构造：device='cpu' 是显式
        # 短路（按设计完全不碰探测），若让 torch 安装损坏时的 OSError 从这里漏出去，
        # 会把「显式指定 CPU」这条原本安全的路径变成构造失败。
        try:
            torch_installed = is_torch_installed()
        except Exception:  # noqa: BLE001 —— 诊断信息，不阻断构造
            torch_installed = False
        logger.info(
            f"推理设备已解析: device={self.device} compute_type={self.compute_type} "
            f"model={self.model_size} torch_installed={torch_installed}"
        )

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
                self.model = self._build_model(model_size, model_dir)
            else:
                # 网络瞬时故障/404/参数错误不 purge、也不立刻再下一次（#145 B6）：
                # 旧代码注释写「等再次加载时自然重试」，实现却无条件 _build_model，
                # 把 HF 404/超时放大一倍。
                logger.warning(f"加载 whisper-{model_size} 失败（非 cache 损坏，不清理）: {e}")
                raise

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
        # 顺序即语义：whisper 的推理内核全部来自 ctranslate2，与 torch 无关；torch 在本项目
        # 里只是 funasr / diarization 两个可选重依赖。所以先问「真正跑推理的引擎能不能用
        # GPU」，torch 只作兜底（历史机器上 torch 自带 CUDA runtime、并把 torch/lib 加进
        # DLL 搜索路径，那种情况下两种判据通常同时为真）。
        #
        # 反例（必须先问 ctranslate2 的原因）：Windows 上从 PyPI 装 torch 默认是 CPU-only
        # wheel，而 pyproject 的 funasr / diarization 两个 extra 都会装 torch。若把
        # is_torch_installed() 排在前面，这类用户会被直接判成 CPU —— 即使 ctranslate2 的
        # CUDA 后端完全可用，这正是本次要修的「判据错位」。
        #
        # 不变量：本函数返回值 ⊇ 旧实现（旧的 is_cuda_available() 判定被完整保留为第二
        # 个分支），所以只可能把「误判为 CPU」修正成 GPU，绝不会把原本的 GPU 判成 CPU。
        try:
            if is_ct2_cuda_available():
                logger.info(" ctranslate2 的 CUDA 后端可用，使用 GPU")
                return True
            if is_cuda_available():
                logger.info(" CUDA 可用，使用 GPU")
                return True
            logger.warning(
                " 未检测到可用的 GPU 推理后端，回退 CPU：可能是无 N 卡 / 驱动过旧 / 缺 "
                "CUDA 运行时库（cuBLAS 12）。注意 whisper 推理不依赖 torch，装 torch "
                "并不会让 whisper 用上 GPU；请检查 CUDA 运行时库与 CUDA_PATH。"
            )
            return False
        except ImportError:
            return False

    @timeit
    def transcript(
        self,
        file_path: str,
        cancel_event: threading.Event = None,
    ) -> TranscriptResult:
        # fast-whisper 模型非线程安全：共享单例上串行化转写（正确性优先于该步骤并行度）。
        # 锁须覆盖 transcribe 调用和 segments 生成器迭代（生成器同样读取共享模型）。
        with self._lock:
            check_cancel(cancel_event)
            # 自愈：模型只在 __init__ 构建，而 close() 会把它置空且没有重载路径。
            # 空闲释放（transcriber_provider._release_idle_gpu）可能在本实例被某个任务
            # 取走之后、进入本锁之前把它卸载 —— 那段窗口里没人持实例 _lock，靠互斥无法
            # 消除。没有这一层，任务会以 `'NoneType' object has no attribute 'transcribe'`
            # 失败；预处理分块路径更糟：异常被吞成「跳过该块」，任务 SUCCESS 但内容缺失。
            # 与 funasr 的 _ensure_model() 同款设计：用到才建，把致命错误降级为一次重载。
            if self.model is None:
                logger.info("模型已被空闲释放卸载，按原尺寸重建")
                self.model = self._build_model(self.model_size, get_model_dir("whisper"))
            try:

                segments_raw, info = self.model.transcribe(file_path)
                check_cancel(cancel_event)

                segments = []

                for seg in segments_raw:
                    check_cancel(cancel_event)
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
            except TaskCancelledError:
                raise
            except Exception as e:
                # 抛给调用方（note._transcribe_audio 捕获并写入 FAILED 状态）；不要返回 None，
                # 否则上层 asdict(None) 会报误导性的 TypeError
                logger.error(f"转写失败：{e}")
                raise
            finally:
                # GPU 用完就重置空闲释放计时器（见 transcriber_provider.schedule_gpu_idle_release）：
                # 空闲超时后卸载模型、归还显存，避免常驻挤压其它 GPU 应用。CPU 无显存可释放，
                # 不挂计时器。延迟导入避免与 transcriber_provider 形成循环依赖。
                # 用 getattr 取值：本类实例可能未经 __init__ 构造（测试用 object.__new__ 覆盖
                # transcript() 的组装语义），裸读 self.device 会在 finally 里抛 AttributeError
                # 并顶掉 return —— 那是回归。
                if getattr(self, "device", None) == "cuda":
                    try:
                        from app.transcriber.transcriber_provider import (
                            schedule_gpu_idle_release,
                        )

                        schedule_gpu_idle_release()
                    except Exception:  # noqa: BLE001 —— 计时器失败不影响转写结果
                        pass


    def close(self) -> None:
        """释放底层模型引用（#127 B3）：切换模型尺寸时 transcriber_provider 调
        close 让旧 large-v3（~3GB）尽快 GC，不再双驻留撑内存。"""
        self.model = None

