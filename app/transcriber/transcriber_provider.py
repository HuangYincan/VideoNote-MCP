import gc
import os
import platform
import threading
import time
from enum import Enum

from app.transcriber.bcut import BcutTranscriber
from app.transcriber.groq import GroqTranscriber
from app.transcriber.kuaishou import KuaishouTranscriber
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
def _get_or_build_transcriber(key: TranscriberType, cls, *args, **kwargs):
    # 已存在实例且模型尺寸不同 → 重建（否则切模型尺寸后拿到的仍是首次构造的实例，
    # CLI transcriber set 配置的 large-v3 永远不会生效）。模型在构造时即加载完毕。
    want_size = kwargs.get("model_size")

    def _usable(inst) -> bool:
        return inst is not None and (
            want_size is None or getattr(inst, "model_size", None) == want_size
        )

    def _hand_out(inst):
        # 取用即刷新空闲释放基准（见 _release_idle_gpu 的复检）：实例一旦交给调用方，
        # 就可能被缓存起来过一会儿才用（预处理模式下中间还隔着一次 ffmpeg 归一化），
        # 这段「已取走但没进 transcript()」的窗口只能靠这个时间戳保护。
        if getattr(inst, "device", None) == "cuda":
            _touch_gpu_use()
        return inst

    inst = _transcribers[key]
    if _usable(inst):
        # 注意：**不能**写成 `return _transcribers[key]` —— 空闲释放会把它置 None，
        # 再读一次就可能把 None 交给调用方（note.py 会据此跳过重取，pipeline 拿它兜底）。
        return _hand_out(inst)

    # 双重检查：防止两个并发任务同时首次构造/重建（whisper 模型加载很重）
    with _cache_lock:
        inst = _transcribers[key]
        if not _usable(inst):
            logger.info(f'创建 {cls.__name__} 实例: {key} (model_size={want_size})')
            try:
                # 先构造新实例，只有成功后才关闭旧实例；否则新模型加载失败时
                # 旧实例仍可复用，不会把缓存留在 model=None 的失效状态（#146 B1）。
                new_transcriber = cls(*args, **kwargs)
                old = inst
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
                _transcribers[key] = new_transcriber
                inst = new_transcriber
                logger.info(f'{cls.__name__} 创建成功')
            except Exception as e:
                logger.error(f"{cls.__name__} 创建失败: {e}")
                raise
        return _hand_out(inst)


# ---------- GPU 显存空闲释放 ----------
# 背景：whisper 单例常驻会把 CTranslate2 的模型（large-v3-turbo fp16 ≈2GB）一直钉在
# 显存里，哪怕几小时不转写也不还回去，挤压游戏 / 绘图 / 其它推理应用。而模型权重是
# 由模型对象持有的（不是 CT2 的 caching allocator），所以只有丢弃对象才能归还显存。
#
# 策略：每次转写结束后重置一个空闲计时器；空闲超过 _GPU_IDLE_RELEASE_SEC 秒就卸载
# 占用 GPU 的转写器并清空单例，下次用到时按 _get_or_build_transcriber 既有逻辑重建。
# 实测重载约 1.7s，批量任务期间计时器会被反复重置、不会重复加载。
#
def _env_int(name: str, default: int, lo: int = None, hi: int = None) -> int:
    """环境变量取整数：未设置 / 非法 → default；越界 → 夹取。

    模块级调用必须容错：非法值（空串、手滑值）若用裸 int() 会在 **import 期** 抛
    ValueError，让整个 transcriber_provider 导入失败 —— 链路是
    server.py → app.services.pipeline → 本模块，于是 **MCP server 直接起不来**
    （连 health_check 都没了，用户还失去了自查手段）。
    与 app/services/note_cache.py 的 _env_int 同口径（那儿已有同样守卫）。
    app/ 是 vendored 层，不能反向 import videonote_mcp.config 的 env_int。
    """
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        val = int(str(raw).strip())
    except (TypeError, ValueError):
        logger.warning("%s=%r 不是整数，按默认值 %s 处理", name, raw, default)
        return default
    if lo is not None and val < lo:
        logger.warning("%s=%s 小于下限 %s，按 %s 处理", name, val, lo, lo)
        return lo
    if hi is not None and val > hi:
        logger.warning("%s=%s 超过上限 %s，按 %s 处理", name, val, hi, hi)
        return hi
    return val


