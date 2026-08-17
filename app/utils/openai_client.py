"""统一构造 OpenAI 兼容客户端：注入全局代理 + 校验 api_key。

为什么要这一层：
  - 代理：openai SDK 默认只认进程级 HTTP_PROXY 环境变量，桌面端用户在 UI 里
    填的代理需要显式塞进 httpx.Client 才生效。
  - api_key 校验：空 key 会让 httpx 拼出非法 header `Bearer `，抛出
    `httpx.LocalProtocolError: Illegal header value b'Bearer '` 这种天书报错。
    在入口挡掉，给用户「xxx 的 API Key 未配置」这种能看懂的提示。
"""
from typing import Optional

import httpx
from openai import OpenAI

from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.logger import get_logger

logger = get_logger(__name__)

# 默认超时：连接 10s 快速失败（DNS/拒绝连接不拖 SDK 600s 默认），读写 300s 兜底死读。
# LLM 调用（gpt_factory → OpenAICompatibleProvider）与 groq 转写都吃这个默认——此前
# timeout=None → openai SDK 默认 600s（代理路径另写 600s），上游挂死时任务卡 10 分钟
# ×重试次数（占死 worker 槽）。与 #58 外部调用全带超时同口径；显式传参覆盖（如
# 连通性测试 15s）。
DEFAULT_TIMEOUT = httpx.Timeout(300.0, connect=10.0)


def build_openai_client(
    api_key: Optional[str],
    base_url: Optional[str],
    *,
    key_label: str = "API Key",
    timeout: Optional[httpx.Timeout] = None,
) -> OpenAI:
    """构造 OpenAI 客户端。api_key 为空直接抛清晰错误；代理已配置则注入。

    key_label 用于错误提示，例如 "Groq 的 API Key" / "OpenAI 供应商的 API Key"。
    timeout 缺省用 DEFAULT_TIMEOUT（连接 10s / 读写 300s）。
    """
    if not api_key or not str(api_key).strip():
        raise ValueError(f"{key_label} 未配置，请先在「设置」里填写后再使用")

    timeout = timeout or DEFAULT_TIMEOUT
    kwargs = {"api_key": str(api_key).strip(), "base_url": base_url, "timeout": timeout}

    proxy_url = ProxyConfigManager().get_proxy_url()
    if not proxy_url:
        return OpenAI(**kwargs)

    import weakref

    http_client = httpx.Client(proxy=proxy_url, timeout=timeout)
    kwargs["http_client"] = http_client
    client = OpenAI(**kwargs)
    # 实例被 GC 时关掉 http_client，避免连接/fd 跨任务累积（docs/05 #74）
    weakref.finalize(client, http_client.close)
    logger.info(f"OpenAI 客户端走代理: {proxy_url}")

    return client
