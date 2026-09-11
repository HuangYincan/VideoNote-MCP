"""抖音扫码：用本机 Chrome 打开官网登录页，让官方 JS 过风控出码。

直连 `sso.douyin.com/get_qrcode/` 会被 ByteDance 风控成 HTML（HTTP 200）。
登录页在真实 Chrome 里自己打 `passport/web/get_qrcode`；我们拦截 JSON 拿
二维码 URL，终端仍出 ASCII 码。官网 `is_frontier` 时「已确认」可能只走
WebSocket，HTTP 会一直停在 scanned。后备轮询必须复用官网实际的请求方法和
表单，并在网页内发送（由官网 JS 处理签名），不能把出码 URL 改成 GET。
手机确认后还可能返回 2046 要求电脑端身份验证：必须显示浏览器，暂停
后备轮询，不打断官方验证流程；等待浏览器真正写入 sessionid 后才落盘。
"""
from __future__ import annotations

import json
import time
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

from app.downloaders.douyin_auth import (
    HOME,
    QR_EXPIRED,
    QR_SCANNED,
    QR_SUCCESS,
    QR_VERIFY,
    QR_WAIT,
    _map_qr_status,
    format_cookie,
    has_sessionid,
    is_douyin_cookie_host,
    is_douyin_qr_url,
    is_douyin_redirect_url,
    qr_requires_verification,
    verify_douyin_login,
)
from app.services.cookie_manager import CookieConfigManager
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.logger import get_logger
from app.utils.url_safety import host_matches, sanitize_error_text

logger = get_logger(__name__)

_CREATE_MARK = "/get_qrcode"
_POLL_MARK = "/check_qrconnect"
_COOKIE_WAIT_SECONDS = 15
_CALLBACK_GRACE_SECONDS = 3
_LOGIN_PAGE = f"{HOME}/login_page?service={HOME}"
_LAUNCH_TRIES = (
    {"channel": "chrome", "headless": False},
    {"channel": "msedge", "headless": False},
    {"headless": False},
)


class BrowserQrClosed(RuntimeError):
    """用户关闭了本次登录窗口；不是可重试的网络错误。"""


class BrowserQrUnavailable(RuntimeError):
    """本机没有可用的 Playwright / Chrome。"""


def build_poll_request(request, token: str) -> dict:
    """复制当前二维码的真实轮询请求；不借用出码参数或过期签名。"""
    url = getattr(request, "url", "")
    method = getattr(request, "method", "")
    if not token or not isinstance(url, str) or not host_matches(url, "douyin.com"):
        return {}
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username or parts.password:
        return {}
    if not parts.path.rstrip("/").endswith(_POLL_MARK) or method not in ("GET", "POST"):
        return {}
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    # URL 签名绑定参数/请求体。交回页面里的官方 JS 重签，不能重放旧签名。
    for key in ("a_bogus", "X-Bogus", "_signature"):
        query.pop(key, None)
    headers = getattr(request, "headers", {}) or {}
    headers = {k.lower(): v for k, v in headers.items()}
    options = {"method": method, "headers": {}}
    if method == "POST":
        content_type = headers.get("content-type", "")
        body = getattr(request, "post_data", "") or ""
        if not isinstance(body, str) or "application/x-www-form-urlencoded" not in content_type:
            return {}
        form = dict(parse_qsl(body, keep_blank_values=True))
        if form.get("token") != token:
            return {}
        form["is_frontier"] = "false"
        options["body"] = urlencode(form)
        options["headers"]["Content-Type"] = content_type
    else:
        if query.get("token") != token:
            return {}
        query["is_frontier"] = "false"
    for key in ("x-tt-passport-csrf-token", "web-sdk-version"):
        if key in headers:
            options["headers"][key] = headers[key]
    options["url"] = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))
    return options


# APIRequestContext 共享 Cookie，但不在浏览器中执行，也不会经过网页 JS 签名。
# 只重放观察到的官网请求；AbortController 给后备轮询设置明确的网络时限。
_POLL_IN_PAGE = """async ({url, method, headers, body}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 8000);
    try {
        const response = await fetch(url, {
            method, headers, body, credentials: 'include',
            signal: controller.signal, redirect: 'error'
        });
        if (!response.ok) return null;
        return await response.json();
    } finally {
        clearTimeout(timer);
    }
}"""