# 上限 86400s（一天）：threading.Timer 的 interval 过大（约 >9.2e9）会让计时器线程
# 抛 OverflowError 立刻死掉 —— 那等于「静默禁用释放」外加每次转写往 stderr 刷 traceback。
# 设 VIDEONOTE_GPU_IDLE_RELEASE_SEC=0 可关闭自动释放（回到常驻行为）。
_GPU_IDLE_RELEASE_SEC = _env_int("VIDEONOTE_GPU_IDLE_RELEASE_SEC", 180, lo=0, hi=86400)
_idle_timer = None
_idle_lock = threading.Lock()
# 计时器代次：Timer.cancel() 对「已经进入回调」的计时器无效，旧回调仍会走到
# _release_idle_gpu 里来。用代次让它认出自己是孤儿并作废 —— 否则它会把 **新** 计时器的
# 引用置空，新计时器从此失去引用（再也 cancel 不到），可能在一批任务中途卸载模型。
_idle_gen = 0
# 最近一次「有任务要 / 刚用完 GPU 单例」的单调时间戳（取用与转写结束都刷新）。
# 释放前复检它，用来挡住「实例已被取走、但还没进 transcript()」的窗口 —— 那个窗口里
# 没人持实例 _lock，只靠 _lock 是挡不住的（预处理模式下该窗口 = 一次 ffmpeg 全轨
# 归一化，实测约 0.6s；多块路径里每块之前也有同量级的空隙）。
_gpu_last_touch = 0.0


def _touch_gpu_use() -> None:
    """标记 GPU 单例刚被使用（取用时 / 转写结束时调用）。"""
    global _gpu_last_touch
    _gpu_last_touch = time.monotonic()


def _arm_idle_timer(delay: float) -> None:
    """挂计时器。**调用方必须已持 `_idle_lock`**。"""
    global _idle_timer, _idle_gen
    _idle_gen += 1
    timer = threading.Timer(delay, _release_idle_gpu, args=(_idle_gen,))
    timer.daemon = True          # 不阻止进程退出
    _idle_timer = timer
    timer.start()


def schedule_gpu_idle_release() -> None:
    """转写结束后调用：重置 GPU 空闲释放计时器。

    计时器在 `_idle_lock` 内替换，保证并发任务不会同时挂出多个计时器；
    旧计时器先 cancel 再挂新的，因此「批量连续任务」期间不会触发卸载。
    """
    if _GPU_IDLE_RELEASE_SEC <= 0:
        return
    _touch_gpu_use()
    with _idle_lock:
        if _idle_timer is not None:
            _idle_timer.cancel()
        _arm_idle_timer(_GPU_IDLE_RELEASE_SEC)


