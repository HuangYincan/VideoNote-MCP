"""SSRF 防护与日志脱敏单元测试（docs/05 第 16 轮 A1 / A4 / A5）。

conftest 的 `_mock_public_dns` 会统一把 getaddrinfo 桩成公网地址；本文件的
域名相关用例通过显式 patch 覆盖「解析到私网 / 解析失败」两条分支。
"""
import socket
from unittest import mock

import pytest
import requests

from app.utils.url_safety import (
    assert_public_http_url,
    is_public_http_url,
    sanitize_error_text,
    sanitize_url,
)


class TestIsPublicHttpUrlLiteralIp:
    """字面 IP 走 ipaddress 直接判定，无需 DNS。"""

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/x",
            "http://127.0.0.1:8080/video",
            "http://10.0.0.1/x",
            "http://172.16.0.1/x",
            "http://172.31.255.255/x",
            "http://192.168.1.1/x",
            "http://169.254.169.254/latest/meta-data/",
            "http://0.0.0.0/x",
            "https://[::1]/x",
            "https://[fc00::1]/x",
            "https://[fe80::1]/x",
        ],
    )
    def test_private_ip_blocked(self, url):
        assert is_public_http_url(url) is False

    @pytest.mark.parametrize(
        "url",
        ["http://8.8.8.8/x", "https://1.1.1.1/x", "http://114.114.114.114/v"],
    )
    def test_public_ip_allowed(self, url):
        assert is_public_http_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            "http://198.18.0.1/x",      # fake-ip 代理段起点
            "http://198.18.5.33/x",     # fake-ip（Clash 默认）
            "http://198.19.255.255/x",  # fake-ip 段终点
            "https://[fdfe::1]/x",      # fake-ip v6 段（Clash/Surge 默认 fdfe::/16）
            "https://[fdfe:dcba:9876::68]/x",  # 实测解析出的 IPv6 fake-ip
        ],
    )
    def test_fake_ip_proxy_range_allowed(self, url):
        """fake-ip 代理段（198.18.0.0/15 + fdfe::/16）：Clash/Surge 的 DNS 返回段，
        流量经代理转发公网，is_global=False 会误杀（2026-08-19 实测）。"""
        assert is_public_http_url(url) is True


class TestIsPublicHttpUrlScheme:
    @pytest.mark.parametrize(
        "url",
        [
            "ftp://example.com/x",
            "file:///etc/passwd",
            "gopher://example.com/x",
            "data:text/plain,hi",
            "",
            "not a url",
            "http://",
        ],
    )
    def test_bad_scheme_or_malformed_blocked(self, url):
        assert is_public_http_url(url) is False


class TestIsPublicHttpUrlHostname:
    """域名依赖 DNS：显式 patch 覆盖解析分支。"""

    def test_host_resolves_to_private_blocked(self):
        def _private(*_a, **_k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

        with mock.patch("socket.getaddrinfo", side_effect=_private):
            assert is_public_http_url("http://internal.example.com/x") is False

    def test_host_resolves_to_fake_ip_allowed(self):
        """域名解析到 fake-ip 段（IPv6 fdfe::/16 + IPv4 198.18.0.0/15 双栈，
        模拟 Clash/Surge 真实 getaddrinfo 顺序）不拦截——代理转发公网。"""

        def _fake_ip(*_a, **_k):
            return [
                (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fdfe:dcba:9876::68", 0, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.102", 0)),
            ]

        with mock.patch("socket.getaddrinfo", side_effect=_fake_ip):
            assert is_public_http_url("http://example.com/v") is True

    def test_host_resolves_to_non_fake_ula_blocked(self):
        """fc00::/7 的其他 ULA 段（非 fdfe::/16）仍拦截——fake-ip 放行不扩大化。"""

        def _ula(*_a, **_k):
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fc00::1", 0, 0, 0))]

        with mock.patch("socket.getaddrinfo", side_effect=_ula):
            assert is_public_http_url("http://internal.example.com/v") is False

    def test_host_resolves_to_public_allowed(self):
        assert is_public_http_url("http://example.com/v") is True  # conftest 桩成 8.8.8.8

    def test_dns_failure_allow_for_ytdlp_error(self):
        def _fail(*_a, **_k):
            raise socket.gaierror(-2, "Name or service not known")

        with mock.patch("socket.getaddrinfo", side_effect=_fail):
            # 解析失败不误杀：让 yt-dlp 报出真实错误（域名不存在/断网）
            assert is_public_http_url("http://no-such-host.invalid/v") is True


class TestAssertPublicHttpUrl:
    def test_blocked_raises_valueerror(self):
        with pytest.raises(ValueError) as ei:
            assert_public_http_url("http://169.254.169.254/latest/meta-data/")
        assert "SSRF" in str(ei.value)

    def test_allowed_no_raise(self):
        assert_public_http_url("http://8.8.8.8/v")  # 不抛即通过


class TestSanitizeUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("http://user:pass@example.com:8080/video?token=abc&x=1", "http://example.com:8080/video"),
            ("https://example.com/v?x=1", "https://example.com/v"),
            ("http://example.com", "http://example.com/"),
            ("", ""),
            (None, ""),
            ("not a url", "not a url"),
        ],
    )
    def test_sanitize(self, raw, expected):
        assert sanitize_url(raw) == expected


