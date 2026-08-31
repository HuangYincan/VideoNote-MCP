"""小红书登录态 + 笔记页解析。

登录：官网 Web 扫码（edith `qrcode/create` + `qrcode/status`），cookie 存
CookieConfigManager 的 `xiaohongshu` 槽。凭证走 CLI：
`videonote login xiaohongshu` / `videonote cookie set xiaohongshu`。
MCP 工具不收 cookie（安全红线）。

笔记：拉 HTML 的 `window.__INITIAL_STATE__`（与 yt-dlp XiaoHongShuIE 同源），
取出视频直链；图文笔记 `video_url` 为空。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from app.downloaders.xiaohongshu_sign import get_a1_and_web_id, sign
from app.services.cookie_manager import CookieConfigManager
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.logger import get_logger
from app.utils.url_parser import extract_video_id, resolve_xiaohongshu_short_url
from app.utils.url_safety import PublicOnlySession, assert_public_http_url, sanitize_url

logger = get_logger(__name__)

EDITH = "https://edith.xiaohongshu.com"
HOME = "https://www.xiaohongshu.com"
QR_CREATE = "/api/sns/web/v1/login/qrcode/create"
QR_STATUS = "/api/sns/web/v1/login/qrcode/status"
ACTIVATE = "/api/sns/web/v1/login/activate"
ORIGIN_CDN = "https://sns-video-bd.xhscdn.com"
_LOGIN_HINT = (
    "配置请走 CLI：`! videonote login xiaohongshu` 或 "
    "`videonote cookie set xiaohongshu '...'`；MCP 工具不收 cookie（安全红线）"
)
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_WEB_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": HOME,
    "Referer": f"{HOME}/",
    "Content-Type": "application/json;charset=utf-8",
}

# 扫码状态：0 等待 / 1 已扫待确认 / 2 成功 / 3 过期
QR_WAIT = 0
QR_SCANNED = 1
QR_SUCCESS = 2
QR_EXPIRED = 3


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


def canonicalize_note_url(url: str) -> str:
    """短链解成带 xsec_token 的长链；解不出则原样返回。"""
    if "xhslink." in (url or "").lower():
        return resolve_xiaohongshu_short_url(url) or url
    return url


def _proxies() -> Optional[dict]:
    proxy = ProxyConfigManager().get_proxy_url()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


@dataclass
class XiaohongshuNote:
    note_id: str
    title: str = ""
    video_url: Optional[str] = None
    cover_url: Optional[str] = None
    duration: float = 0.0
    desc: str = ""
    page_url: str = ""
    raw: dict = field(default_factory=dict)


def parse_initial_state(html: str) -> Optional[dict]:
    """从笔记页 HTML 抽出 `window.__INITIAL_STATE__`。"""
    if not html:
        return None
    marker = "window.__INITIAL_STATE__"
    idx = html.find(marker)
    if idx < 0:
        return None
    eq = html.find("=", idx)
    if eq < 0:
        return None
    start = eq + 1
    end = html.find("</script>", start)
    blob = html[start:end if end > 0 else None].strip().rstrip(";")
    blob = re.sub(r"\bundefined\b", "null", blob)
    try:
        data = json.loads(blob)
    except ValueError:
        logger.info("小红书 INITIAL_STATE 不是合法 JSON")
        return None
    return data if isinstance(data, dict) else None


def _pick_video_url(note: dict) -> tuple[Optional[str], float]:
    """从 note.video 选一条可下的直链；时长按毫秒换算为秒。"""
    video = note.get("video") if isinstance(note, dict) else None
    if not isinstance(video, dict):
        return None, 0.0
    media = video.get("media") if isinstance(video.get("media"), dict) else {}
    stream = media.get("stream") if isinstance(media.get("stream"), dict) else {}
    candidates = []
    for codec, items in stream.items():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            url = item.get("masterUrl")
            if not url:
                backups = item.get("backupUrls")
                url = backups[0] if isinstance(backups, list) and backups else None
            if not url or not str(url).startswith("http"):
                continue
            height = item.get("height") or 0
            try:
                height = int(height)
            except (TypeError, ValueError):
                height = 0
            dur = item.get("duration")
            try:
                duration_s = float(dur) / 1000.0 if dur is not None else 0.0
            except (TypeError, ValueError):
                duration_s = 0.0
            candidates.append((codec != "h264", -height, str(url), duration_s))
    if candidates:
        candidates.sort()
        _pref, _h, url, duration_s = candidates[0]
        return url, duration_s
    consumer = video.get("consumer") if isinstance(video.get("consumer"), dict) else {}
    origin = consumer.get("originVideoKey") or consumer.get("origin_video_key")
    if origin:
        return f"{ORIGIN_CDN}/{origin}", 0.0
    return None, 0.0


def _cover_url(note: dict) -> Optional[str]:
    images = note.get("imageList") or note.get("image_list") or []
    if isinstance(images, list):
        for img in images:
            if not isinstance(img, dict):
                continue
            for key in ("urlDefault", "urlPre", "url"):
                u = img.get(key)
                if u and str(u).startswith("http"):
                    return str(u)
    video = note.get("video") if isinstance(note.get("video"), dict) else {}
    for key in ("cover", "thumbnail"):
        u = video.get(key)
        if isinstance(u, str) and u.startswith("http"):
            return u
        if isinstance(u, dict):
            for k in ("urlDefault", "urlPre", "url"):
                vu = u.get(k)
                if vu and str(vu).startswith("http"):
                    return str(vu)
    return None


def note_from_state(state: dict, note_id: str, page_url: str) -> XiaohongshuNote:
    nmap = ((state.get("note") or {}) if isinstance(state, dict) else {}).get("noteDetailMap") or {}
    if not isinstance(nmap, dict):
        nmap = {}
    card = nmap.get(note_id)
    if not isinstance(card, dict) and nmap:
        card = next(iter(nmap.values()), None)
    note = (card or {}).get("note") if isinstance(card, dict) else None
    if not isinstance(note, dict):
        note = {}
    video_url, duration = _pick_video_url(note)
    title = (note.get("title") or "").strip() or (note.get("desc") or "").strip()[:80]
    return XiaohongshuNote(
        note_id=note_id,
        title=title,
        video_url=video_url,
        cover_url=_cover_url(note),
        duration=duration,
        desc=(note.get("desc") or "").strip(),
        page_url=page_url,
        raw=note,
    )


def _cookie_dict_from_jar(jar) -> dict:
    try:
        return {c.name: c.value for c in jar}
    except Exception:  # noqa: BLE001
        try:
            return dict(jar)
        except Exception:  # noqa: BLE001
            return {}


class XiaohongshuAuth:
    """扫码登录 + 带 cookie 的笔记页抓取。session 可注入（测试 mock）。"""

    def __init__(self, session=None, cookie_mgr: Optional[CookieConfigManager] = None):
        self._cookie_mgr = cookie_mgr or CookieConfigManager()
        self._session = session
        self._owns_session = session is None

    def _session_obj(self):
        if self._session is None:
            self._session = PublicOnlySession()
            self._session.headers.update(_WEB_HEADERS)
            self._owns_session = True
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

    def _apply_cookie_string(self, raw: str) -> None:
        session = self._session_obj()
        for name, value in parse_cookie_string(raw).items():
            try:
                session.cookies.set(name, value, domain=".xiaohongshu.com")
            except Exception:  # noqa: BLE001
                session.cookies.set(name, value)

    def _seed_device(self) -> None:
        session = self._session_obj()
        jar = _cookie_dict_from_jar(session.cookies)
        if jar.get("a1") and jar.get("webId"):
            return
        a1, web_id = get_a1_and_web_id()
        for name, value in (
            ("a1", a1),
            ("webId", web_id),
            ("xsecappid", "xhs-pc-web"),
            ("webBuild", "4.62.3"),
        ):
            if not jar.get(name):
                try:
                    session.cookies.set(name, value, domain=".xiaohongshu.com")
                except Exception:  # noqa: BLE001
                    session.cookies.set(name, value)

    def _a1(self) -> str:
        return _cookie_dict_from_jar(self._session_obj().cookies).get("a1") or ""

    def _sign_headers(self, uri: str, data=None) -> dict:
        headers = dict(sign(uri, data, a1=self._a1()))
        headers.update(_WEB_HEADERS)
        return headers

    def _request(self, method: str, url: str, *, uri: str, data=None, params=None, timeout: int = 15):
        session = self._session_obj()
        headers = self._sign_headers(uri, data)
        kwargs = {"headers": headers, "timeout": timeout, "proxies": _proxies()}
        if method.upper() == "GET":
            return session.get(url, params=params, **kwargs)
        return session.post(url, json=data if data is not None else {}, **kwargs)

    def activate(self) -> None:
        """拿游客 web_session（匿名也能看部分笔记）。失败不致命。"""
        try:
            self._seed_device()
            self._request("POST", f"{EDITH}{ACTIVATE}", uri=ACTIVATE, data={})
        except Exception as exc:  # noqa: BLE001
            logger.info("小红书 activate 失败（可忽略）: %s", exc)

    def create_qr(self) -> dict:
        """申请扫码二维码。返回 {qr_id, code, url}。"""
        self._seed_device()
        payload = {"qr_type": 1}
        resp = self._request("POST", f"{EDITH}{QR_CREATE}", uri=QR_CREATE, data=payload)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or not data.get("url"):
            msg = ""
            if isinstance(body, dict):
                msg = body.get("msg") or body.get("message") or ""
            if resp.status_code == 406:
                raise RuntimeError(
                    "小红书拒绝了旧版请求签名（HTTP 406）。"
                    "请安装 Google Chrome 后重试扫码，或 "
                    "`videonote login xiaohongshu --cookie`"
                )
            raise RuntimeError(f"生成二维码失败: {msg or resp.status_code}")
        return {
            "qr_id": data.get("qr_id") or "",
            "code": data.get("code") or "",
            "url": data.get("url") or "",
        }

    def poll_qr(self, qr_id: str, code: str) -> dict:
        """轮询扫码状态。返回 {code_status, login_info?}。"""
        params = {"qr_id": qr_id, "code": code}
        uri = f"{QR_STATUS}?qr_id={qr_id}&code={code}"
        resp = self._request("GET", f"{EDITH}{QR_STATUS}", uri=uri, params=params)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError(f"轮询二维码失败: {resp.status_code}")
        status = data.get("code_status")
        try:
            status = int(status)
        except (TypeError, ValueError):
            status = QR_WAIT
        info = data.get("login_info") if isinstance(data.get("login_info"), dict) else {}
        sess = info.get("session")
        if status == QR_SUCCESS and sess:
            try:
                self._session_obj().cookies.set("web_session", sess, domain=".xiaohongshu.com")
            except Exception:  # noqa: BLE001
                self._session_obj().cookies.set("web_session", sess)
        return {"code_status": status, "login_info": info}

    def persist(self) -> str:
        """把当前 session cookie 写入 CookieConfigManager，并探测登录态。"""
        cookies = _cookie_dict_from_jar(self._session_obj().cookies)
        raw = format_cookie(cookies)
        if not cookies.get("web_session"):
            return "登录成功但未取到 web_session"
        self._cookie_mgr.set("xiaohongshu", raw)
        return verify_xiaohongshu_login(cookie_mgr=self._cookie_mgr)

    def load_stored(self) -> None:
        raw = self._cookie_mgr.get("xiaohongshu") or ""
        if raw:
            self._apply_cookie_string(raw)
        else:
            self._seed_device()

    def fetch_note(self, video_url: str) -> XiaohongshuNote:
        """拉笔记页并解析。短链先解；缺登录时仍尝试（公开笔记可能成功）。"""
        assert_public_http_url(video_url)
        page_url = canonicalize_note_url(video_url)
        note_id = extract_video_id(page_url, "xiaohongshu")
        if not note_id:
            raise ValueError(f"无法从小红书链接提取笔记 ID: {sanitize_url(video_url)}")
        self.load_stored()
        if not _cookie_dict_from_jar(self._session_obj().cookies).get("web_session"):
            self.activate()
        session = self._session_obj()
        headers = {
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{HOME}/",
        }
        try:
            resp = session.get(page_url, headers=headers, timeout=20, proxies=_proxies())
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"请求小红书笔记页失败: {exc}") from exc
        if resp.status_code != 200:
            raise RuntimeError(
                f"小红书笔记页 HTTP {resp.status_code}。{ _LOGIN_HINT}"
            )
        state = parse_initial_state(resp.text or "")
        if not state:
            raise RuntimeError(
                "无法解析小红书笔记页（可能需登录或遇到验证码）。" + _LOGIN_HINT
            )
        note = note_from_state(state, note_id, page_url)
        if not note.title:
            # og:title 兜底
            m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', resp.text or "", re.I)
            if m:
                note.title = m.group(1).strip()
        return note


def verify_xiaohongshu_login(cookie_mgr: Optional[CookieConfigManager] = None) -> str:
    """探测已存登录态。空串=成功，否则是错误信息（不含 cookie 明文）。"""
    mgr = cookie_mgr or CookieConfigManager()
    cookies = parse_cookie_string(mgr.get("xiaohongshu") or "")
    if not cookies.get("web_session"):
        return "未配置 web_session"
    auth = XiaohongshuAuth(cookie_mgr=mgr)
    try:
        auth._apply_cookie_string(format_cookie(cookies))
        session = auth._session_obj()
        headers = {
            "User-Agent": _UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": f"{HOME}/",
        }
        # 不用 edith user/me：旧版 x-s 会被 406，误报登录失败。
        resp = session.get(HOME, headers=headers, timeout=15, proxies=_proxies())
        if resp.status_code in (401, 403, 471):
            return "登录态无效或已过期，请重新 `videonote login xiaohongshu`"
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}"
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.info("小红书登录探测失败: %s", exc)
        return "请求失败（网络？检查 `videonote proxy list`）"
    finally:
        auth.close()
