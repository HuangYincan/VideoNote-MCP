"""YoutubeDownloader 的 Cookie 注入（#C2）：cookiesfrombrowser 优先于手动 cookiefile。

不碰真实网络/yt-dlp：mock CookieConfigManager 与 yt_dlp.YoutubeDL。
"""
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.downloaders.youtube_downloader import YoutubeDownloader


class _YdlCtx:
    def __init__(self, info):
        self._info = info

    def extract_info(self, *a, **k):
        return self._info

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_dl(cookie=None, browser=None):
    """构造下载器，cookie/browser 由 mock 的 CookieConfigManager 提供。"""
    with mock.patch(
        "app.downloaders.youtube_downloader.CookieConfigManager"
    ) as m_mgr_cls:
        m_mgr = m_mgr_cls.return_value
        m_mgr.get.return_value = cookie
        m_mgr.get_browser.return_value = browser
        dl = YoutubeDownloader()
    return dl, m_mgr


def _capture_opts(dl, url="https://www.youtube.com/watch?v=xxxxxx"):
    captured = {}

    def _fake_ydl(opts):
        captured.update(opts)
        return _YdlCtx({"id": "x", "title": "t", "duration": 10, "ext": "m4a"})

    with mock.patch("yt_dlp.YoutubeDL", side_effect=_fake_ydl):
        dl.download(url, output_dir=tempfile.mkdtemp())
    return captured


class TestCookieInjection:
    """browser 优先、手动 cookie 次之、无配置时不注入。"""

    def test_browser_config_uses_cookiesfrombrowser(self):
        dl, _ = _make_dl(cookie=None, browser="safari")
        opts = _capture_opts(dl)
        assert opts.get("cookiesfrombrowser") == ("safari",)
        assert "cookiefile" not in opts

    def test_manual_cookie_uses_cookiefile(self):
        dl, _ = _make_dl(cookie="SID=abc", browser=None)
        opts = _capture_opts(dl)
        assert "cookiesfrombrowser" not in opts
        assert opts.get("cookiefile") is not None

    def test_browser_wins_over_manual_cookie(self):
        dl, _ = _make_dl(cookie="SID=abc", browser="chrome")
        opts = _capture_opts(dl)
        assert opts.get("cookiesfrombrowser") == ("chrome",)
        assert "cookiefile" not in opts

    def test_no_cookie_no_injection(self):
        dl, _ = _make_dl(cookie=None, browser=None)
        opts = _capture_opts(dl)
        assert "cookiesfrombrowser" not in opts
        assert "cookiefile" not in opts
