"""下载器共享工具：yt-dlp 调用带退避重试 + 取消钩子。"""

import os
import subprocess
import threading
import time
from typing import Any, Callable, Optional

import requests

from app.utils.url_safety import assert_public_http_url, pin_public_host, public_get


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


def run_ffmpeg_cancellable(
    command: list,
    *,
    cancel_event: Optional[threading.Event] = None,
    timeout: float = 600,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    output_path: Optional[str] = None,
    require_nonzero: bool = False,
) -> None:
    """跑 ffmpeg 并轮询取消事件（#133 B5 / 小红书 `_to_mp3` 同款）。

    ``subprocess.run(timeout=600)`` 在 cancel 后仍会占满 worker 最多 10 分钟。
    改为 Popen + 0.2s poll：事件置位即 terminate，超时 kill。
    """
    from app.exceptions.task import TaskCancelledError

    try:
        proc = subprocess.Popen(command, stdout=stdout, stderr=stderr)
    except OSError as exc:
        raise RuntimeError("启动 ffmpeg 失败") from exc
    deadline = time.monotonic() + timeout
    while proc.poll() is None:
        if cancel_event is not None and cancel_event.is_set():
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            raise TaskCancelledError("任务已取消")
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            raise RuntimeError("ffmpeg 转换超时")
        time.sleep(0.2)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 转换失败（退出码 {proc.returncode}）")
    if output_path is not None:
        if not os.path.exists(output_path):
            raise RuntimeError(f"ffmpeg 未写出文件: {output_path}")
        if require_nonzero and os.path.getsize(output_path) <= 0:
            raise RuntimeError(f"ffmpeg 未写出有效文件: {output_path}")


def public_get_retry(
    url: str,
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    deadline: float = 30.0,
    **kwargs,
) -> requests.Response:
    """GET a public URL with bounded retries for transient network responses.

    Each attempt goes through ``public_get`` so the initial URL and every redirect
    hop retain the SSRF guard.  Only connection/timeout failures, HTTP 429, and
    HTTP 5xx responses are retried; the final response is returned unchanged so
    existing callers keep their current JSON/business-error handling.
    """
    if attempts <= 0:
        raise ValueError(f"attempts 必须为正整数，收到: {attempts}")
    if deadline <= 0:
        raise ValueError(f"deadline 必须为正数，收到: {deadline}")
    deadline_at = time.monotonic() + deadline
    last_error = None

    for i in range(attempts):
        try:
            response = public_get(url, **kwargs)
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            last_error = exc
            if i == attempts - 1:
                raise
        else:
            status = getattr(response, "status_code", 200)
            if status not in (429,) and not 500 <= status < 600:
                return response
            if i == attempts - 1 or time.monotonic() >= deadline_at:
                return response
            close = getattr(response, "close", None)
            if callable(close):
                close()

        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(base_delay * (2**i), remaining))

    if last_error is not None:
        raise last_error
    raise requests.exceptions.Timeout("GET 重试达到总 deadline")


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

    SSRF（#140，复扫 A1 修复）：url 来自平台 API 返回的资源地址（抖音 url_list /
    快手 photoUrl），入口 URL 校验覆盖不到——**禁用自动跟随重定向 + 手动逐跳跟进**：
    每一跳（含 302 Location 目标）都先过 `assert_public_http_url` 再发出请求。
    裸 `requests.get` 默认跟随会把公网入口 302 到内网/云元数据（169.254.169.254）
    的第二跳实际发出（复扫已复现）。
    """
    if attempts <= 0:
        raise ValueError(f"attempts 必须为正整数，收到: {attempts}")
    assert_public_http_url(url)
    for i in range(attempts):
        try:
            resp = _follow_redirects_public(url, headers=headers, timeout=timeout)
            with resp:
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


_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 10


def _follow_redirects_public(url: str, *, headers, timeout) -> "requests.Response":
    """逐跳公网校验地跟随重定向，返回最终响应（#140 复扫 A1）。

    allow_redirects=False + 手动循环：requests 内部跟随无法在每跳发出前校验；
    Location 目标先 assert_public_http_url 再请求，内网跳点（含相对跳转）在
    发出前被拦截（抛 ValueError，非网络错误不重试）。
    """
    from urllib.parse import urljoin

    redirects = 0
    while True:
        assert_public_http_url(url)
        # 钉死本跳解析 IP，避免 requests.get 二次解析时 DNS rebinding（#146 A1）
        with pin_public_host(url):
            resp = requests.get(
                url, headers=headers, timeout=timeout, stream=True, allow_redirects=False
            )
        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("Location")
            redirects += 1
            resp.close()
            if not location or redirects > _MAX_REDIRECTS:
                raise requests.exceptions.TooManyRedirects(
                    f"重定向超过 {_MAX_REDIRECTS} 次或缺少 Location"
                )
            url = urljoin(str(url), location)
            continue
        return resp


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