def poll_payload_status(body: dict) -> tuple[str, str]:
    """从 check_qrconnect / WS JSON 抽出 (QR_*, redirect_url)。"""
    if not isinstance(body, dict):
        return QR_WAIT, ""
    data = body.get("data") if isinstance(body.get("data"), dict) else None
    if not isinstance(data, dict):
        data = body
    if qr_requires_verification(body):
        return QR_VERIFY, ""
    if any(item.get("error_code") not in (None, "", 0, "0") for item in (body, data)):
        return QR_WAIT, ""
    st = _map_qr_status(data)
    redir = str(data.get("redirect_url") or body.get("redirect_url") or "")
    return st, redir


def next_qr_status(current: str, incoming: str) -> str:
    """扫码状态只前进，避免 frontier 下后续 HTTP scanned/new 把 confirmed 盖掉。"""
    # 已扫码后的身份验证不受旧二维码过期/迟到 confirmed 影响；仅 Cookie 能完成它。
    # confirmed 也可能只完成了手机授权，随后才返回 2046。
    if current == QR_VERIFY or incoming == QR_VERIFY:
        return QR_VERIFY
    if current in (QR_SUCCESS, QR_EXPIRED):
        return current
    if incoming == QR_EXPIRED:
        return QR_EXPIRED
    if incoming == QR_SUCCESS:
        return QR_SUCCESS
    if current == QR_SCANNED and incoming == QR_WAIT:
        return QR_SCANNED
    return incoming


def parse_qr_create_payload(body: dict) -> dict:
    """从 get_qrcode JSON 抽出 {token, url}。"""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        data = {}
    err = data.get("error_code")
    if err in (None, "", 0, "0") and isinstance(body, dict):
        err = body.get("error_code")
    token = str(data.get("token") or "")
    url = str(data.get("qrcode_index_url") or data.get("frontend_show_qrcode") or "")
    if err not in (0, None, "", "0"):
        return {"token": "", "url": ""}
    return {"token": token, "url": url}


def cookies_from_playwright(raw_cookies: list) -> dict:
    """只留 douyin.com 精确后缀域；同名后者覆盖。"""
    out = {}
    for item in raw_cookies or []:
        if not isinstance(item, dict):
            continue
        domain = (item.get("domain") or "").lower()
        if not is_douyin_cookie_host(domain):
            continue
        name = item.get("name")
        if name:
            out[name] = item.get("value") or ""
    return out


