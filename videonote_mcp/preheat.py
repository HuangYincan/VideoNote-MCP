"""Windows MCP 首次转写引擎预热的可测试实现（issue #57 规避方案）。

背景：MCP 进程里**首次**导入 faster_whisper（连带 av / numpy / ctranslate2 三个
60MB 级未签名原生 DLL）会长时间停顿，且只发生在「由 MCP 启动的 server 进程」里。
把整条链的首次原生加载从 worker 线程挪到主线程、挪到进程尚无线程竞争的时刻，可
规避该停顿。风险是若停顿没被消除、只是挪到启动路径，MCP 的 initialize 握手会超时，
故默认开启但可用 `VIDEONOTE_PREHEAT_TRANSCRIBER=0` 关闭。

逻辑抽到独立模块：`server.py` 顶层 import 会执行大量启动副作用（stdout 重定向、
线程池、环境初始化），无法在测试里直接 import 验证这个分支。这里保持零副作用，
用参数注入 platform / enabled，便于回归测试。
"""
import importlib
import logging
import sys
import time

from videonote_mcp.config import env_bool

_logger = logging.getLogger(__name__)

ENV_PREHEAT = "VIDEONOTE_PREHEAT_TRANSCRIBER"
_PREHEAT_MODULE = "faster_whisper"


def preheat_transcriber_engine(
    *,
    platform: str = None,
    enabled: bool = None,
    logger: logging.Logger = None,
) -> bool:
    """在 Windows 上主线程预热转写引擎导入；返回是否真的执行了导入。

    - `platform` 默认 `sys.platform`；非 win32 一律 no-op（其余平台无此停顿问题）。
    - `enabled` 默认读 `VIDEONOTE_PREHEAT_TRANSCRIBER`（未设置时默认开）。
    - 预热失败只记 warning，绝不影响 server 启动。
    """
    log = logger or _logger
    plat = sys.platform if platform is None else platform
    if plat != "win32":
        return False
    if enabled is None:
        enabled = env_bool(ENV_PREHEAT, True)
    if not enabled:
        return False
    try:
        t0 = time.monotonic()
        importlib.import_module(_PREHEAT_MODULE)
        log.info("预热转写引擎导入完成，用时 %.2fs", time.monotonic() - t0)
        return True
    except Exception as exc:  # noqa: BLE001 —— 预热失败不影响功能，任务路径会自己导入
        log.warning("预热转写引擎失败（忽略，不影响功能）: %s", exc)
        return False
