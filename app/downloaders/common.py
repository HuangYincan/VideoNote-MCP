"""下载器共享工具：yt-dlp 调用带退避重试 + 取消钩子。"""

import threading
import time
from typing import Any, Callable, Optional


def ytdlp_cancel_hook(cancel_event: Optional[threading.Event]) -> Callable[[dict], None]:
    """构造 yt-dlp progress_hooks：任务被取消时抛 TaskCancelledError。

    传给各下载器的 ydl_opts['progress_hooks']——yt-dlp 在下载/提取过程中周期性
    调用 hook，取消即 raise 中断进行中的下载，不必等 socket 超时（docs/05 第 16 轮 B1）。
    TaskCancelledError 不是 DownloadError，不会被 ytdlp_retry 当瞬时网络错误重试。
    """

    def _hook(_d: dict) -> None:
        if cancel_event is not None and cancel_event.is_set():
            from app.exceptions.task import TaskCancelledError

            raise TaskCancelledError("任务已取消")

    return _hook


def stream_download(
    url: str,
    output_path: str,
    *,
    headers: Optional[dict] = None,
    timeout: tuple = (10.0, 300.0),
    attempts: int = 3,
    base_delay: float = 1.0,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """流式下载到文件：连接/读分离超时 + 指数退避重试 + 取消检查（docs/05 第 16 轮 B4/B1）。

    抖音/快手直连 CDN 的大视频：旧实现 timeout=30 一次性覆盖连接与读，socket 停滞
    >30s（慢网/CDN 抖动）整体失败且已下字节作废、任务直接 FAILED。改为
    连接 10s + 单次读 300s；瞬时网络错误（连接失败/超时/5xx）指数退避重试。
    取消事件在下载循环内检查（B1）：cancel 即抛 TaskCancelledError，不等读超时。
    """
    import requests

    if attempts <= 0:
        raise ValueError(f"attempts 必须为正整数，收到: {attempts}")
    for i in range(attempts):
        try:
            with requests.get(url, headers=headers, timeout=timeout, stream=True) as resp:
                resp.raise_for_status()
                with open(output_path, "wb") as f:
                    for chunk in resp.iter_content(1024 * 1024):
                        if cancel_event is not None and cancel_event.is_set():
                            from app.exceptions.task import TaskCancelledError

                            raise TaskCancelledError("任务已取消")
                        f.write(chunk)
            return
        except requests.exceptions.RequestException as exc:
            retriable = isinstance(
                exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
            ) or (
                isinstance(exc, requests.exceptions.HTTPError)
                and exc.response is not None
                and 500 <= exc.response.status_code < 600
            )
            # 业务错误（404/403）重试无意义，立即抛出
            if not retriable or i == attempts - 1:
                raise
        time.sleep(base_delay * (2**i))
    # 循环内最后一次迭代必然 return 或 raise，正常流程到不了这里
    raise RuntimeError("stream_download 重试耗尽但未抛异常，属不可达分支")


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
                    # DNS 瞬时故障：yt-dlp 消息形如 `urlopen error [Errno -2] Name or
                    # service not known`——不含任何上述关键词，此前从不重试（#124 B3）
                    "urlopen",
                    "name or service",
                    "temporary failure in name resolution",
                    "dns",
                )
            )
            if not retriable or i == attempts - 1:
                raise
        except (ConnectionError, TimeoutError, OSError) as e:
            # FileNotFoundError 也是 OSError 子类：yt-dlp 未安装 / 输入路径不存在
            # 不是瞬时网络错误，指数退避白等 3 次才报错（#125 B7）
            if isinstance(e, FileNotFoundError):
                raise
            if i == attempts - 1:
                raise
        time.sleep(base_delay * (2**i))
    # 循环内最后一次迭代必然 return 或 raise，正常流程到不了这里
    raise RuntimeError("ytdlp_retry 重试耗尽但未抛异常，属不可达分支")
