import functools
import time

from app.utils.logger import get_logger

logger = get_logger(__name__)


def timeit(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        start = time.perf_counter()
        result = func(*args, **kwargs)
        end = time.perf_counter()
        duration = end - start
        # 度量走 logger 而非 print：print 在 MCP 下靠 stderr 重定向兜底，
        # 裸脚本 / pytest 直跑会打进 stdout 污染输出（#127 B7）
        logger.info(f"{func.__name__} executed in {duration:.4f} seconds")
        return result
    return wrapper
