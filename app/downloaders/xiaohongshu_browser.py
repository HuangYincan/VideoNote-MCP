"""小红书扫码：用本机 Chrome 打开官网，让官方 JS 签名出码。

edith `qrcode/create` 已拒绝旧版 x-s（HTTP 406）。登录页在真实 Chrome 里
会自己带现行签名；我们拦截 create 的 JSON 拿二维码 URL，终端仍出 ASCII 码，
轮询期间保持页面存活（站点自己打 userinfo）。
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qs, urlparse

from app.downloaders.xiaohongshu_auth import (
    HOME,
    QR_SUCCESS,
    QR_WAIT,
    format_cookie,
    is_xiaohongshu_qr_url,
)
from app.services.cookie_manager import CookieConfigManager
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.logger import get_logger
from app.utils.url_safety import host_matches, hostname_matches, sanitize_error_text

logger = get_logger(__name__)

_CREATE_MARK = "/login/qrcode/create"
_USERINFO_MARK = "/api/qrcode/userinfo"
_STATUS_MARK = "/login/qrcode/status"
_EXPLORE = f"{HOME}/explore"
_LAUNCH_TRIES = (
    {"channel": "chrome", "headless": True},
    {"channel": "msedge", "headless": True},
    {"headless": True},
)


class BrowserQrUnavailable(RuntimeError):
    """本机没有可用的 Playwright / Chrome。"""


def parse_qr_create_payload(body: dict) -> dict:
    """从 qrcode/create JSON 抽出 {url, qr_id, code}。字段名兼容驼峰/下划线。"""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        data = {}
    url = str(data.get("url") or "")
    qr_id = str(data.get("qr_id") or data.get("qrId") or "")
    code = str(data.get("code") or data.get("xhs_code") or data.get("xhsCode") or "")
    if url and (not qr_id or not code):
        qs = parse_qs(urlparse(url).query)
        qr_id = qr_id or (qs.get("qrId") or qs.get("qr_id") or [""])[0]
        code = code or (qs.get("xhs_code") or qs.get("code") or [""])[0]
    return {"url": url, "qr_id": qr_id, "code": code}


def poll_status_from_payload(body: dict) -> Optional[int]:
    """userinfo / qrcode/status → 0 等待 / 1 已扫 / 2 成功 / 3 过期。"""
    if not isinstance(body, dict):
        return None
    data = body.get("data") if isinstance(body.get("data"), dict) else body
    for key in ("codeStatus", "code_status", "status"):
        if data.get(key) is None:
            continue
        try:
            return int(data.get(key))
        except (TypeError, ValueError):
            continue
    return None


def cookies_from_playwright(raw_cookies: list) -> dict:
    """只留 xiaohongshu.com 精确后缀域；同名后者覆盖。"""
    out = {}
    for item in raw_cookies or []:
        if not isinstance(item, dict):
            continue
        domain = (item.get("domain") or "").lower()
        if not hostname_matches(domain, "xiaohongshu.com"):
            continue
        name = item.get("name")
        if name:
            out[name] = item.get("value") or ""
    return out


class XiaohongshuBrowserQr:
    """与 XiaohongshuAuth 同款：create_qr / poll_qr / persist / close。"""

    pumps_events = True

    def __init__(self, cookie_mgr: Optional[CookieConfigManager] = None):
        self._cookie_mgr = cookie_mgr or CookieConfigManager()
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._created: dict = {}
        self._status = QR_WAIT
        self._guest_session = ""
        self._logged_cookies: dict = {}

    def _on_response(self, resp) -> None:
        url = resp.url or ""
        if not host_matches(url, "xiaohongshu.com"):
            return
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return
        if not isinstance(body, dict):
            return
        if _CREATE_MARK in url:
            parsed = parse_qr_create_payload(body)
            if parsed.get("url") and is_xiaohongshu_qr_url(parsed["url"]):
                self._created = parsed
            return
        if _USERINFO_MARK in url or _STATUS_MARK in url:
            st = poll_status_from_payload(body)
            if st is not None:
                self._status = st

    def _launch(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserQrUnavailable(
                "缺少 playwright。请 `uv sync` 或 `uvx --with playwright videonote login xiaohongshu`"
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
                logger.info("小红书扫码浏览器: %s", kwargs)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.info("启动浏览器失败 %s: %s", kwargs, sanitize_error_text(exc))
        if self._browser is None:
            self.close()
            raise BrowserQrUnavailable(
                f"无法启动 Chrome/Edge（{last_err}）。请安装 Google Chrome，或改用 "
                "`videonote login xiaohongshu --cookie`"
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
                self._page.goto(_EXPLORE, wait_until="domcontentloaded", timeout=45000)
            except Exception as exc:
                raise RuntimeError(f"打开小红书失败: {sanitize_error_text(exc)}") from exc
            self._page.wait_for_timeout(1500)
            self._guest_session = self._cookies().get("web_session") or ""
            clicked = False
            for sel in ("text=登录", "button:has-text('登录')"):
                loc = self._page.locator(sel).first
                try:
                    if loc.count() and loc.is_visible():
                        loc.click(timeout=5000)
                        clicked = True
                        break
                except Exception:  # noqa: BLE001
                    continue
            if not clicked:
                logger.info("未点到登录按钮，等待页面自带二维码")
            for _ in range(40):
                if self._created.get("url"):
                    break
                self._page.wait_for_timeout(500)
            qr_url = self._created.get("url") or ""
            if not qr_url:
                raise RuntimeError(
                    "未拿到小红书二维码（登录框没出来？）。改用 "
                    "`videonote login xiaohongshu --cookie`"
                )
            if not is_xiaohongshu_qr_url(qr_url):
                raise RuntimeError("小红书二维码地址不是官方域名，已拒绝展示")
            return dict(self._created)
        except Exception:
            self.close()
            raise

    def poll_qr(self, qr_id: str, code: str) -> dict:
        """抽一次站点轮询结果；内部 wait 以便 Playwright 收包。"""
        if self._page is not None:
            try:
                self._page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001
                pass
        cookies = self._cookies()
        if self._status == QR_SUCCESS:
            if cookies:
                self._logged_cookies = cookies
        return {"code_status": self._status, "login_info": {}}

    def persist(self) -> str:
        cookies = self._logged_cookies or self._cookies()
        if not cookies.get("web_session"):
            return "登录成功但未取到 web_session"
        self._cookie_mgr.set("xiaohongshu", format_cookie(cookies))
        return ""

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
