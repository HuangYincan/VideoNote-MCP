import os
import threading
from pathlib import Path
from typing import Optional, Dict

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
            return data.get(platform, {}).get("cookie")

    def set(self, platform: str, cookie: str):
        with self._lock:
            data = self._read()
            data[platform] = {"cookie": cookie}
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
            return {k: v.get("cookie", "") for k, v in data.items()}

    def exists(self, platform: str) -> bool:
        return self.get(platform) is not None
