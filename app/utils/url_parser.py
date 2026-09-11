import functools
import re
from typing import Optional
from urllib.parse import parse_qs, urlsplit

import requests

from app.utils.logger import get_logger
from app.utils.url_safety import assert_public_http_url, host_matches, public_head

logger = get_logger(__name__)

_HTTP_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_DOUYIN_PATH_ID_RE = re.compile(r"/(?:share/)?(?:video|note)/(\d+)", re.IGNORECASE)
_DOUYIN_QUERY_ID_KEYS = ("modal_id", "aweme_id", "item_id", "item_ids")
_URL_TRAILING_CHARS = ".,;:!?)]}'\"\u3002\uff0c\uff01"


def extract_video_id(url: str, platform: str) -> Optional[str]:
    """
    从视频链接中提取视频 ID

    :param url: 视频链接
    :param platform: 平台名（bilibili / youtube / douyin / xiaoyuzhou / xiaohongshu）
    :return: 提取到的视频 ID 或 None
    """
    if platform == "bilibili":
        # 如果是短链接，则解析真实链接
        if "b23.tv" in url:
            resolved_url = resolve_bilibili_short_url(url)
            if resolved_url:
                url = resolved_url

        # 匹配 BV号（如 BV1vc411b7Wa）
        match = re.search(r"BV([0-9A-Za-z]+)", url)
        return f"BV{match.group(1)}" if match else None

    elif platform == "youtube":
        # 匹配 v=xxxxx、youtu.be/xxxxx、shorts/xxxxx 或 embed/xxxxx（#121 B9），ID 长度通常为 11
        match = re.search(r"(?:v=|youtu\.be/|shorts/|embed/)([0-9A-Za-z_-]{11})", url)
        return match.group(1) if match else None

    elif platform == "douyin":
        return extract_douyin_aweme_id(url)

    elif platform == "xiaoyuzhou":
        # https://www.xiaoyuzhoufm.com/episode/{24-hex} ；播客页 /podcast/ 不是单集
        match = re.search(r"/episode/([0-9a-fA-F]{16,})", url)
        return match.group(1) if match else None

    elif platform == "xiaohongshu":
        if "xhslink." in url.lower():
            resolved_url = resolve_xiaohongshu_short_url(url)
            if resolved_url:
                url = resolved_url
        # /explore/{id} / discovery/item/{id} / notes/{id} / user/profile/{uid}/{noteId}
        match = re.search(r"(?:explore|discovery/item|notes)/([0-9a-fA-F]{16,})", url)
        if match:
            return match.group(1)
        match = re.search(r"/user/profile/[0-9a-fA-F]+/([0-9a-fA-F]{16,})", url)
        return match.group(1) if match else None

    return None


def extract_douyin_aweme_id(url: str) -> Optional[str]:
    """从抖音链接（或 App 分享口令）提取稳定 aweme_id。

    同一条视频常见形态归一为同一个 id，供下载器与转写缓存共用：
      - ``https://www.douyin.com/video/{id}``
      - ``https://www.douyin.com/jingxuan?modal_id={id}``（精选/发现/用户页弹层）
      - ``https://www.iesdouyin.com/share/video/{id}``
      - ``https://v.douyin.com/{code}/``（短链：先解真实链接再提 id）

    查询参数（``modal_id`` / ``aweme_id``）优先于路径，避免 feed 页路径
    盖住弹层里真正在看的那条。短链只在本地解析不出 id 时才 HEAD（#125 B1）。
    """
    page = _first_douyin_http_url(url)
    vid = parse_douyin_aweme_id(page)
    if vid:
        return vid
    if _is_douyin_short_url(page):
        resolved = resolve_douyin_short_url(page)
        if resolved:
            return parse_douyin_aweme_id(resolved)
    return None


def parse_douyin_aweme_id(url: str) -> Optional[str]:
    """从已展开的抖音 URL 本地提取 aweme_id（不发网络）。"""
    raw = (url or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw)
    except ValueError:
        return _id_from_douyin_query(raw) or _id_from_douyin_path(raw)
    return (
        _id_from_douyin_query(parts.query)
        or _id_from_douyin_query(parts.fragment)
        or _id_from_douyin_path(parts.path)
        or _id_from_douyin_path(parts.fragment)
    )


def _id_from_douyin_query(query: str) -> Optional[str]:
    if not query:
        return None
    params = parse_qs(query.lstrip("?#"), keep_blank_values=False)
    lowered = {str(k).lower(): v for k, v in params.items()}
    for key in _DOUYIN_QUERY_ID_KEYS:
        vals = lowered.get(key)
        if not vals:
            continue
        vid = (vals[0] or "").split(",")[0].strip()
        if vid.isdigit():
            return vid
    return None


