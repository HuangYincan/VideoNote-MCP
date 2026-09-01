"""小宇宙官方文稿：走 episode-transcript API，绕过本地 ASR。

流程：
1. 从 URL 提 episode id（/episode/{eid}）
2. GET /v1/episode/get?eid= → 拿 transcriptMediaId（RSS 转载优先这个，不是 media.id）
3. POST /v1/episode-transcript/get {eid, mediaId} → 签名 transcriptUrl
4. GET transcriptUrl（必须 Android UA，CDN 会校验）→ [{text, startMs}, ...]
5. 解析为 TranscriptResult

官方文稿需要登录态（x-jike-access-token）。access token 约 2 小时过期，
配套 x-jike-refresh-token 长效；401 时自动续期并写回 CookieConfigManager。
凭证一律走 CLI：`videonote login xiaoyuzhou` / `videonote cookie set xiaoyuzhou`。
"""
from __future__ import annotations

import re
import uuid
from typing import List, Optional

from app.exceptions.task import OfficialTranscriptFetchError
from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.cookie_manager import CookieConfigManager
from app.utils.logger import get_logger
from app.utils.url_parser import extract_video_id
from app.utils.url_safety import public_get, public_post

logger = get_logger(__name__)

API_BASE = "https://api.xiaoyuzhoufm.com"
APP_UA = "Xiaoyuzhou/2.99.1(android 28)"
_LOGIN_HINT = (
    "配置请走 CLI：`! videonote login xiaoyuzhou` 或 "
    "`videonote cookie set xiaoyuzhou 'x-jike-access-token=...; x-jike-refresh-token=...'`；"
    "MCP 工具不收 token（安全红线）"
)


def parse_xiaoyuzhou_tokens(raw: str) -> dict:
    """把 cookie 字符串或裸 JWT 解析成 access / refresh / device。

    接受：
    - `x-jike-access-token=...; x-jike-refresh-token=...`
    - `access_token=...; refresh_token=...`
    - 单独一枚 JWT（当作 access token）
    """
    out = {"access": None, "refresh": None, "device": None}
    text = (raw or "").strip()
    if not text:
        return out
    first = text.splitlines()[0].strip()
    if ";" not in text and "=" not in first and first.count(".") >= 2:
        out["access"] = first
        return out
    for part in re.split(r"[;\n]+", text):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().lower()
        value = value.strip().strip('"').strip("'")
        if not value:
            continue
        if key in ("x-jike-access-token", "access-token", "access_token"):
            out["access"] = value
        elif key in ("x-jike-refresh-token", "refresh-token", "refresh_token"):
            out["refresh"] = value
        elif key in ("x-jike-device-id", "device-id", "device_id"):
            out["device"] = value
    return out


def format_xiaoyuzhou_cookie(access: str, refresh: str = "", device: str = "") -> str:
    parts = [f"x-jike-access-token={access}"]
    if refresh:
        parts.append(f"x-jike-refresh-token={refresh}")
    if device:
        parts.append(f"x-jike-device-id={device}")
    return "; ".join(parts)


def _app_headers(access_token: Optional[str] = None, device_id: Optional[str] = None) -> dict:
    headers = {
        "User-Agent": APP_UA,
        "applicationid": "app.podcast.cosmos",
        "app-version": "2.99.1",
        "os": "android",
        "content-type": "application/json;charset=utf-8",
        "accept": "application/json, text/plain, */*",
    }
    if access_token:
        headers["x-jike-access-token"] = access_token
    if device_id:
        headers["x-jike-device-id"] = device_id
    return headers


