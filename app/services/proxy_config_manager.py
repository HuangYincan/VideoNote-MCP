import os
from pathlib import Path
from typing import Any, Dict, Optional

from app.utils.json_store import read_json, write_json_atomic


class ProxyConfigManager:
    """全局代理配置，存 JSON 文件，支持前端动态修改。

    作用范围：LLM API + 转写 API（Groq 等）+ yt-dlp 视频下载。
    优先级：配置文件里 enabled=true 的 url > 环境变量 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。
    这样桌面端/web 用户在设置页填，docker/服务器部署用环境变量兜底。
    """

    def __init__(self, filepath: Optional[str] = None):
        # 默认落在 VIDEONOTE_CONFIG_DIR（MCP/CLI 由 setup_environment 设置），避免 CWD 相对路径
        # 在笔记目录等任意 CWD 里冒出空的 config/ 文件夹
        if filepath is None:
            filepath = str(Path(os.environ.get("VIDEONOTE_CONFIG_DIR", "config")) / "proxy.json")
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        return read_json(self.path)

    def _write(self, data: Dict[str, Any]):
        write_json_atomic(self.path, data)

    def get_config(self) -> Dict[str, Any]:
        data = self._read()
        return {
            "enabled": bool(data.get("enabled", False)),
            "url": data.get("url", "") or "",
        }

    def update_config(self, enabled: bool, url: Optional[str] = None) -> Dict[str, Any]:
        data = self._read()
        data["enabled"] = bool(enabled)
        if url is not None:
            data["url"] = url.strip()
        self._write(data)
        return self.get_config()

    def get_proxy_url(self) -> Optional[str]:
        """返回当前生效的代理 URL；没有则 None。

        - 配置文件 enabled=true 且 url 非空 → 用配置的 url
        - 否则回退到环境变量（标准的 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY，大小写都认）
        """
        cfg = self.get_config()
        if cfg["enabled"] and cfg["url"]:
            return cfg["url"]
        for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            val = os.environ.get(key)
            if val:
                return val
        return None
