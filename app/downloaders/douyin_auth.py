"""抖音 Web 扫码登录。

走官网 SSO：`sso.douyin.com/get_qrcode/` + `/check_qrconnect/`，确认后跟随
官方 `redirect_url` 收集 Cookie，写入 CookieConfigManager 的 `douyin` 槽。

凭证走 CLI：`videonote login douyin` / `videonote cookie set douyin`。
MCP 工具不收 cookie（安全红线）。
"""
from __future__ import annotations

import random
import re
import string
from typing import Optional
from urllib.parse import urlparse

from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger
from app.utils.url_safety import PublicOnlySession, host_matches, sanitize_error_text

logger = get_logger(__name__)

SSO = "https://sso.douyin.com"
HOME = "https://www.douyin.com"
ACCOUNT_INFO = "https://www.douyin.com/passport/web/account/info/"
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_WEB_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": HOME,
    "Referer": f"{SSO}/",
}

# 扫码状态（SSO data.status）：1 等待 / 2 已扫待确认 / 3 成功 / 5 过期
QR_WAIT = "1"
QR_SCANNED = "2"
QR_SUCCESS = "3"
QR_EXPIRED = "5"

_DOUYIN_HOSTS = ("douyin.com", "snssdk.com")


def is_douyin_qr_url(url: str) -> bool:
    """扫码 payload 只允许抖音 / aweme.snssdk 官方域。"""
    return host_matches(url, *_DOUYIN_HOSTS)


def is_douyin_redirect_url(url: str) -> bool:
    """登录回调只认官方后缀，拒绝跟随到任意 Location。"""
    return host_matches(url, *_DOUYIN_HOSTS)


def parse_cookie_string(raw: Optional[str]) -> dict:
    """`a=1; b=2` → dict。空/None → {}。"""
    out = {}
    text = (raw or "").strip()
    if not text:
        return out
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def format_cookie(cookies: dict) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v)


def has_sessionid(cookies: dict) -> bool:
    return bool(cookies.get("sessionid") or cookies.get("sessionid_ss"))


def gen_verify_fp() -> str:
    """生成 s_v_web_id / verifyFp（SSO 多个接口会带）。"""
    chars = string.ascii_letters + string.digits
    chunks = ["".join(random.choices(chars, k=n)) for n in (8, 8, 4, 4, 4, 12)]
    return "verify_" + "_".join(chunks)


def _cookie_dict_from_jar(jar) -> dict:
    try:
        return {c.name: c.value for c in jar}
    except Exception:  # noqa: BLE001
        try:
            return dict(jar)
        except Exception:  # noqa: BLE001
            return {}


def _map_qr_status(data: dict) -> str:
    """把 SSO data.status / redirect_url 归一成 QR_* 常量。"""
    redirect = (data.get("redirect_url") or "") if isinstance(data, dict) else ""
    raw = ""
    if isinstance(data, dict):
        raw = str(data.get("status", "") or "")
    if redirect or raw in ("3", "4"):
        return QR_SUCCESS
    if raw == "2":
        return QR_SCANNED
    if raw == "5":
        return QR_EXPIRED
    return QR_WAIT


