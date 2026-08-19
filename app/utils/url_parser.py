import functools
import re
from typing import Optional

import requests

from app.utils.logger import get_logger

logger = get_logger(__name__)


def extract_video_id(url: str, platform: str) -> Optional[str]:
    """
    从视频链接中提取视频 ID

    :param url: 视频链接
    :param platform: 平台名（bilibili / youtube / douyin）
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
        # v.douyin.com 短链（App 分享默认形态）先解真实链接——不解则 /video/ 匹配
        # 不到 → 缓存身份 douyin:None 永不命中，同一视频每次都重下重转写（#125 B1）
        if "v.douyin.com" in url:
            resolved_url = resolve_douyin_short_url(url)
            if resolved_url:
                url = resolved_url
        # 匹配 douyin.com/video/1234567890123456789（含 share/video/ 形态）
        match = re.search(r"/video/(\d+)", url)
        return match.group(1) if match else None

    return None


@functools.lru_cache(maxsize=64)
def resolve_bilibili_short_url(short_url: str) -> Optional[str]:
    """
    解析哔哩哔哩短链接以获取真实视频链接

    :param short_url: Bilibili短链接（如"https://b23.tv/xxxxxx"）
    :return: 真实的视频链接或None
    """
    # SSRF 入口守卫（#133 A1）：调用方只按 "b23.tv" 子串分流，攻击者 URL
    # （如 http://169.254.169.254/?x=b23.tv）会原样走到 requests.head
    from app.utils.url_safety import assert_public_http_url

    assert_public_http_url(short_url)
    try:
        response = requests.head(short_url, allow_redirects=True, timeout=(5, 10))
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
    # SSRF 入口守卫（#133 A1）：与 resolve_bilibili_short_url 同口径
    from app.utils.url_safety import assert_public_http_url

    assert_public_http_url(short_url)
    try:
        response = requests.head(short_url, allow_redirects=True, timeout=(5, 10))
        return response.url
    except requests.RequestException as e:
        logger.warning("Error resolving douyin short URL: %s", e)
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
