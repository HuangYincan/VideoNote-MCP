"""URL 安全校验 —— SSRF 防护与日志脱敏（docs/05 第 16 轮扫描 A1 / A4 / A5；#140 逐跳收尾）。

SSRF 背景：yt-dlp 通用提取器（GenericIE）接受任意 URL，被恶意/被注入的 agent
可借 `generate_note` / `inspect_video` 访问内网服务、环回地址、
云厂商元数据端点（169.254.169.254）等。本模块在把 URL 交给下载器前校验
scheme 与目标主机，拦截指向私网/环回/链路本地/保留地址的请求。

设计约束：
- 只放行 http/https（对 generic 的任意 URL）；本地文件路径由调用方先行分流（local）。
- 字面 IP：直接判 `ipaddress` 的 `is_global`（覆盖 127/8、10/8、172.16/12、
  192.168/16、169.254/16、0.0.0.0、::1、fc00::/7、fe80::/10 等）。
- 域名：解析后任一地址非公网即拦截（split-horizon DNS 的保守选择）。
- DNS 解析失败：放行交由 yt-dlp 正常报错，不因解析器抖动误杀合法链接。

覆盖层次（#140：#133 A1 只校验入口 URL 的缺口收尾）：
- `PublicOnlySession` / `public_get` / `public_head` / `public_post`：requests 系出站请求的
  **逐跳**校验——重写 `Session.send` 覆盖初始 URL 与每次重定向跳点
  （requests 的 `resolve_redirects` 经 `self.send` 发出下一跳），
  短链解析（url_parser）、快手/抖音视频页跟随、B 站 API 返回的资源 URL 均走这里。
- 平台 API 返回的直连资源 URL（抖音 url_list / 快手 photoUrl）无法用入口 URL
  覆盖——由 `downloaders/common.stream_download` 在下载前统一校验。
- 已知边界：yt-dlp 内部重定向到内网的场景不在防护内（完整方案需给 yt-dlp
  挂自定义下载器，或网络侧对 Docker/remote MCP 强制 egress 白名单/代理）。
"""
from __future__ import annotations

import functools
import ipaddress
import logging
import socket
from typing import Optional
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("http", "https")


def sanitize_url(url: Optional[str]) -> str:
    """日志脱敏：剥离 userinfo 与 query（签名 token / 凭据），只留 scheme://host[:port]/path。"""
    if not url:
        return ""
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.scheme:
        # 非 URL（防御输入）：本无凭据可剥离，原样返回便于日志排查
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}{parts.path or '/'}"


def hostname_matches(host: str, *suffixes: str) -> bool:
    """host 是否等于或为 suffix 的子域（精确后缀，非子串）。

    ``xiaohongshu.com`` / ``edith.xiaohongshu.com`` 命中；
    ``evilxiaohongshu.com`` / ``xiaohongshu.com.evil.com`` 不命中。
    """
    h = (host or "").lower().lstrip(".").rstrip(".")
    if not h:
        return False
    return any(h == s.lower() or h.endswith("." + s.lower()) for s in suffixes)


def host_matches(url: str, *suffixes: str) -> bool:
    """URL 的 hostname 是否匹配任一官方后缀（#144 A1/A3）。"""
    if not url:
        return False
    raw = url.strip()
    try:
        parts = urlsplit(raw if "://" in raw else f"http://{raw}")
    except ValueError:
        return False
    return hostname_matches(parts.hostname or "", *suffixes)


# fake-ip 代理（Clash/Surge/Stash 等 macOS 常用）把 DNS 解析结果返回保留段，
# 流量由代理转发到公网——is_global 判 False 会误杀所有 URL（2026-08-19
# 用户实测：双栈 fake-ip 下 generate_note/inspect_video 全部被 SSRF 防护拦截）。
#   IPv4：198.18.0.0/15（RFC 2544 benchmarking 段，公网不可路由，真实内网不使用）
#   IPv6：fdfe::/16（Clash/Surge fake-ip v6 默认段，ULA fd00::/8 随机段；真实内网
#        几乎不用 fdfe 前缀，其余 fc00::/7 ULA 仍拦）
# 放行这两个段不削弱防护目标：内网/环回/元数据端点（169.254.169.254）仍全拦。
_FAKE_IP_PROXY_NETS = (
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fdfe::/16"),
)


