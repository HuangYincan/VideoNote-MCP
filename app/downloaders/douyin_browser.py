"""抖音扫码：用本机 Chrome 打开官网登录页，让官方 JS 过风控出码。

直连 `sso.douyin.com/get_qrcode/` 会被 ByteDance 风控成 HTML（HTTP 200）。
登录页在真实 Chrome 里自己打 `passport/web/get_qrcode`；我们拦截 JSON 拿
二维码 URL，终端仍出 ASCII 码，轮询期间保持页面存活（站点自己打 check_qrconnect）。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from app.downloaders.douyin_auth import (
    HOME,
    QR_SUCCESS,
    QR_WAIT,
    _map_qr_status,
    format_cookie,
    has_sessionid,
    is_douyin_cookie_host,
    is_douyin_qr_url,
    is_douyin_redirect_url,
    verify_douyin_login,
)
from app.services.cookie_manager import CookieConfigManager
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.logger import get_logger
from app.utils.url_safety import host_matches, sanitize_error_text

logger = get_logger(__name__)

_CREATE_MARK = "/get_qrcode"
_POLL_MARK = "/check_qrconnect"
_LOGIN_PAGE = f"{HOME}/login_page?service={HOME}"
_LAUNCH_TRIES = (
    {"channel": "chrome", "headless": True},
    {"channel": "msedge", "headless": True},
    {"headless": True},
)


class BrowserQrUnavailable(RuntimeError):
    """本机没有可用的 Playwright / Chrome。"""


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
    if err not in (0, None, "0") and not (token and url):
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

    def _on_response(self, resp) -> None:
        url = resp.url or ""
        if not host_matches(url, "douyin.com"):
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        if not isinstance(body, dict):
            return
        if _CREATE_MARK in url:
            parsed = parse_qr_create_payload(body)
            if parsed.get("url") and is_douyin_qr_url(parsed["url"]) and parsed.get("token"):
                self._created = parsed
            return
        if _POLL_MARK in url:
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            st = _map_qr_status(data if isinstance(data, dict) else {})
            self._status = st
            redir = ""
            if isinstance(data, dict):
                redir = str(data.get("redirect_url") or "")
            if redir:
                self._redirect = redir

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
                f"无法启动 Chrome/Edge（{last_err}）。请安装 Google Chrome，或改用 "
                "`videonote login douyin --cookie`"
            )
        self._context = self._browser.new_context(
            locale="zh-CN",
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
            ),
        )
        self._page = self._context.new_page()
        self._page.on("response", self._on_response)

    def _cookies(self) -> dict:
        if self._context is None:
            return {}
        try:
            return cookies_from_playwright(self._context.cookies())
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
            return dict(self._created)
        except Exception:
            self.close()
            raise

    def poll_qr(self, token: str) -> dict:
        """抽一次站点轮询结果；内部 wait 以便 Playwright 收包。"""
        if self._page is not None:
            try:
                self._page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001
                pass
        cookies = self._cookies()
        if has_sessionid(cookies):
            self._status = QR_SUCCESS
            self._logged_cookies = cookies
        return {"status": self._status, "redirect_url": self._redirect}

    def finalize(self, redirect_url: str) -> None:
        """浏览器会话里站点会自己跟随回调；这里只在仍缺 sessionid 时打开官方跳转。"""
        if has_sessionid(self._cookies()):
            return
        url = (redirect_url or self._redirect or "").strip()
        if not url or self._page is None:
            return
        if not is_douyin_redirect_url(url):
            host = urlparse(url).netloc or "未知域名"
            raise ValueError(f"登录回调域名不是抖音官方（{host}）")
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=20000)
            self._page.wait_for_timeout(1500)
        except Exception as exc:  # noqa: BLE001
            logger.info("跟随抖音登录回调失败: %s", sanitize_error_text(exc))

    def persist(self) -> str:
        cookies = self._logged_cookies or self._cookies()
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
