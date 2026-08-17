"""下载器共享工具：yt-dlp 调用带退避重试。"""

import time
from typing import Any


def ytdlp_retry(fn, *args, attempts: int = 3, base_delay: float = 1.5, **kwargs) -> Any:
    """执行 yt-dlp 调用，对瞬时网络错误做指数退避重试。

    只重试网络类错误（超时 / 连接失败 / 5xx / 429 / 断流），
    业务错误（404、登录墙、权限、地区限制）立即抛出——重试无意义且浪费时间。
    全部重试耗尽后抛最后一个异常，调用方照常处理。
    """
    import yt_dlp

    # attempts<=0 会让 for 不执行、落到循环后的 raise None（#122 B4）；显式拒绝
    if attempts <= 0:
        raise ValueError(f"attempts 必须为正整数，收到: {attempts}")

    for i in range(attempts):
        try:
            return fn(*args, **kwargs)
        except yt_dlp.utils.DownloadError as e:
            msg = str(e).lower()
            retriable = any(
                k in msg
                for k in (
                    "timed out",
                    "urlerror",
                    "connection",
                    "eof",
                    "http error 5",  # 5xx
                    "http error 429",
                    "network",
                    "socket",
                    "reset by peer",
                    "remote end",
                    "502",
                    "503",
                    "504",
                )
            )
            if not retriable or i == attempts - 1:
                raise
        except (ConnectionError, TimeoutError, OSError):
            if i == attempts - 1:
                raise
        time.sleep(base_delay * (2**i))
    # 循环内最后一次迭代必然 return 或 raise，正常流程到不了这里
    raise RuntimeError("ytdlp_retry 重试耗尽但未抛异常，属不可达分支")
