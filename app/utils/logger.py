import logging
import os
import sys
from functools import lru_cache
from pathlib import Path


# 日志目录：落在 VIDEONOTE_DATA_DIR/logs（由 videonote_mcp.config 设置），避免依赖 CWD。
# 必须延迟到首次 get_logger() 时才解析 —— 否则若本模块在 setup_environment() 之前被
# import（如 cli.py → provider_probe → openai_client 的链路），环境变量尚未就绪，
# LOG_DIR 会落到当前工作目录（CWD/logs），而不是数据目录。
@lru_cache(maxsize=1)
def _log_dir() -> Path:
    d = Path(os.environ.get("VIDEONOTE_DATA_DIR", ".")) / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d

# 日志格式
formatter = logging.Formatter(
    fmt="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)

# 控制台输出：必须走 stderr —— MCP stdio 传输使用 stdout 承载 JSON-RPC，绝不能污染
console_handler = logging.StreamHandler(sys.stderr)
console_handler.setFormatter(formatter)

# 文件输出：同样延迟到首次调用 get_logger() 时创建，此时 VIDEONOTE_DATA_DIR 已就绪
_file_handler = None
# root handler 只挂一次（幂等 guard）
_root_file_handler_installed = False


def _ensure_root_file_handler() -> logging.Handler:
    """把文件 handler 挂到 root logger（一次性，#124 B16）。

    第三方库（httpx / yt-dlp / openai 客户端等）不经 get_logger()，此前它们的
    WARNING+ 只走 stderr lastResort，不进 app.log —— 依赖报错在日志文件里失明。
    挂上后第三方 WARNING+ 也落盘。root level 保持 WARNING：第三方 INFO/DEBUG
    洪泛不落盘（app.* 的 logger 自带 handler + propagate=False，不受影响）。
    """
    global _file_handler, _root_file_handler_installed
    root = logging.getLogger()
    if _file_handler is None:
        _file_handler = logging.FileHandler(_log_dir() / "app.log", encoding="utf-8")
        _file_handler.setFormatter(formatter)
    if not _root_file_handler_installed:
        if not any(h is _file_handler for h in root.handlers):
            root.addHandler(_file_handler)
        if root.level == logging.NOTSET:
            root.setLevel(logging.WARNING)
        _root_file_handler_installed = True
    return _file_handler


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        logger.addHandler(console_handler)
        logger.addHandler(_ensure_root_file_handler())
        logger.propagate = False
    return logger