class DouyinAuth:
    """扫码登录会话。session 可注入（测试 mock）。"""

    def __init__(self, session=None, cookie_mgr: Optional[CookieConfigManager] = None):
        self._cookie_mgr = cookie_mgr or CookieConfigManager()
        self._session = session
        self._owns_session = session is None
        self.verify_fp = gen_verify_fp()

    def _session_obj(self):
        if self._session is None:
            self._session = PublicOnlySession()
            self._owns_session = True
        self._session.headers.update(_WEB_HEADERS)
        try:
            self._session.cookies.set("s_v_web_id", self.verify_fp, domain=".douyin.com")
        except Exception:  # noqa: BLE001
            self._session.cookies.set("s_v_web_id", self.verify_fp)
        return self._session

    def close(self) -> None:
        if self._owns_session and self._session is not None:
            close = getattr(self._session, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass
        self._session = None

    def _params(self) -> dict:
        return {
            "service": HOME,
            "need_logo": "false",
            "need_short_url": "true",
            "aid": "6383",
            "account_sdk_source": "sso",
            "sdk_version": "2.2.7",
            "language": "zh",
            "verifyFp": self.verify_fp,
            "fp": self.verify_fp,
        }

    def create_qr(self) -> dict:
        """申请二维码。返回 {token, url}；url 必须是官方域。"""
        session = self._session_obj()
        try:
            session.get(f"{HOME}/", timeout=10)
        except Exception:  # noqa: BLE001 —— 主站预热失败仍继续出码
            pass
        try:
            resp = session.get(f"{SSO}/get_qrcode/", params=self._params(), timeout=10)
        except Exception as exc:
            raise RuntimeError(f"生成二维码失败（网络？）: {sanitize_error_text(exc)}") from exc
        try:
            payload = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"生成二维码失败: HTTP {resp.status_code}") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = {}
        token = data.get("token") or ""
        qr_url = data.get("qrcode_index_url") or data.get("frontend_show_qrcode") or ""
        err_code = payload.get("error_code") if isinstance(payload, dict) else None
        if not token or not qr_url:
            msg = ""
            if isinstance(payload, dict):
                msg = payload.get("description") or payload.get("message") or ""
            if err_code not in (0, None, "0"):
                msg = msg or f"error_code={err_code}"
            raise RuntimeError(f"生成二维码失败: {msg or resp.status_code}")
        if not is_douyin_qr_url(qr_url):
            raise RuntimeError("生成二维码失败：返回的二维码地址不是官方域名")
        return {"token": token, "url": qr_url}

    def poll_qr(self, token: str) -> dict:
        """轮询扫码状态。返回 {status, redirect_url}。"""
        if not token:
            raise RuntimeError("轮询二维码失败：缺少 token")
        params = self._params()
        params["token"] = token
        try:
            resp = self._session_obj().get(
                f"{SSO}/check_qrconnect/", params=params, timeout=10
            )
        except Exception as exc:
            raise RuntimeError(f"轮询失败（网络？）: {sanitize_error_text(exc)}") from exc
        try:
            payload = resp.json() if resp.content else {}
        except ValueError:
            payload = {}
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = {}
        return {
            "status": _map_qr_status(data),
            "redirect_url": data.get("redirect_url") or "",
        }

    def finalize(self, redirect_url: str) -> None:
        """跟随确认后的官方跳转，落地 sessionid 等 Cookie。"""
        url = (redirect_url or "").strip()
        if not url:
            return
        if not is_douyin_redirect_url(url):
            host = urlparse(url).netloc or "未知域名"
            raise ValueError(f"登录回调域名不是抖音官方（{host}）")
        session = self._session_obj()
        session.get(url, timeout=10)
        try:
            session.get(f"{HOME}/", timeout=10)
        except Exception:  # noqa: BLE001
            pass

    def persist(self) -> str:
        """把当前 session cookie 写入 CookieConfigManager，并探测登录态。"""
        cookies = _cookie_dict_from_jar(self._session_obj().cookies)
        raw = format_cookie(cookies)
        if not has_sessionid(cookies):
            return "登录成功但未取到 sessionid"
        self._cookie_mgr.set("douyin", raw)
        return verify_douyin_login(cookie_mgr=self._cookie_mgr)


def verify_douyin_login(cookie_mgr: Optional[CookieConfigManager] = None) -> str:
    """探测已存登录态。空串=成功，否则是错误信息（不含 cookie 明文）。"""
    mgr = cookie_mgr or CookieConfigManager()
    cookies = parse_cookie_string(mgr.get("douyin") or "")
    if not has_sessionid(cookies):
        return "未配置 sessionid"
    try:
        with PublicOnlySession() as session:
            session.headers.update(_WEB_HEADERS)
            for name, value in cookies.items():
                try:
                    session.cookies.set(name, value, domain=".douyin.com")
                except Exception:  # noqa: BLE001
                    session.cookies.set(name, value)
            resp = session.get(ACCOUNT_INFO, params={"aid": "6383"}, timeout=15)
        if resp.status_code in (401, 403):
            return "登录态无效或已过期，请重新 `videonote login douyin`"
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}"
        try:
            body = resp.json()
        except ValueError:
            body = {}
        data = body.get("data") if isinstance(body, dict) else None
        if isinstance(data, dict) and (
            data.get("user_id") or data.get("uid") or data.get("name") or data.get("uniq_id")
        ):
            return ""
        if isinstance(body, dict) and body.get("message") == "success" and data:
            return ""
        return "登录态无效或未能确认，请重新 `videonote login douyin`"
    except Exception as exc:  # noqa: BLE001
        logger.info("抖音登录探测失败: %s", sanitize_error_text(exc))
        return "请求失败（网络？检查 `videonote proxy list`）"
