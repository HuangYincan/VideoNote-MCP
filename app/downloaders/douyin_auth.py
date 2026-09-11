"""抖音 Web 扫码登录。

直连 `sso.douyin.com/get_qrcode/` 常被风控成 HTML（HTTP 200 非 JSON）。
默认扫码走本机 Chrome 打开官网登录页（`douyin_browser`），拦截
`passport/web/get_qrcode` + `check_qrconnect`；本模块是无浏览器时的 HTTP 回退，
以及 Cookie 校验 / 粘贴路径。确认后跟随官方 `redirect_url` 收集 Cookie，
写入 CookieConfigManager 的 `douyin` 槽。

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
from app.utils.url_safety import (
    PublicOnlySession,
    host_matches,
    hostname_matches,
    sanitize_error_text,
)

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

# 扫码状态（归一后）：1 等待 / 2 已扫待确认 / 3 已确认 / 5 过期 / verification_required 二次验证
# SSO 用数字；passport/web 用 new / scanned / confirmed / expired
QR_WAIT = "1"
QR_SCANNED = "2"
QR_SUCCESS = "3"
QR_EXPIRED = "5"
QR_VERIFY = "verification_required"

_QR_HOSTS = ("douyin.com", "snssdk.com", "amemv.com", "iesdouyin.com")
_REDIRECT_HOSTS = ("douyin.com", "snssdk.com")
_COOKIE_HOSTS = ("douyin.com",)


def is_douyin_qr_url(url: str) -> bool:
    """扫码 payload 只允许抖音官方扫码页（含 api.amemv.com）。"""
    return host_matches(url, *_QR_HOSTS)


def is_douyin_redirect_url(url: str) -> bool:
    """登录回调只认官方后缀，拒绝跟随到任意 Location。"""
    return host_matches(url, *_REDIRECT_HOSTS)


def is_douyin_cookie_host(host: str) -> bool:
    """只收集 .douyin.com 精确后缀上的 Cookie。"""
    return hostname_matches(host, *_COOKIE_HOSTS)


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


def _is_html_response(resp) -> bool:
    """SSO 被风控时返回 HTTP 200 + text/html，不是 JSON。"""
    headers = getattr(resp, "headers", None) or {}
    try:
        ct = str(headers.get("content-type") or headers.get("Content-Type") or "")
    except Exception:  # noqa: BLE001
        ct = ""
    text = getattr(resp, "text", None)
    if not isinstance(text, str):
        content = getattr(resp, "content", b"") or b""
        if isinstance(content, (bytes, bytearray)):
            text = bytes(content[:200]).decode("utf-8", "replace")
        else:
            text = str(content or "")
    head = (text or "")[:240].lstrip().lower()
    return "html" in ct.lower() or head.startswith("<!doctype") or head.startswith("<html")


def qr_requires_verification(payload: dict) -> bool:
    """只识别已实测的身份验证错误 2046，不把普通网络/风控错误当成登录成功。"""
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    return any(
        isinstance(item, dict) and item.get("error_code") in (2046, "2046")
        for item in (payload, data)
    )


def _map_qr_status(data: dict) -> str:
    """把 SSO / passport data.status / redirect_url 归一成 QR_* 常量。

    旧 SSO 数字：1 等待 / 2 已扫 / 3·4 成功（4 常带 redirect_url）/ 5 过期。
    passport/web：new / scanned / confirmed；4 / refused / expired 是失效刷新。
    """
    if qr_requires_verification(data):
        return QR_VERIFY
    redirect = (data.get("redirect_url") or "") if isinstance(data, dict) else ""
    raw = ""
    if isinstance(data, dict):
        raw = str(data.get("status", "") or "").strip().lower()
    if redirect or raw in ("3", "confirmed", "success"):
        return QR_SUCCESS
    if raw in ("2", "scanned", "scanning"):
        return QR_SCANNED
    if raw in ("4", "5", "expired", "expire", "canceled", "cancelled", "refused"):
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
            if _is_html_response(resp):
                raise RuntimeError(
                    "抖音返回了风控页而不是二维码。请安装 Google Chrome 后重试，"
                    "或改用 `videonote login douyin --cookie`"
                ) from exc
            raise RuntimeError(f"HTTP {resp.status_code}（非 JSON）") from exc
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = {}
        token = data.get("token") or ""
        qr_url = data.get("qrcode_index_url") or data.get("frontend_show_qrcode") or ""
        err_code = data.get("error_code")
        if err_code in (None, "", 0, "0") and isinstance(payload, dict):
            err_code = payload.get("error_code")
        if not token or not qr_url:
            msg = ""
            if isinstance(data, dict):
                msg = str(data.get("description") or "")
            if isinstance(payload, dict):
                msg = msg or str(payload.get("description") or payload.get("message") or "")
            if err_code not in (0, None, "0"):
                msg = msg or f"error_code={err_code}"
            if err_code in (4031, "4031") or "安全风险" in msg:
                raise RuntimeError(
                    f"{msg or '直连接口被风控'}。请安装 Google Chrome 后重试，"
                    "或改用 `videonote login douyin --cookie`"
                )
            raise RuntimeError(msg or f"HTTP {resp.status_code}")
        if not is_douyin_qr_url(qr_url):
            raise RuntimeError("返回的二维码地址不是官方域名")
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
            "status": QR_VERIFY if qr_requires_verification(payload) else _map_qr_status(data),
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
    """附加校验：空串=已确认；非空区分认证拒绝与无法确认，不含凭证明文。

    这是独立 HTTP 会话，不是刚扫码的浏览器。HTML、风控或未知 JSON 只能
    说明探测未能确认，不能据此把浏览器登录判成失败，也不能要求重新扫码。
    """
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
        if resp.status_code == 401:
            return "登录态无效或已过期，请重新 `videonote login douyin`"
        if resp.status_code != 200:
            return f"未能确认登录态：校验接口返回 HTTP {resp.status_code}"
        try:
            body = resp.json()
        except ValueError:
            return "未能确认登录态：校验接口未返回 JSON（可能受到风控限制）"
        data = body.get("data") if isinstance(body, dict) else None
        # 错误码优先于 message/账号字段；普通接口 success 也不是认证成功证据。
        for item in (body, data):
            if not isinstance(item, dict):
                continue
            for key in ("error_code", "status_code"):
                value = item.get(key)
                if value in (None, 0, "0", ""):
                    continue
                # 只回显短数字错误码，不输出响应正文、description 或任意服务端字符串。
                code = str(value) if isinstance(value, (int, str)) and not isinstance(value, bool) else ""
                if re.fullmatch(r"-?[0-9]{1,6}", code):
                    return f"未能确认登录态：校验接口返回 {key}={code}"
                return "未能确认登录态：校验接口返回异常状态"
        if isinstance(data, dict) and (
            data.get("user_id") or data.get("uid") or data.get("name") or data.get("uniq_id")
        ):
            return ""
        return "未能确认登录态：校验响应不含可识别的账号信息"
    except Exception as exc:  # noqa: BLE001
        # 网络异常可能包含 Cookie/响应片段，只记录类型。
        logger.info("抖音登录探测失败（%s）", type(exc).__name__)
        return "未能确认登录态：请求失败（网络？检查 `videonote proxy list`）"
