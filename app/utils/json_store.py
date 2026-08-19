"""JSON 配置文件的安全读写：损坏不静默当空 + 原子写（app 层配置管理器共用）。

三个配置管理器（transcriber / cookie / proxy）共享同一套缺陷（docs/05 #106 扫描）：
- `_read` 对损坏 JSON 静默返回 `{}`——转写引擎悄悄回退 fast-whisper、cookie 悄悄消失，
  全程无日志；cookie `set()` 读损坏文件得 `{}` 再写回会把其它平台 cookie 永久抹掉。
- `_write` 非原子——磁盘满/进程中断留下半截 JSON，此后所有读取静默变空配置。

本模块提供：`read_json`（缺失→default；损坏→warning + 备份 `.corrupt.json` + default）、
`write_json_atomic`（tmp + chmod + replace，与 videonote_mcp/config.py 的 app_config 同款）。
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def read_json(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读 JSON 配置。文件缺失返回 default（空 dict）；损坏则打 warning、
    把损坏文件备份为 `<name>.corrupt.json`（只留第一份）后返回 default。"""
    if default is None:
        default = {}
    p = Path(path)
    if not p.exists():
        return default
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"JSON 根不是对象: {type(data).__name__}")
        return data
    except Exception as exc:  # noqa: BLE001 —— 配置损坏属于数据问题，不阻断进程
        backup = Path(f"{p}.corrupt")
        try:
            if not backup.exists():
                p.replace(backup)
        except OSError:
            pass
        logger.warning("配置文件损坏（已备份到 %s，按空配置处理）: %s", backup, exc)
        return default


def _unique_tmp(p: Path) -> Path:
    """tmp 带进程+随机唯一后缀（docs/05 第 16 轮 B8）：
    固定 <path>.tmp 在 CLI 与 MCP server 双进程并发写同一配置时互相截断丢更新；
    唯一后缀各自写完整文件，replace 仍是原子的。
    """
    import os
    import uuid

    return p.with_suffix(f".{os.getpid()}.{uuid.uuid4().hex}.tmp")


def _write_bytes_with_mode(path: Path, content: bytes, mode: int) -> None:
    """以 mode 权限创建文件并写入：os.open 创建即限权（0600），
    无「先默认 umask 创建、后 chmod」的短暂权限窗口（docs/05 第 16 轮 B8/L2）。"""
    import os

    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        os.write(fd, content)
    finally:
        os.close(fd)


def write_json_atomic(path: Path, data: Dict[str, Any], mode: int = 0o600) -> None:
    """原子写 JSON（tmp + replace + 权限）：磁盘满/进程中断不会留下半截文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(p)
    _write_bytes_with_mode(tmp, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), mode)
    tmp.replace(p)


def write_text_atomic(path: Path, text: str, mode: int = 0o600) -> None:
    """原子写文本（tmp + replace）：markdown/纯文本产物的写盘保护（#124 B13）。

    note.py 的笔记/缓存产物曾直接 write_text——进程中断/磁盘满留下截断文件：
    转写缓存截断 → 下次任务重下+重转写（小时级成本）；note.md 截断 → 半残不可恢复。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_tmp(p)
    _write_bytes_with_mode(tmp, text.encode("utf-8"), mode)
    tmp.replace(p)
