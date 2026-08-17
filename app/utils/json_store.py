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


def write_json_atomic(path: Path, data: Dict[str, Any], mode: int = 0o600) -> None:
    """原子写 JSON（tmp + replace + 权限）：磁盘满/进程中断不会留下半截文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(mode)
    except OSError:
        pass
    tmp.replace(p)
