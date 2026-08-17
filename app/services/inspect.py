"""解析视频链接：单集 / B 站分 P / YouTube（及 yt-dlp）播放列表。

只做元信息，不下载。返回每集可直接喂给 generate_note / prepare_note_material 的 url。
Agent 按单视频流程处理；本模块不批量提交。
"""
from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from app.services.pipeline import detect_platform
from app.utils.url_safety import assert_public_http_url

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 200
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def inspect_video(url: str, platform: Optional[str] = None) -> dict:
    """解析链接，列出可独立生成笔记的条目。

    返回 {ok, platform, kind: single|multi, title, video_id, current_p?,
          total, truncated, entries:[{p, title, duration, url, video_id}]}。
    失败 {ok: False, error}，不抛给调用方。
    """
    raw = (url or "").strip()
    if not raw:
        return {"ok": False, "error": "url 为空"}
    try:
        plat = platform or detect_platform(raw)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # SSRF 入口守卫（#133 A1）：本地路径已在上面分流；其余平台（bilibili/
    # kuaishou/douyin 的短链解析、yt-dlp generic 展开）统一校验，防显式
    # platform 参数绕过 #132 A1（原只覆盖 generic/youtube 下载器内部）
    if plat != "local":
        try:
            assert_public_http_url(raw)
        except ValueError as exc:
            return {"ok": False, "platform": plat, "error": str(exc)}

    try:
        if plat == "bilibili":
            return _inspect_bilibili(raw)
        if plat == "local":
            # file:// URI 先规整（#133 B2）：#130 A5 用裸 Path(raw) 漏了 file://——
            # inspect 曾是全工具面唯一不认 file:// 的本地入口（#105/#107 系列输入
            # 规整的漏网点），同一文件 validate_url/generate_note 可用、inspect
            # 却报「本地文件不存在」。entries[].url 透传规整后的路径。
            from videonote_mcp.server import _coerce_local_path

            local_path = _coerce_local_path(raw)
            if not local_path.is_file():
                return {
                    "ok": False,
                    "platform": "local",
                    "kind": "single",
                    "error": f"本地文件不存在: {raw}",
                }
            return {
                "ok": True,
                "platform": "local",
                "kind": "single",
                "title": "",
                "video_id": None,
                "total": 1,
                "truncated": False,
                "entries": [{"p": 1, "title": "", "duration": None, "url": str(local_path), "video_id": None}],
            }
        return _inspect_ytdlp(raw, plat)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"inspect_video 失败: {exc}")
        return {"ok": False, "platform": plat, "error": str(exc)}


def _bili_headers() -> dict:
    h = {"User-Agent": _UA, "Referer": "https://www.bilibili.com"}
    try:
        from app.services.cookie_manager import CookieConfigManager

        cookie = CookieConfigManager().get("bilibili") or ""
        if cookie:
            h["Cookie"] = cookie
    except Exception:
        pass
    return h


def _inspect_bilibili(url: str) -> dict:
    import requests

    from app.utils.url_parser import (
        extract_bilibili_p_number,
        extract_video_id,
        resolve_bilibili_short_url,
    )

    resolved = url
    if "b23.tv" in url:
        resolved = resolve_bilibili_short_url(url) or url
    bvid = extract_video_id(resolved, "bilibili")
    if not bvid:
        return {"ok": False, "platform": "bilibili", "error": "无法从链接提取 BV 号"}
    current_p = extract_bilibili_p_number(resolved)

    resp = requests.get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bvid},
        headers=_bili_headers(),
        timeout=10,
    )
    data = resp.json()
    if data.get("code") != 0:
        return {
            "ok": False,
            "platform": "bilibili",
            "error": f"view API: code={data.get('code')} {data.get('message')}",
        }
    d = data.get("data") or {}
    pages = d.get("pages") or []
    title = d.get("title") or ""
    if not pages:
        pages = [{"page": 1, "part": title, "duration": d.get("duration"), "cid": d.get("cid")}]

    total = len(pages)
    truncated = total > _MAX_ENTRIES
    entries: List[dict] = []
    for pg in pages[:_MAX_ENTRIES]:
        p = int(pg.get("page") or (len(entries) + 1))
        entries.append(
            {
                "p": p,
                "title": pg.get("part") or title,
                "duration": pg.get("duration"),
                "url": f"https://www.bilibili.com/video/{bvid}?p={p}",
                "video_id": bvid,
                "cid": pg.get("cid"),
            }
        )
    kind = "multi" if total > 1 else "single"
    return {
        "ok": True,
        "platform": "bilibili",
        "kind": kind,
        "title": title,
        "video_id": bvid,
        "current_p": current_p,
        "total": total,
        "truncated": truncated,
        "entries": entries,
    }