class XiaoyuzhouTranscriptFetcher:
    """通过小宇宙官方 API 直拉文稿。"""

    def __init__(self, cookie_mgr: Optional[CookieConfigManager] = None):
        self._cookie_mgr = cookie_mgr or CookieConfigManager()

    def _load_tokens(self) -> dict:
        tokens = parse_xiaoyuzhou_tokens(self._cookie_mgr.get("xiaoyuzhou") or "")
        if tokens["access"] and not tokens["device"]:
            tokens["device"] = str(uuid.uuid4())
            self._persist(tokens)
        return tokens

    def _persist(self, tokens: dict) -> None:
        access = tokens.get("access")
        if not access:
            return
        self._cookie_mgr.set(
            "xiaoyuzhou",
            format_xiaoyuzhou_cookie(
                access,
                tokens.get("refresh") or "",
                tokens.get("device") or "",
            ),
        )

    def _refresh(self, tokens: dict) -> bool:
        refresh = tokens.get("refresh")
        if not refresh:
            return False
        headers = _app_headers(device_id=tokens.get("device"))
        headers["x-jike-refresh-token"] = refresh
        try:
            resp = public_post(f"{API_BASE}/app_auth_tokens.refresh", headers=headers, timeout=15)
        except Exception as exc:  # noqa: BLE001
            logger.warning("小宇宙 refresh token 续期失败: %s", exc)
            return False
        if resp.status_code != 200:
            logger.info("小宇宙 refresh token 续期返回 HTTP %s", resp.status_code)
            return False
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        new_access = resp.headers.get("x-jike-access-token") or body.get("x-jike-access-token")
        new_refresh = resp.headers.get("x-jike-refresh-token") or body.get("x-jike-refresh-token")
        if not new_access:
            return False
        tokens["access"] = new_access
        if new_refresh:
            tokens["refresh"] = new_refresh
        self._persist(tokens)
        return True

    def _send(self, method: str, url: str, headers: dict, payload, params):
        if method.upper() == "GET":
            return public_get(url, headers=headers, params=params, timeout=15)
        return public_post(url, headers=headers, json=payload, timeout=15)

    def _request(
        self,
        method: str,
        path: str,
        tokens: dict,
        *,
        payload: Optional[dict] = None,
        params: Optional[dict] = None,
        allow_refresh: bool = True,
    ):
        url = f"{API_BASE}{path}"
        headers = _app_headers(tokens.get("access"), tokens.get("device"))
        last_exc: Optional[Exception] = None
        resp = None
        for attempt in range(2):
            try:
                resp = self._send(method, url, headers, payload, params)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("小宇宙 API %s %s 失败: %s", method, path, exc)
                continue
            if resp.status_code >= 500 and attempt == 0:
                logger.warning("小宇宙 API %s %s HTTP %s，将重试", method, path, resp.status_code)
                continue
            break
        else:
            raise OfficialTranscriptFetchError(
                f"小宇宙官方文稿请求失败: {method} {path}。已登录时不会回退本地转写。{_LOGIN_HINT}"
            ) from last_exc
        if resp.status_code == 401 and allow_refresh and tokens.get("refresh"):
            if self._refresh(tokens):
                return self._request(
                    method,
                    path,
                    tokens,
                    payload=payload,
                    params=params,
                    allow_refresh=False,
                )
            logger.info("小宇宙登录已过期，请重新 %s", _LOGIN_HINT)
        return resp

    def _episode_media_id(self, data: dict) -> Optional[str]:
        """官方文稿要 native transcriptMediaId；RSS 转载的 media.id 是外链，会返回 no_subtitle。"""
        if not isinstance(data, dict):
            return None
        mid = data.get("transcriptMediaId")
        transcript = data.get("transcript")
        if not mid and isinstance(transcript, dict):
            mid = transcript.get("mediaId")
        if not mid:
            media = data.get("media")
            if isinstance(media, dict):
                mid = media.get("id")
        return mid or None

    def fetch_subtitles(self, video_url: str) -> Optional[TranscriptResult]:
        eid = extract_video_id(video_url, "xiaoyuzhou")
        if not eid:
            logger.info("无法从小宇宙 URL 提取 episode id")
            return None

        tokens = self._load_tokens()
        if not tokens.get("access"):
            logger.info("未配置小宇宙登录态：官方文稿拿不到，将走语音识别。%s", _LOGIN_HINT)
            return None

        ep_resp = self._request("GET", "/v1/episode/get", tokens, params={"eid": eid})
        if ep_resp.status_code == 401:
            raise OfficialTranscriptFetchError(
                f"小宇宙官方文稿 401：登录态无效。{_LOGIN_HINT}"
            )
        if ep_resp.status_code != 200:
            raise OfficialTranscriptFetchError(
                f"小宇宙 episode/get HTTP {ep_resp.status_code}。已登录时不会回退本地转写。"
            )
        try:
            ep_json = ep_resp.json()
        except ValueError as exc:
            raise OfficialTranscriptFetchError("小宇宙 episode/get 返回非 JSON") from exc
        episode = ep_json.get("data") if isinstance(ep_json, dict) else None
        if not isinstance(episode, dict):
            logger.info("小宇宙 episode/get 无 data：eid=%s", eid)
            return None

        media_id = self._episode_media_id(episode)
        if not media_id:
            logger.info("该期没有官方文稿（无 transcriptMediaId）：eid=%s", eid)
            return None

        tr_resp = self._request(
            "POST",
            "/v1/episode-transcript/get",
            tokens,
            payload={"eid": eid, "mediaId": media_id},
        )
        if tr_resp.status_code == 401:
            raise OfficialTranscriptFetchError(
                f"小宇宙文稿接口 401：登录态无效。{_LOGIN_HINT}"
            )
        if tr_resp.status_code != 200:
            raise OfficialTranscriptFetchError(
                f"小宇宙 episode-transcript/get HTTP {tr_resp.status_code}。已登录时不会回退本地转写。"
            )
        try:
            tr_json = tr_resp.json()
        except ValueError as exc:
            raise OfficialTranscriptFetchError(
                "小宇宙 episode-transcript/get 返回非 JSON"
            ) from exc

        inner = tr_json.get("data") if isinstance(tr_json, dict) else None
        if isinstance(inner, dict) and isinstance(inner.get("data"), dict):
            inner = inner["data"]
        transcript_url = inner.get("transcriptUrl") if isinstance(inner, dict) else None
        if not transcript_url:
            logger.info("该期官方文稿不可用（no_subtitle）：eid=%s", eid)
            return None

        body_resp = None
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            try:
                body_resp = public_get(
                    transcript_url,
                    headers={"User-Agent": APP_UA},
                    timeout=30,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning("下载小宇宙文稿 CDN 失败: %s", exc)
                continue
            if body_resp.status_code >= 500 and attempt == 0:
                logger.warning("小宇宙文稿 CDN HTTP %s，将重试", body_resp.status_code)
                continue
            break
        else:
            raise OfficialTranscriptFetchError(
                "下载小宇宙文稿 CDN 失败。已登录时不会回退本地转写。"
            ) from last_exc
        if body_resp.status_code != 200:
            raise OfficialTranscriptFetchError(
                f"小宇宙文稿 CDN HTTP {body_resp.status_code}（403 通常是 UA 白名单变化）"
            )
        try:
            raw_segs = body_resp.json()
        except ValueError as exc:
            raise OfficialTranscriptFetchError("小宇宙文稿 CDN 返回非 JSON") from exc
        if not isinstance(raw_segs, list):
            logger.warning("小宇宙文稿形状异常：%s", type(raw_segs).__name__)
            return None

        duration = episode.get("duration")
        try:
            duration_s = float(duration) if duration is not None else None
        except (TypeError, ValueError):
            duration_s = None

        parsed: List[tuple] = []
        for item in raw_segs:
            if not isinstance(item, dict):
                continue
            text = (item.get("text") or "").strip()
            if not text:
                continue
            try:
                start = float(item.get("startMs") or 0) / 1000.0
            except (TypeError, ValueError):
                start = 0.0
            end = item.get("endMs")
            try:
                end_s = float(end) / 1000.0 if end is not None else None
            except (TypeError, ValueError):
                end_s = None
            parsed.append((start, end_s, text))

        segments: List[TranscriptSegment] = []
        for i, (start, end_s, text) in enumerate(parsed):
            if end_s is not None:
                end = end_s
            elif i + 1 < len(parsed):
                end = parsed[i + 1][0]
            elif duration_s is not None and duration_s > start:
                end = duration_s
            else:
                end = start
            segments.append(TranscriptSegment(start=start, end=end, text=text))

        if not segments:
            return None

        full_text = " ".join(s.text for s in segments)
        logger.info("小宇宙官方文稿成功: eid=%s 共 %s 段", eid, len(segments))
        return TranscriptResult(
            language="zh",
            full_text=full_text,
            segments=segments,
            raw={
                "source": "xiaoyuzhou_official_transcript",
                "eid": eid,
                "media_id": media_id,
            },
        )


def verify_xiaoyuzhou_login() -> str:
    """探测已存登录态。空串=成功，否则是错误信息（不含 token 明文）。"""
    fetcher = XiaoyuzhouTranscriptFetcher()
    tokens = fetcher._load_tokens()
    if not tokens.get("access"):
        return "未配置 x-jike-access-token"
    try:
        resp = fetcher._request(
            "POST",
            "/v1/subscription/list",
            tokens,
            payload={"limit": "1", "sortOrder": "desc", "sortBy": "subscribedAt"},
        )
    except OfficialTranscriptFetchError:
        return "请求失败（网络？检查 `videonote proxy list`）"
    if resp.status_code == 200:
        return ""
    if resp.status_code == 401:
        return "登录态无效或已过期（需要有效的 x-jike-refresh-token 或重新登录）"
    return f"HTTP {resp.status_code}"