class DouyinBrowserQr:
    """与 DouyinAuth 同款：create_qr / poll_qr / finalize / persist / close。"""

    pumps_events = True
    interactive_verification = True

    def __init__(self, cookie_mgr: Optional[CookieConfigManager] = None):
        self._cookie_mgr = cookie_mgr or CookieConfigManager()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._created: dict = {}
        self._status = QR_WAIT
        self._redirect = ""
        self._logged_cookies: dict = {}
        self._poll_request: dict = {}
        self._displayed_token = ""
        self._expires_at: Optional[float] = None

    @property
    def verification_required(self) -> bool:
        return self._status == QR_VERIFY

    def _apply_poll_body(self, body: dict) -> None:
        if has_sessionid(self._logged_cookies):
            return
        st, redir = poll_payload_status(body)
        prev = self._status
        self._status = next_qr_status(self._status, st)
        if redir and st == QR_SUCCESS and self._status == QR_SUCCESS:
            self._redirect = redir
        if self._status != prev:
            logger.info("抖音扫码状态: %s", self._status)

    def _on_ws_payload(self, payload) -> None:
        text = payload
        if isinstance(payload, dict):
            text = payload.get("payload") or payload.get("data") or ""
        if isinstance(text, bytes):
            try:
                text = text.decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return
        if not isinstance(text, str) or not text or text[0] not in "{[":
            return
        try:
            body = json.loads(text)
        except Exception:  # noqa: BLE001
            return
        if isinstance(body, dict):
            # 官网也有其他业务 WS；只消费能绑定到当前二维码的消息。
            data = body.get("data") if isinstance(body.get("data"), dict) else body
            token = self._created.get("token")
            if token and data.get("token") != token:
                return
            self._apply_poll_body(body)

    def _on_websocket(self, ws) -> None:
        url = getattr(ws, "url", "") or ""
        if not host_matches(url, "douyin.com", "snssdk.com"):
            return
        try:
            ws.on("framereceived", self._on_ws_payload)
        except Exception:  # noqa: BLE001
            pass

    def _on_request(self, request) -> None:
        options = build_poll_request(request, self._created.get("token") or "")
        if options:
            self._poll_request = options

    def _on_response(self, resp) -> None:
        url = resp.url or ""
        if not host_matches(url, "douyin.com"):
            return
        path = urlsplit(url).path.rstrip("/")
        is_create = path.endswith(_CREATE_MARK)
        is_poll = path.endswith(_POLL_MARK)
        if not (is_create or is_poll):
            return
        http_status = getattr(resp, "status", None)
        if isinstance(http_status, int) and not 200 <= http_status < 300:
            return
        if is_poll:
            options = build_poll_request(resp.request, self._created.get("token") or "")
            if self._created.get("token") and not options:
                return  # 页面自动刷新出的另一张二维码，不是终端展示的那一张。
            if options:
                self._poll_request = options
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        if not isinstance(body, dict):
            return
        if is_create:
            parsed = parse_qr_create_payload(body)
            if parsed.get("url") and is_douyin_qr_url(parsed["url"]) and parsed.get("token"):
                if self._displayed_token:
                    if parsed["token"] != self._displayed_token:
                        self._status = next_qr_status(self._status, QR_EXPIRED)
                    return
                self._created = parsed
                self._poll_request = {}
                self._status, self._redirect = QR_WAIT, ""
                # 官网 expire_time 是 Unix 秒数。换算为单调时钟，避免系统校时影响等待。
                expiry = body.get("data", {}).get("expire_time")
                try:
                    remaining = max(0.0, min(180.0, float(expiry) - time.time()))
                except (TypeError, ValueError):
                    remaining = 180.0
                self._expires_at = time.monotonic() + remaining
            return
        self._apply_poll_body(body)

    def _active_poll(self, token: str) -> None:
        if self.verification_required:
            return  # 重放请求不会完成短信/刷脸，反而可能重置官方验证流程。
        if self._page is None or token != self._created.get("token") or not self._poll_request:
            return
        try:
            body = self._page.evaluate(_POLL_IN_PAGE, self._poll_request)
        except Exception as exc:  # noqa: BLE001
            # 不输出异常里的带 token URL；官网自己的轮询/回调仍在运行。
            logger.info("抖音后备轮询暂不可用（%s），继续等待官网回调", type(exc).__name__)
            return
        if isinstance(body, dict):
            self._apply_poll_body(body)

    def _launch(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserQrUnavailable(
                "缺少 playwright。请 `uv sync` 或 `uvx --with playwright videonote login douyin`"
            ) from exc
        proxy = ProxyConfigManager().get_proxy_url()
        launch_proxy = {"server": proxy} if proxy else None
        self._pw = sync_playwright().start()
        last_err: Optional[Exception] = None
        for kwargs in _LAUNCH_TRIES:
            try:
                opts = dict(kwargs)
                opts.setdefault("timeout", 15000)
                if launch_proxy:
                    opts["proxy"] = launch_proxy
                self._browser = self._pw.chromium.launch(**opts)
                logger.info("抖音扫码浏览器: %s", kwargs)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.info("启动浏览器失败 %s: %s", kwargs, sanitize_error_text(exc))
        if self._browser is None:
            self.close()
            raise BrowserQrUnavailable(
                f"无法启动 Chrome/Edge（{sanitize_error_text(last_err)}）。请安装 Google Chrome，或改用 "
                "`videonote login douyin --cookie`"
            )
        self._context = self._browser.new_context(
            locale="zh-CN",
            viewport={"width": 1280, "height": 800},
        )
        self._page = self._context.new_page()
        self._page.on("request", self._on_request)
        self._page.on("response", self._on_response)
        self._page.on("websocket", self._on_websocket)

    def _cookies(self) -> dict:
        if self._context is None:
            return {}
        try:
            # 只收主站实际可用的 Cookie，避免 SSO 子域或其他 path 的同名项覆盖。
            return cookies_from_playwright(self._context.cookies(HOME))
        except Exception:  # noqa: BLE001
            return {}

    def create_qr(self) -> dict:
        try:
            self._launch()
            assert self._page is not None
            try:
                self._page.goto(_LOGIN_PAGE, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                raise RuntimeError(f"打开抖音登录页失败: {sanitize_error_text(exc)}") from exc
            for _ in range(40):
                if self._created.get("url") and self._created.get("token"):
                    break
                self._page.wait_for_timeout(500)
            if not (self._created.get("url") and self._created.get("token")):
                try:
                    self._page.goto(f"{HOME}/", wait_until="domcontentloaded", timeout=45000)
                    for sel in ("text=登录", "button:has-text('登录')"):
                        loc = self._page.locator(sel).first
                        try:
                            if loc.count():
                                loc.click(timeout=5000, force=True)
                                break
                        except Exception:  # noqa: BLE001
                            continue
                    for _ in range(20):
                        if self._created.get("url") and self._created.get("token"):
                            break
                        self._page.wait_for_timeout(500)
                except Exception:  # noqa: BLE001
                    pass
            qr_url = self._created.get("url") or ""
            token = self._created.get("token") or ""
            if not qr_url or not token:
                raise RuntimeError(
                    "未拿到抖音二维码（登录框没出来？）。改用 "
                    "`videonote login douyin --cookie`"
                )
            if not is_douyin_qr_url(qr_url):
                raise RuntimeError("抖音二维码地址不是官方域名，已拒绝展示")
            self._displayed_token = token
            created = dict(self._created)
            if self._expires_at is not None:
                created["expires_in"] = max(0.0, self._expires_at - time.monotonic())
            return created
        except BaseException:
            self.close()
            raise

    def _capture_session(self) -> bool:
        cookies = self._cookies()
        if not has_sessionid(cookies):
            return False
        self._status = QR_SUCCESS
        self._logged_cookies = cookies
        return True

    def poll_qr(self, token: str) -> dict:
        """先驱动官网事件与回调，再用实际请求做浏览器内短轮询。"""
        # 登录成功后官网可能关掉登录页；已有 Cookie 时不再调用已关闭的页面。
        if not self._capture_session():
            if self._page is not None:
                if self._page.is_closed() is True:
                    raise BrowserQrClosed("登录窗口已关闭，本次扫码已取消")
                self._page.wait_for_timeout(2000)
            if not self._capture_session() and self._status not in (QR_SUCCESS, QR_EXPIRED, QR_VERIFY):
                self._active_poll(token)
                self._capture_session()
        return {"status": self._status, "redirect_url": self._redirect}

    def _wait_for_session(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while not self._capture_session():
            if self.verification_required:
                return False  # 交还 CLI 显示操作提示，并使用独立的人工验证等待时间。
            remaining = deadline - time.monotonic()
            if remaining <= 0 or self._page is None:
                return False
            self._page.wait_for_timeout(min(250, remaining * 1000))
        return True

    def finalize(self, redirect_url: str) -> None:
        """让官网先走完回调；有确认而没 Cookie 时仍给异步落地留出时间。"""
        if self._capture_session():
            return
        url = (redirect_url or self._redirect or "").strip()
        if url and not is_douyin_redirect_url(url):
            host = urlparse(url).hostname or "未知域名"
            raise ValueError(f"登录回调域名不是抖音官方（{host}）")
        if self._page is None:
            return
        # 立即 goto 会中断官方 JS 的票据交换请求；先给它完成的机会。
        deadline = time.monotonic() + _COOKIE_WAIT_SECONDS
        if self._wait_for_session(_CALLBACK_GRACE_SECONDS) or self.verification_required:
            return
        if url:
            try:
                self._page.goto(
                    url, wait_until="domcontentloaded",
                    timeout=max(1, (deadline - time.monotonic()) * 1000),
                )
            except Exception as exc:  # noqa: BLE001
                logger.info("抖音登录回调尚未完成（%s）", type(exc).__name__)
        self._wait_for_session(max(0, deadline - time.monotonic()))

    def persist(self) -> str:
        cookies = self._cookies()
        if not has_sessionid(cookies):
            cookies = self._logged_cookies
        if not has_sessionid(cookies):
            return "登录成功但未取到 sessionid"
        self._cookie_mgr.set("douyin", format_cookie(cookies))
        return verify_douyin_login(cookie_mgr=self._cookie_mgr)

    def close(self) -> None:
        for obj in (self._page, self._context, self._browser):
            if obj is None:
                continue
            try:
                obj.close()
            except Exception:  # noqa: BLE001
                pass
        self._page = self._context = self._browser = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = None
