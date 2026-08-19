import os
import threading
from pathlib import Path
from typing import Dict, Optional

from app.utils.json_store import read_json, write_json_atomic


class CookieConfigManager:
    # class-level 锁（#124 B15）：多任务并发时各 NoteGenerator 各持一个实例，
    # 都读同一个 downloader.json。set/delete 是 read-modify-write，并发会互相
    # 抹掉对方刚写入的平台 cookie（读旧文件 → 覆盖写）；锁上整个 RMW 区间。
    # 用 RLock：exists() 内部调用 get()，重入不阻塞。
    _lock = threading.RLock()

    def __init__(self, filepath: str = None):
        # 默认落在 VIDEONOTE_CONFIG_DIR（由 videonote_mcp.config 设置），避免依赖 CWD
        if filepath is None:
            filepath = str(Path(os.environ.get("VIDEONOTE_CONFIG_DIR", "config")) / "downloader.json")
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self._lock:
                if not self.path.exists():
                    self._write({})

    def _read(self) -> Dict[str, Dict[str, str]]:
        return read_json(self.path)

    def _write(self, data: Dict[str, Dict[str, str]]):
        write_json_atomic(self.path, data)

    def get(self, platform: str) -> Optional[str]:
        with self._lock:
            data = self._read()
            val = data.get(platform)
            # 旧格式 {platform: "cookie 字符串"} 兼容：新格式 {platform: {cookie: ...}}
            # 值类型异常不裸崩（json_store 容错只覆盖文件层，不覆盖值类型，#125 B9）
            if isinstance(val, dict):
                return val.get("cookie")
            return val if isinstance(val, str) else None

    def get_browser(self, platform: str) -> Optional[str]:
        """已配置的 cookiesfrombrowser 来源（safari/chrome/...）；None 未配置。"""
        with self._lock:
            val = self._read().get(platform)
            if isinstance(val, dict):
                return val.get("browser") or None
            return None

    def set(self, platform: str, cookie: str):
        with self._lock:
            data = self._read()
            # 保留已配置的 browser（set_browser 与 set 互不覆盖，#C2）
            browser = data[platform].get("browser") if isinstance(data.get(platform), dict) else None
            data[platform] = {"cookie": cookie}
            if browser:
                data[platform]["browser"] = browser
            self._write(data)

    def set_browser(self, platform: str, browser: str):
        """配置 cookiesfrombrowser 来源；保留已存的手动 cookie（下载器优先用 browser）。"""
        with self._lock:
            data = self._read()
            entry = data.get(platform)
            if not isinstance(entry, dict):
                entry = {"cookie": entry if isinstance(entry, str) else ""}
            entry["browser"] = browser
            data[platform] = entry
            self._write(data)

    def delete(self, platform: str):
        with self._lock:
            data = self._read()
            if platform in data:
                del data[platform]
                self._write(data)

    def list_all(self) -> Dict[str, str]:
        with self._lock:
            data = self._read()
            out = {}
            for k, v in data.items():
                out[k] = v.get("cookie", "") if isinstance(v, dict) else (v if isinstance(v, str) else "")
            return out

    def exists(self, platform: str) -> bool:
        return self.get(platform) is not None
