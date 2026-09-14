import ctypes
import logging
import os
import sys

# 用标准库 logger：本模块会被多个低层模块 import，绝不能在 import 期创建日志目录
# （get_logger 会立刻 _log_dir() 落盘，若发生在 setup_environment 之前就会误落到
# CWD/logs —— 见 app/utils/logger.py 与 cli.py 的 import 顺序约定）。
logger = logging.getLogger(__name__)


def env_int(name: str, default: int, lo: int = None, hi: int = None) -> int:
    """环境变量取整数：未设置 / 非法 → default；越界 → 夹取。

    模块级调用必须容错：非法值（空串、手滑值）若用裸 int() 会在 **import 期** 抛
    ValueError，让整个模块导入失败 —— 链路是 server.py → app.services.pipeline →
    transcriber_provider，于是 **MCP server 直接起不来**。`app/` 是 vendored 层，
    不能反向 import videonote_mcp.config 的 env_int，故在此自持一份供 app 内共用
    （transcriber_provider / note_cache / universal_gpt 同口径）。
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


def is_cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except ImportError:
        return False
def is_torch_installed() -> bool:
    try:
        import torch  # noqa: F401  —— 可用性探测，不需使用
        return True
    except ImportError:
        return False


# ctranslate2 的 CUDA 后端同时链接 **cuBLAS 与 cuBLASLt**：只探 cuBLAS 会在缺
# cuBLASLt 的机器上得到「探测说能用、首次 transcribe() 才 FAILED」的假阳性（正好
# 是本次探测要避免的失败模式）。故一对库都要求可加载。CUDA 12 是当前 ctranslate2
# wheel 的目标版本，CUDA 11 用 *_11 / .so.11 后缀，两代都试（命中任一对即可）。
_CT2_CUDA_LIBS_WIN = (
    ("cublas64_12.dll", "cublasLt64_12.dll"),
    ("cublas64_11.dll", "cublasLt64_11.dll"),
)
_CT2_CUDA_LIBS_POSIX = (
    ("libcublas.so.12", "libcublasLt.so.12"),
    ("libcublas.so.11", "libcublasLt.so.11"),
)


def _load_shared_library(name: str) -> bool:
    """按 ctranslate2 自己的查找顺序尝试加载单个动态库。

    ctranslate2 是**延迟加载**这些库的：模型构造阶段不碰它，直到首次 transcribe()
    才 LoadLibrary，所以「构造成功」不等于「GPU 能用」。二进制内的 Windows 查找逻辑
    是两段（可从 ctranslate2.dll 字符串确认）：
      1) 裸名（走标准搜索顺序，含 PATH）
      2) 回退 `%CUDA_PATH%\\bin\\<name>`
    此处按同样顺序探测，避免「探测说能用、一跑就炸」。
    """
    if sys.platform != "win32":
        try:
            ctypes.CDLL(name)
            return True
        except OSError:
            return False
    # winmode=0：恢复 legacy 搜索顺序（含 PATH），与 ctranslate2 的 LoadLibraryW 一致。
    # 默认 winmode 在 Python 3.8+ 改用 LOAD_LIBRARY_SEARCH_DEFAULT_DIRS，**不搜 PATH**，
    # 用它探测会得到假阴性（实测确认）。
    try:
        ctypes.WinDLL(name, winmode=0)
        return True
    except (OSError, AttributeError):
        pass
    cuda_path = os.environ.get("CUDA_PATH")
    if cuda_path:
        full = os.path.join(cuda_path, "bin", name)
        if os.path.isfile(full):
            try:
                ctypes.WinDLL(full)
                return True
            except OSError:
                pass
    return False


def _ct2_cuda_libs_loadable() -> bool:
    """cuBLAS + cuBLASLt 这一对 CUDA 运行时库能否真正加载（任一代完整即可）。"""
    pairs = _CT2_CUDA_LIBS_WIN if sys.platform == "win32" else _CT2_CUDA_LIBS_POSIX
    for pair in pairs:
        if all(_load_shared_library(name) for name in pair):
            return True
    return False


def is_ct2_cuda_available() -> bool:
    """ctranslate2 自带的 CUDA 后端是否**真能用**（不依赖 torch）。

    faster-whisper 的推理内核全部来自 ctranslate2，整条 GPU 路径不碰 torch；
    而 torch 在本项目里只是 funasr / 说话人分离两个可选重依赖。用「torch 是否
    装了」来判断「whisper 能不能用 GPU」是能力判据错位。

    两级探测，缺一不可——只做第 1 级会让「有 N 卡但缺 CUDA 运行时库」的机器
    从「CPU 能跑」变成「GPU 一跑就 FAILED」：
      1) get_cuda_device_count() > 0：有卡且驱动就绪；**缺 CUDA 运行时库时它也返回
         1**，所以单靠它不足以判定；
      2) cuBLAS + cuBLASLt 真能加载：见 _ct2_cuda_libs_loadable()。

    任何异常一律返回 False（fail-closed），保证最坏情况是回落 CPU。
    """
    try:
        import ctranslate2
    except ImportError:
        return False
    try:
        if ctranslate2.get_cuda_device_count() <= 0:
            return False
    except Exception:  # noqa: BLE001 —— 驱动异常/无卡，一律当不可用
        return False
    try:
        return _ct2_cuda_libs_loadable()
    except Exception:  # noqa: BLE001
        return False