def _release_idle_gpu(gen: int) -> None:
    """空闲超时回调：卸载占用 GPU 的转写器并清空单例，把显存还给驱动。"""
    global _idle_timer
    with _idle_lock:
        if gen != _idle_gen:
            return               # 已被更新的计时器取代（孤儿回调），作废
        _idle_timer = None
    # 复检「真的空闲了吗」：到点之前若有任务取用过单例（_get_or_build_transcriber 会
    # touch），本次释放作废并补挂剩余时间。实例 _lock 只能证明「没有正在进行的
    # transcript()」，不能证明「没有任务持有实例引用」——所以必须有这一层。
    idle_for = time.monotonic() - _gpu_last_touch
    if idle_for < _GPU_IDLE_RELEASE_SEC:
        with _idle_lock:
            if _idle_timer is None:      # 期间没有新计时器才补挂
                _arm_idle_timer(max(0.05, _GPU_IDLE_RELEASE_SEC - idle_for))
        return
    released = []
    with _cache_lock:
        for key, inst in list(_transcribers.items()):
            # 只处理 whisper：计时器由 whisper.transcript() 挂载，funasr 从未挂过。
            # 若在此顺手释放 funasr，会白白付一次分钟级的模型重载（其加载成本远高于
            # whisper 的约 1.7s），收益/风险不成立。要覆盖 funasr，需先给它的
            # transcript() 也挂计时器（且它已有 _ensure_model 自愈）。
            if key is not TranscriberType.FAST_WHISPER:
                continue
            if inst is None or getattr(inst, "device", None) != "cuda":
                continue
            try:
                # 非阻塞抢实例转写锁：抢到说明此刻没有 transcript() 在跑；抢不到说明
                # 正在转写 —— 【跳过】而不是等待（等待会造成危险的交错）。
                lock = getattr(inst, "_lock", None)
                if lock is None:
                    # 拿不到锁 = 无法确认空闲 → 不释放（fail-closed）。
                    # 旧写法会直接 close：任何没有 _lock 的转写器都被无条件卸载，
                    # 等于把「无法确认空闲」当成「确认空闲」。
                    logger.warning("空闲释放跳过 %s：实例无 _lock，无法确认空闲", key.value)
                    continue
                if not lock.acquire(blocking=False):
                    continue
                try:
                    if getattr(inst, "model", None) is None:
                        continue         # 已经释放过（模型为空），无需重复
                    # **只关模型、不摘单例**：实例留在注册表里，下次 transcript() 由
                    # 自愈逻辑按原尺寸重建（whisper.py 的 model is None 分支，与 funasr
                    # 的 _ensure_model 同款）。摘掉槽位会引入两个问题：
                    #   1) 重建后的模型成了「游离模型」，释放扫描再也看不到它 → 永不归还；
                    #   2) 新任务 get_transcriber() 见槽位为 None 会再建一个 → 双份显存。
                    inst.close()
                    released.append(key.value)
                finally:
                    lock.release()
            except Exception as exc:  # noqa: BLE001 —— 释放失败不影响后续重建
                logger.warning("空闲释放 %s 失败: %s", key.value, exc)
    if released:
        # 锁外执行：全量 GC 可能有几十毫秒，不该阻塞并发任务的模型构造
        gc.collect()             # 触发 CT2 对象析构，此时显存才真正归还
        logger.info("GPU 空闲 %.1fs，已释放显存并卸载: %s", idle_for, ", ".join(released))

# 各类型获取方法
def get_groq_transcriber():
    return _get_or_build_transcriber(TranscriberType.GROQ, GroqTranscriber)

def get_whisper_transcriber(model_size="small", device="cuda"):
    # 首次导入 faster_whisper（连带 av / numpy / ctranslate2 三个 60MB 级原生 DLL）
    # 在 MCP 进程里可能长时间停顿（实测 48~455s，会自愈，每进程首触）。这里前后各打
    # 一行日志，让停顿在日志里自解释 —— 否则那几分钟完全空白，只能事后靠两行相减
    # 才能看出，正在观察的人会误判成卡死而取消任务（已有 2 个任务被这样取消）。
    logger.info("首次导入转写引擎（faster_whisper/av/ctranslate2），可能需数分钟，请勿中断或取消")
    _t0 = time.monotonic()
    from app.transcriber.whisper import WhisperTranscriber
    logger.info("转写引擎导入完成，用时 %.1fs", time.monotonic() - _t0)
    return _get_or_build_transcriber(TranscriberType.FAST_WHISPER, WhisperTranscriber, model_size=model_size, device=device)

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
    return _get_or_build_transcriber(TranscriberType.MLX_WHISPER, MLXWhisperTranscriber, model_size=model_size)

def get_funasr_transcriber(device="cpu"):
    from app.transcriber.funasr_transcriber import FunASRTranscriber
    return _get_or_build_transcriber(TranscriberType.FUNASR, FunASRTranscriber, device=device)

# 通用入口
def get_transcriber(transcriber_type="fast-whisper", model_size=None, device="cuda"):
    """
    获取指定类型的转录器实例

    参数:
        transcriber_type: 支持 "fast-whisper", "mlx-whisper", "bcut", "kuaishou", "groq"
        model_size: 模型大小，适用于 whisper 类；显式传入优先于环境变量
            （WHISPER_MODEL_SIZE 仅作兜底，避免 setup/CLI 配置的
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
