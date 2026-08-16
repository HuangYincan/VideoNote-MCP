import json
import os
from pathlib import Path
from typing import Optional, Dict


class CookieConfigManager:
    def __init__(self, filepath: str = None):
        # 默认落在 VIDEONOTE_CONFIG_DIR（由 videonote_mcp.config 设置），避免依赖 CWD
        if filepath is None:
            filepath = str(Path(os.environ.get("VIDEONOTE_CONFIG_DIR", "config")) / "downloader.json")
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})

    def _read(self) -> Dict[str, Dict[str, str]]:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _write(self, data: Dict[str, Dict[str, str]]):
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def get(self, platform: str) -> Optional[str]:
        data = self._read()
        return data.get(platform, {}).get("cookie")

    def set(self, platform: str, cookie: str):
        data = self._read()
        data[platform] = {"cookie": cookie}
        self._write(data)

    def delete(self, platform: str):
        data = self._read()
        if platform in data:
            del data[platform]
            self._write(data)

    def list_all(self) -> Dict[str, str]:
        data = self._read()
        return {k: v.get("cookie", "") for k, v in data.items()}

    def exists(self, platform: str) -> bool:
        return self.get(platform) is not None