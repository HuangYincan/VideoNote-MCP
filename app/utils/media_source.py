"""Platform detection shared by metadata inspection and the processing pipeline."""
from pathlib import Path
from typing import Optional

_PLATFORM_HINTS = [
    ("bilibili", ("bilibili.com", "b23.tv")),
    ("youtube", ("youtube.com", "youtu.be")),
    ("douyin", ("douyin.com", "iesdouyin.com")),
    ("tiktok", ("tiktok.com",)),
    ("kuaishou", ("kuaishou.com", "gifshow.com")),
    ("xiaoyuzhou", ("xiaoyuzhoufm.com", "xiaoyuzhou.fm")),
    ("xiaohongshu", ("xiaohongshu.com", "xhslink.com", "xhslink.cn", "rednote.com")),
]


def _match_platform_host(u: str) -> Optional[str]:
    """基于 host 精确匹配平台（含子域名/端口/无协议 URL）。

    旧的子串匹配（`"bilibili.com" in u`）会把 evilbilibili.com、bilibili.com.evil.com
    误判成 bilibili；这里按 host == 目标 或 host 以 `.目标` 结尾判断。
    """
    from urllib.parse import urlparse

    s = u if "://" in u else f"http://{u}"
    try:
        host = urlparse(s).netloc.lower().split(":")[0].rstrip(".")
    except Exception:
        return None
    if not host:
        return None
    for platform, needles in _PLATFORM_HINTS:
        if any(host == n or host.endswith("." + n) for n in needles):
            return platform
    return None


def detect_platform(url: str) -> str:
    """从 URL / 本地路径识别平台（无任务/引擎初始化）。

    未知 URL 返回 `"generic"`——走 yt-dlp 通用提取器（覆盖 1800+ 站点，含 GenericIE 兜底）。
    只有 yt-dlp 也解析失败时，调用方才用 handoff_result 把任务交给 Agent 接手。
    空 url 仍 raise ValueError。
    """
    u = (url or "").strip().lower()
    if not u:
        raise ValueError("url 为空")
    if u.startswith(("file:", "/", "./", "../", "~/")) or Path(u).expanduser().exists():
        return "local"
    return _match_platform_host(u) or "generic"

