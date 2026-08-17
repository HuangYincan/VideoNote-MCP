import atexit
from typing import Callable, Dict

from app.downloaders.bilibili_downloader import BilibiliDownloader
from app.downloaders.douyin_downloader import DouyinDownloader
from app.downloaders.generic_downloader import GenericDownloader
from app.downloaders.kuaishou_downloader import KuaiShouDownloader
from app.downloaders.local_downloader import LocalDownloader
from app.downloaders.youtube_downloader import YoutubeDownloader

# 惰性工厂：不要在模块导入期实例化下载器 —— 部分下载器 __init__ 会写 /tmp cookie 文件，
# 而模块级单例的 __del__ 在解释器退出时不触发，会导致 SESSDATA 明文文件泄漏（见 docs/05 #49）。
# 每次 get_downloader() 新建实例：任务结束后实例出作用域被 GC，__del__ 正常清理。
_DOWNLOADER_FACTORY: Dict[str, Callable] = {
    'youtube': YoutubeDownloader,
    'bilibili': BilibiliDownloader,
    # tiktok 走 yt-dlp 通用提取：抖音 API 无法解析 tiktok.com（docs/05 #47）
    'tiktok': GenericDownloader,
    'kuaishou': KuaiShouDownloader,
    'douyin': DouyinDownloader,
    'local': LocalDownloader,
    'generic': GenericDownloader,
}

# 兼容旧引用（只用于「是否支持某平台」的判断，不要直接实例化取下载器）
SUPPORT_PLATFORM_MAP: Dict[str, type] = _DOWNLOADER_FACTORY

_created: list = []
_atexit_registered = False


def get_downloader(platform: str):
    """按平台惰性创建下载器实例（每次新建，保证 __del__ 的 cookie 清理能触发）。"""
    factory = _DOWNLOADER_FACTORY.get(platform)
    if factory is None:
        raise ValueError(f"不支持的平台：{platform}")
    inst = factory()
    _created.append(inst)
    return inst


def _cleanup_created() -> None:
    for inst in _created:
        cleanup = getattr(inst, "_cleanup_cookie_file", None)
        if callable(cleanup):
            try:
                cleanup()
            except Exception:
                pass


def register_atexit_cleanup() -> None:
    """注册解释器退出时的 cookie 文件兜底清理（与 __del__ 双保险）。"""
    global _atexit_registered
    if not _atexit_registered:
        atexit.register(_cleanup_created)
        _atexit_registered = True


register_atexit_cleanup()
