"""SSRF 防护与日志脱敏单元测试（docs/05 第 16 轮 A1 / A4 / A5）。

conftest 的 `_mock_public_dns` 会统一把 getaddrinfo 桩成公网地址；本文件的
域名相关用例通过显式 patch 覆盖「解析到私网 / 解析失败」两条分支。
"""
import socket
from unittest import mock

import pytest

from app.utils.url_safety import (
    assert_public_http_url,
    is_public_http_url,
    sanitize_url,
)


class IsPublicHttpUrlLiteralIpTest:
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


class IsPublicHttpUrlSchemeTest:
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


class IsPublicHttpUrlHostnameTest:
    """域名依赖 DNS：显式 patch 覆盖解析分支。"""

    def test_host_resolves_to_private_blocked(self):
        def _private(*_a, **_k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0))]

        with mock.patch("socket.getaddrinfo", side_effect=_private):
            assert is_public_http_url("http://internal.example.com/x") is False

    def test_host_resolves_to_public_allowed(self):
        assert is_public_http_url("http://example.com/v") is True  # conftest 桩成 8.8.8.8

    def test_dns_failure_allow_for_ytdlp_error(self):
        def _fail(*_a, **_k):
            raise socket.gaierror(-2, "Name or service not known")

        with mock.patch("socket.getaddrinfo", side_effect=_fail):
            # 解析失败不误杀：让 yt-dlp 报出真实错误（域名不存在/断网）
            assert is_public_http_url("http://no-such-host.invalid/v") is True


class AssertPublicHttpUrlTest:
    def test_blocked_raises_valueerror(self):
        with pytest.raises(ValueError) as ei:
            assert_public_http_url("http://169.254.169.254/latest/meta-data/")
        assert "SSRF" in str(ei.value)

    def test_allowed_no_raise(self):
        assert_public_http_url("http://8.8.8.8/v")  # 不抛即通过


class SanitizeUrlTest:
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