def _ip_is_global(ip: ipaddress._BaseAddress) -> bool:
    """IPv4/IPv6 统一判公网。is_global 聚合了 private/loopback/link_local/reserved/unspecified/multicast。"""
    if any(ip in net for net in _FAKE_IP_PROXY_NETS):
        return True
    try:
        return bool(ip.is_global)
    except Exception:  # noqa: BLE001 —— 个别保留区间 is_global 可能抛错，保守拦截
        return False


@functools.lru_cache(maxsize=512)
def _host_is_public(host: str) -> bool:
    """域名/字面 IP → 是否公网。按 host 缓存，避免批处理内对同一域名重复解析。"""
    # 字面 IP（IPv4 / IPv6）
    try:
        ip = ipaddress.ip_address(host)
        return _ip_is_global(ip)
    except ValueError:
        pass
    # 域名 → 解析后任一非公网即拦截
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        # DNS 解析失败（可能是不存在的域名/断网）：放行，让 yt-dlp 报出真实错误
        logger.warning("URL 主机解析失败（交由下载器报错）: %s", host)
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not _ip_is_global(ip):
            logger.warning("拦截指向非公网 IP 的 URL 主机: %s -> %s", host, info[4][0])
            return False
    return True


def is_public_http_url(url: str) -> bool:
    """URL 是否允许下载：http/https + 目标主机公网。非 http(s) scheme / 私网一律 False。"""
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        return False
    host = parts.hostname
    if not host:
        return False
    return _host_is_public(host)


def assert_public_http_url(url: str) -> None:
    """下载入口校验：不安全 URL 抛 ValueError（带清晰原因，供上层转成 ok:false）。"""
    if not is_public_http_url(url):
        raise ValueError(
            f"URL 被 SSRF 防护拦截（仅放行 http/https 且目标须为公网地址，不支持内网/环回/元数据端点）: {sanitize_url(url)}"
        )


class PublicOnlySession(requests.Session):
    """逐跳 SSRF 校验的 requests.Session（#140：#133 A1 入口校验+首跳 DNS 的缺口收尾）。

    `Session.send` 在发出每个请求前校验目标主机公网——requests 的
    `resolve_redirects` 内部经 `self.send` 发出下一跳（含相对跳转拼好的绝对
    URL），重写 `send` 即覆盖**初始 URL + 全部 Location 跳点**；拦截抛
    ValueError（与 `assert_public_http_url` 同消息）。
    """

    def send(self, request, **kwargs):  # type: ignore[override]
        assert_public_http_url(request.url)
        return super().send(request, **kwargs)


def public_get(url: str, **kwargs) -> "requests.Response":
    """`requests.get` 的逐跳 SSRF 校验版：短链跟随 / API 返回资源 URL 统一入口。

    与 requests.get 同语义（allow_redirects=True），区别是每一跳都先过公网校验。
    """
    with PublicOnlySession() as session:
        return session.request("GET", url, **kwargs)


def public_head(url: str, **kwargs) -> "requests.Response":
    """`requests.head` 的逐跳 SSRF 校验版（短链解析等 HEAD 跟随场景）。"""
    with PublicOnlySession() as session:
        return session.request("HEAD", url, **kwargs)


def public_post(url: str, **kwargs) -> "requests.Response":
    """`requests.post` 的逐跳 SSRF 校验版（平台官方 API 出站）。"""
    with PublicOnlySession() as session:
        return session.request("POST", url, **kwargs)
