"""Local path normalization and access policy, independent of MCP/CLI bootstrapping."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse


def coerce_local_path(value: str) -> Path:
    """Expand home paths and decode file URIs (including Windows drive prefixes)."""
    text = str(value or "").strip()
    if text.startswith("file://"):
        text = unquote(urlparse(text).path or "")
        if os.name == "nt" and len(text) >= 3 and text[0] == "/" and text[2] == ":":
            text = text[1:]
    return Path(text).expanduser()


@dataclass(frozen=True)
class LocalPathPolicy:
    """Explicit data-root policy; resolving a symlink cannot grant access outside it.

    Entry points inject their configured root. Standalone app callers may use
    from_environment after normal environment setup; neither path boots a server.
    This is a local trusted-directory check, not a cross-process TOCTOU defence.
    """

    data_dir: Path
    allow_external: bool = False

    @classmethod
    def from_environment(cls) -> LocalPathPolicy:
        from app.utils.path_helper import get_data_dir

        allowed = os.getenv("VIDEONOTE_ALLOW_EXTERNAL_PATHS", "").strip().lower()
        return cls(Path(get_data_dir()), allowed in ("1", "true", "yes", "on"))

    def contains(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(self.data_dir.resolve())
        except (OSError, RuntimeError):  # unreadable path / symlink loop: fail closed
            return False

    def guard(self, path: Path, what: str) -> None:
        if self.allow_external or self.contains(path):
            return
        raise ValueError(
            f"{what} 必须在数据目录内（数据目录: {self.data_dir}；收到: {path}）。"
            "为防止本地文件被误读/误写，默认只允许数据目录内的路径；"
            "确实需要时可设置 VIDEONOTE_ALLOW_EXTERNAL_PATHS=1"
            "（或插件设置 allow_external_paths）后重启 MCP"
        )
