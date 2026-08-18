"""供应商连通性检测 —— setup 向导 / MCP 共用的 probe 逻辑（唯一来源）。

probe_models 一次请求同时验证 api_key + base_url 并拿到模型列表
（OpenAI 兼容的 GET /v1/models）；probe_chat 用最小对话请求做兜底
（部分中转站 / 自建网关不实现 /v1/models）。

空 key 归一化为占位符：Ollama 这类无 key 供应商也应能探测，
401 / 400 本身就是有效的检测结果。生成流程（build_openai_client 的严格
空 key 校验）不受影响。
"""
from typing import Optional

from app.utils.openai_client import build_openai_client


def _normalize_key(api_key: Optional[str]) -> str:
    return (api_key or "").strip() or "not-needed"


def probe_models(
    api_key: Optional[str],
    base_url: Optional[str],
    *,
    name: str = "",
    timeout: float = 15.0,
) -> dict:
    """GET /v1/models 探测：验证 key/base_url 并列出可用模型。

    返回 {"ok": bool, "models": list[str], "error": str | None}。
    """
    try:
        client = build_openai_client(
            api_key=_normalize_key(api_key),
            base_url=base_url,
            key_label=f"{name} 的 API Key" if name else "API Key",
            timeout=timeout,
        )
        models = [m.id for m in client.models.list().data]
        return {"ok": True, "models": models, "error": None}
    except Exception as e:
        return {"ok": False, "models": [], "error": str(e)}


def probe_chat(
    api_key: Optional[str],
    base_url: Optional[str],
    model: str,
    *,
    timeout: float = 15.0,
) -> dict:
    """最小 chat 请求探测（/v1/models 不可用时的兜底）。

    与 app/gpt 的 OpenAICompatibleProvider 同款请求（其 test_connection 已删为
    死代码 #134，本函数是唯一 probe 路径），但返回错误文本便于展示。返回 {"ok": bool, "error": str | None}。
    """
    try:
        client = build_openai_client(
            api_key=_normalize_key(api_key),
            base_url=base_url,
            key_label="模型供应商的 API Key",
            timeout=timeout,
        )
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
            temperature=0,
        )
        return {"ok": True, "error": None}
    except Exception as e:
        return {"ok": False, "error": str(e)}
