import ctypes
import os
import sys


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


def _ct2_cublas_loadable() -> bool:
    """cuBLAS 能否真正加载（按 ctranslate2 自己的查找顺序）。

    ctranslate2 对 cuBLAS 是**延迟加载**：模型构造阶段不碰它，直到首次
    transcribe() 才 LoadLibrary，所以「构造成功」不等于「GPU 能用」。
    二进制内的查找逻辑是两段（可从 ctranslate2.dll 字符串确认）：
      1) 裸名 `cublas64_12.dll`（走标准搜索顺序，含 PATH）
      2) 回退 `%CUDA_PATH%\\bin\\cublas64_12.dll`
    此处按同样顺序探测，避免「探测说能用、一跑就炸」。
    """
    name = "cublas64_12.dll" if sys.platform == "win32" else "libcublas.so.12"
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


def is_ct2_cuda_available() -> bool:
    """ctranslate2 自带的 CUDA 后端是否**真能用**（不依赖 torch）。

    faster-whisper 的推理内核全部来自 ctranslate2，整条 GPU 路径不碰 torch；
    而 torch 在本项目里只是 funasr / 说话人分离两个可选重依赖。用「torch 是否
    装了」来判断「whisper 能不能用 GPU」是能力判据错位。

    两级探测，缺一不可——只做第 1 级会让「有 N 卡但缺 CUDA 运行时库」的机器
    从「CPU 能跑」变成「GPU 一跑就 FAILED」：
      1) get_cuda_device_count() > 0：有卡且驱动就绪；**缺 cuBLAS 时它也返回 1**，
         所以单靠它不足以判定；
      2) cuBLAS 真能加载：见 _ct2_cublas_loadable()。

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
        return _ct2_cublas_loadable()
    except Exception:  # noqa: BLE001
        return False