class TestSanitizeErrorText:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (
                "GET https://cdn.example/video.mp4?token=secret&sig=abc failed",
                "GET https://cdn.example/video.mp4 failed",
            ),
            (
                "headers={'Authorization': 'Bearer secret', 'Cookie': 'SESSDATA=secret'}",
                "headers={'Authorization': '<redacted>', 'Cookie': '<redacted>'}",
            ),
            ("request token=secret signature=abc", "request token=<redacted> signature=<redacted>"),
            ("x-amz-signature=abc", "x-amz-signature=<redacted>"),
        ],
    )
    def test_redacts_urls_and_credential_assignments(self, raw, expected):
        assert sanitize_error_text(raw) == expected

    def test_preserves_non_sensitive_diagnostic_context(self):
        assert sanitize_error_text("HTTP 503 from cdn.example/path") == "HTTP 503 from cdn.example/path"


    """#133 A1：短链解析器对任意 URL 直连 requests.head——调用方只按
    "b23.tv" / "v.douyin.com" 子串分流，攻击者 URL 会原样进来，须先过 SSRF 守卫。
    """

    def test_bilibili_resolver_blocks_private_ip(self):
        from app.utils.url_parser import resolve_bilibili_short_url

        with pytest.raises(ValueError, match="SSRF"):
            resolve_bilibili_short_url("http://169.254.169.254/?x=b23.tv")

    def test_bilibili_resolver_blocks_loopback(self):
        from app.utils.url_parser import resolve_bilibili_short_url

        with pytest.raises(ValueError, match="SSRF"):
            resolve_bilibili_short_url("http://127.0.0.1/xxx")

    def test_douyin_resolver_blocks_private_ip(self):
        from app.utils.url_parser import resolve_douyin_short_url

        with pytest.raises(ValueError, match="SSRF"):
            resolve_douyin_short_url("http://10.0.0.1/v.douyin.com/abc")

    def test_resolver_public_url_still_resolves(self):
        # 合法短链（conftest DNS 桩为公网）不被误拦；HEAD 经 adapter（mock 掉，
        # 不碰真实网络）返回最终 URL
        from app.utils.url_parser import resolve_bilibili_short_url

        with mock.patch("requests.adapters.HTTPAdapter.send", side_effect=_http_ok("https://www.bilibili.com/video/BV1xx")) as m_send:
            out = resolve_bilibili_short_url("https://b23.tv/xxxxxx")
        assert out == "https://www.bilibili.com/video/BV1xx"
        m_send.assert_called_once()


def _http_ok(url: str, status: int = 200, location: str = ""):
    """构造 HTTPAdapter.send 桩：返回指定状态/跳转的 Response（逐跳校验测试用）。

    不碰真实网络：adapter 是 requests 的出站最后一层，mock 它即挡住所有 I/O；
    Response.url 决定 get_redirect_target 的相对跳转基准，必须与请求 URL 一致。
    mock.patch 类方法时 side_effect 不绑定 self——签名是 (request, **kwargs)。
    """

    def _send(request, **kwargs):
        resp = requests.Response()
        resp.status_code = status
        resp._content = b""
        resp.url = url
        resp.request = request
        resp.raw = None
        resp.headers = requests.structures.CaseInsensitiveDict({"Location": location} if location else {})
        return resp

    return _send


class TestPublicOnlySessionRedirect:
    """#140：逐跳校验 —— 重定向目标在发出前校验。

    requests 的 resolve_redirects 经 `self.send` 发下一跳（含相对跳转拼好的绝对
    URL），重写 Session.send 即覆盖初始 URL + 全部 Location 跳点。
    """

    def test_session_send_blocks_private_without_network(self):
        from app.utils.url_safety import PublicOnlySession

        req = requests.Request("GET", "http://169.254.169.254/latest/meta-data/").prepare()
        with mock.patch("requests.adapters.HTTPAdapter.send", side_effect=AssertionError("不应发出请求")):
            with pytest.raises(ValueError, match="SSRF"):
                PublicOnlySession().send(req)

    def test_redirect_to_private_blocked_before_fetch(self):
        from app.utils.url_safety import public_get

        calls = []

        def _send(request, **kwargs):
            calls.append(request.url)
            return _http_ok(request.url, status=302, location="http://169.254.169.254/latest/meta-data/")(request, **kwargs)

        with mock.patch("requests.adapters.HTTPAdapter.send", side_effect=_send):
            with pytest.raises(ValueError, match="SSRF"):
                public_get("http://public.example.com/a.mp3")
        # 第一跳结束后第二跳被拦：跳点 URL 从未发出
        assert calls == ["http://public.example.com/a.mp3"]

    def test_public_redirect_chain_followed(self):
        from app.utils.url_safety import public_get

        calls = []

        def _send(request, **kwargs):
            calls.append(request.url)
            if len(calls) == 1:
                return _http_ok(request.url, status=302, location="http://cdn.example.com/b.mp3")(request, **kwargs)
            return _http_ok(request.url)(request, **kwargs)

        with mock.patch("requests.adapters.HTTPAdapter.send", side_effect=_send):
            resp = public_get("http://example.com/a.mp3")
        assert resp.status_code == 200
        assert len(calls) == 2
        assert calls[1] == "http://cdn.example.com/b.mp3"

    def test_public_post_blocks_private_without_network(self):
        from app.utils.url_safety import public_post

        with mock.patch("requests.adapters.HTTPAdapter.send", side_effect=AssertionError("不应发出请求")):
            with pytest.raises(ValueError, match="SSRF"):
                public_post("http://169.254.169.254/latest/meta-data/", json={"eid": "x"})