def _inspect_ytdlp(url: str, platform: str) -> dict:
    """YouTube / generic：extract_flat 列出播放列表，否则单条。"""
    # SSRF 防护（docs/05 第 16 轮 A1）：yt-dlp 边界先校验
    assert_public_http_url(url)
    import yt_dlp

    from app.downloaders.youtube_downloader import _apply_proxy

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "noplaylist": False,
    }
    _apply_proxy(ydl_opts)
    # cookie 注入（#122 B3）：旧实现写 Netscape 临时文件但把 cookie 塞进
    # `generic` 字段、域名绑死 .example.com，yt-dlp 永远不会带上（参考
    # generic_downloader 同款坑）。改 http_headers.Cookie 直接注入，
    # 对目标站点及其 CDN 分片请求统一生效。
    cookie = ""
    try:
        from app.services.cookie_manager import CookieConfigManager

        cookie = CookieConfigManager().get(platform) or ""
    except Exception:
        cookie = ""
    if cookie:
        ydl_opts["http_headers"] = {"Cookie": cookie}

    from app.downloaders.common import ytdlp_retry

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ytdlp_retry(ydl.extract_info, url, download=False)

    if not info:
        return {"ok": False, "platform": platform, "error": "yt-dlp 未返回信息"}

    if info.get("_type") == "playlist":
        raw_entries = [e for e in (info.get("entries") or []) if e]
        total = len(raw_entries)
        truncated = total > _MAX_ENTRIES
        entries = []
        for i, e in enumerate(raw_entries[:_MAX_ENTRIES], start=1):
            vid = e.get("id")
            page_url = (
                e.get("webpage_url")
                or e.get("url")
                or (_youtube_watch(vid) if platform == "youtube" and vid else None)
            )
            if page_url and not str(page_url).startswith("http"):
                # extract_flat 有时只给 id
                page_url = _youtube_watch(vid) if platform == "youtube" and vid else page_url
            if not page_url or not str(page_url).startswith("http"):
                # 坏条目以成功形状返回会让 Agent 拿无效 URL 去下载阶段才失败——跳过并留痕
                logger.warning(
                    f"播放列表条目 {i}（title={e.get('title')!r}）无可用 http URL，已跳过"
                )
                continue
            entries.append(
                {
                    "p": i,
                    "title": e.get("title") or "",
                    "duration": e.get("duration"),
                    "url": page_url,
                    "video_id": vid,
                }
            )
        if not entries:
            return {"ok": False, "platform": platform, "error": "播放列表无可用条目（URL 均无效）"}
        return {
            "ok": True,
            "platform": platform,
            "kind": "multi" if total > 1 else "single",
            "title": info.get("title") or "",
            "video_id": info.get("id"),
            "current_p": _current_playlist_index(url),
            "total": total,
            "truncated": truncated,
            "entries": entries,
        }

    vid = info.get("id")
    page_url = info.get("webpage_url") or url
    return {
        "ok": True,
        "platform": platform,
        "kind": "single",
        "title": info.get("title") or "",
        "video_id": vid,
        "total": 1,
        "truncated": False,
        "entries": [
            {
                "p": 1,
                "title": info.get("title") or "",
                "duration": info.get("duration"),
                "url": page_url,
                "video_id": vid,
            }
        ],
    }


def _youtube_watch(vid: str) -> str:
    return f"https://www.youtube.com/watch?v={vid}"


def _current_playlist_index(url: str) -> Optional[int]:
    try:
        q = parse_qs(urlparse(url).query)
        if "index" in q:
            return int(q["index"][0])
    except Exception:
        return None
    return None