def _id_from_douyin_path(path: str) -> Optional[str]:
    if not path:
        return None
    match = _DOUYIN_PATH_ID_RE.search(path)
    return match.group(1) if match else None


def _first_douyin_http_url(text: str) -> str:
    """分享口令里抽出第一条抖音链接；没有 http URL 则原样返回。"""
    raw = (text or "").strip()
    found = [_strip_url_trailing(u) for u in _HTTP_URL_RE.findall(raw)]
    if not found:
        return raw
    for candidate in found:
        if host_matches(candidate, "douyin.com", "iesdouyin.com"):
            return candidate
    return found[0]


def _strip_url_trailing(url: str) -> str:
    cleaned = url
    while cleaned and cleaned[-1] in _URL_TRAILING_CHARS:
        cleaned = cleaned[:-1]
    return cleaned


def _is_douyin_short_url(url: str) -> bool:
    return host_matches(url, "v.douyin.com")


@functools.lru_cache(maxsize=64)
def resolve_bilibili_short_url(short_url: str) -> Optional[str]:
    """
    解析哔哩哔哩短链接以获取真实视频链接

    :param short_url: Bilibili短链接（如"https://b23.tv/xxxxxx"）
    :return: 真实的视频链接或None
    """
    # SSRF 入口守卫（#133 A1）：调用方只按 "b23.tv" 子串分流，攻击者 URL
    # （如 http://169.254.169.254/?x=b23.tv）会原样走到 HEAD 请求。
    # public_head 逐跳校验（#140）：重定向到内网的 Location 在发出前拦截。
    assert_public_http_url(short_url)
    try:
        response = public_head(short_url, timeout=(5, 10))
        return response.url
    except requests.RequestException as e:
        logger.warning("Error resolving short URL: %s", e)
        return None


@functools.lru_cache(maxsize=64)
def resolve_douyin_short_url(short_url: str) -> Optional[str]:
    """解析抖音短链接（v.douyin.com/xxx，App 分享默认形态）以获取真实视频链接。

    HEAD 重定向到真实分享页/视频页（可能被反爬 403/405）——失败返回 None，
    调用方保持「解析不出 → 不命中缓存」的原有行为（#125 B1）。
    """
    # SSRF 入口守卫（#133 A1）：与 resolve_bilibili_short_url 同口径。
    # public_head 逐跳校验（#140）：重定向到内网的 Location 在发出前拦截。
    assert_public_http_url(short_url)
    try:
        response = public_head(short_url, timeout=(5, 10))
        return response.url
    except requests.RequestException as e:
        logger.warning("Error resolving douyin short URL: %s", e)
        return None


@functools.lru_cache(maxsize=64)
def resolve_xiaohongshu_short_url(short_url: str) -> Optional[str]:
    """解析小红书短链（xhslink.com / xhslink.cn，App 分享默认形态）。

    HEAD 跟随到带 xsec_token 的长链；失败返回 None（调用方保持「解析不出 → 不命中缓存」）。
    """
    assert_public_http_url(short_url)
    try:
        response = public_head(short_url, timeout=(5, 10))
        return response.url
    except requests.RequestException as e:
        logger.warning("Error resolving xiaohongshu short URL: %s", e)
        return None


def extract_bilibili_p_number(url: str) -> Optional[int]:
    """
    从 B 站分 P 视频 URL 中提取 p 参数（分 P 序号）。

    支持格式：
      - https://www.bilibili.com/video/BVxxx/?p=36
      - https://www.bilibili.com/video/BVxxx?p=5
      - https://b23.tv/xxxxx?p=10
      - https://www.bilibili.com/video/BVxxx/pN (尾缀形式)

    :param url: B 站视频链接
    :return: 分 P 序号（从 1 开始），非分 P 视频返回 None
    """
    if "b23.tv" in url:
        url = resolve_bilibili_short_url(url) or url

    # 匹配 ?p=NNN 或 &p=NNN
    match = re.search(r'[?&]p=(\d+)', url)
    if match:
        p = int(match.group(1))
        if p >= 1:
            return p

    # 匹配 /pN 尾缀形式（较少见）
    match = re.search(r'/p(\d+)(?:/?$|\?|&)', url)
    if match:
        p_val = int(match.group(1))
        if p_val >= 1:
            return p_val

    return None
