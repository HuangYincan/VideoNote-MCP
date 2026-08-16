"""解析视频链接：单集 / B 站分 P / YouTube（及 yt-dlp）播放列表。

只做元信息，不下载。返回每集可直接喂给 generate_note / prepare_note_material 的 url。
Agent 按单视频流程处理；本模块不批量提交。
"""
from __future__ import annotations

import logging
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from app.services.pipeline import detect_platform

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

    try:
        if plat == "bilibili":
            return _inspect_bilibili(raw)
        if plat == "local":
            return {
                "ok": True,
                "platform": "local",
                "kind": "single",
                "title": "",
                "video_id": None,
                "total": 1,
                "truncated": False,
                "entries": [{"p": 1, "title": "", "duration": None, "url": raw, "video_id": None}],
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
    cookiefile = None
    try:
        from app.services.cookie_manager import CookieConfigManager

        cookie = CookieConfigManager().get(platform) or ""
        if cookie:
            import os
            import tempfile

            fd, cookiefile = tempfile.mkstemp(suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("# Netscape HTTP Cookie File\n")
                f.write(f".example.com\tTRUE\t/\tTRUE\t0\tgeneric\t{cookie}\n")
            try:
                os.chmod(cookiefile, 0o600)
            except OSError:
                pass
            ydl_opts["cookiefile"] = cookiefile
    except Exception:
        cookiefile = None

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    finally:
        if cookiefile:
            try:
                import os

                os.unlink(cookiefile)
            except OSError:
                pass

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
            entries.append(
                {
                    "p": i,
                    "title": e.get("title") or "",
                    "duration": e.get("duration"),
                    "url": page_url,
                    "video_id": vid,
                }
            )
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
