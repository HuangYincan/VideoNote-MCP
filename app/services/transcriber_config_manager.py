import os
import threading
from pathlib import Path
from typing import Optional, Dict, Any

from app.utils.json_store import read_json, write_json_atomic


def _config_bool(data: Dict[str, Any], key: str, env_name: str) -> bool:
    """配置布尔解析：配置文件优先、env 兜底，字符串值按与 videonote_mcp.config.env_bool
    相同的规则（'1'/'true'/'yes'/'on'，大小写不敏感）解析（#124 A6）。

    两点旧坑：1) env 解析大小写敏感且不接受 yes/on，与 config.env_bool 规则分叉；
    2) 配置文件里手滑的字符串 "false" 被 `bool("false")` 判成 True——预处理/说话人
    分离被字符串垃圾值误开。此处不 import videonote_mcp（vendored 层单向依赖纪律），
    规则内联同源。
    """
    v = data.get(key)
    if v is None:
        v = os.getenv(env_name, "0")
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


class TranscriberConfigManager:
    """管理转写器配置，存储在 JSON 文件中，支持前端动态修改。"""

    # class-level 锁（#126 B9，与 cookie_manager #124 B15 同款）：update_config 是
    # read-modify-write 整文件，并发 set_transcriber 会互相抹掉对方刚写的字段；
    # 锁上整个 RMW 区间。get_config 只读不锁（json 读取是原子的文件级操作）。
    _lock = threading.RLock()

    def __init__(self, filepath: str = None):
        # 默认落在 VIDEONOTE_CONFIG_DIR（由 videonote_mcp.config 设置），避免依赖 CWD
        if filepath is None:
            filepath = str(Path(os.environ.get("VIDEONOTE_CONFIG_DIR", "config")) / "transcriber.json")
        self.path = Path(filepath)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        return read_json(self.path)

    def _write(self, data: Dict[str, Any]):
        write_json_atomic(self.path, data)

    def get_config(self) -> Dict[str, Any]:
        """获取当前转写器配置，fallback 到环境变量默认值。

        默认 size 为 small（#34 与 videonote_mcp.config 同源；MCP/CLI 下 env 已先设
        small，裸 vendored 用法由这里兜底——旧注释称 tiny 是更早时代的残留，#125 A8）。
        """
        data = self._read()
        return {
            "transcriber_type": data.get(
                "transcriber_type",
                os.getenv("TRANSCRIBER_TYPE", "fast-whisper"),
            ),
            "whisper_model_size": data.get(
                "whisper_model_size",
                os.getenv("WHISPER_MODEL_SIZE", "small"),
            ),
            "enable_preprocess": _config_bool(data, "enable_preprocess", "VIDEONOTE_ENABLE_PREPROCESS"),
            "diarization": _config_bool(data, "diarization", "VIDEONOTE_DIARIZATION"),
            "diarization_speakers": data.get("diarization_speakers"),
        }

    def update_config(
        self,
        transcriber_type: str,
        whisper_model_size: Optional[str] = None,
        enable_preprocess: Optional[bool] = None,
        diarization: Optional[bool] = None,
        diarization_speakers: Optional[int] = None,
    ) -> Dict[str, Any]:
        """更新转写器配置并持久化。"""
        with self._lock:
            data = self._read()
            data["transcriber_type"] = transcriber_type
            if whisper_model_size is not None:
                data["whisper_model_size"] = whisper_model_size
            if enable_preprocess is not None:
                data["enable_preprocess"] = bool(enable_preprocess)
            if diarization is not None:
                data["diarization"] = bool(diarization)
            if diarization_speakers is not None:
                data["diarization_speakers"] = int(diarization_speakers)
            self._write(data)
        return self.get_config()

    def get_transcriber_type(self) -> str:
        return self.get_config()["transcriber_type"]

    def get_whisper_model_size(self) -> str:
        return self.get_config()["whisper_model_size"]

    def get_enable_preprocess(self) -> bool:
        return bool(self.get_config()["enable_preprocess"])

    def get_diarization(self) -> bool:
        return bool(self.get_config()["diarization"])

    def get_diarization_speakers(self) -> Optional[int]:
        """说话人数提示（可选；None=自动检测）。"""
        v = self.get_config().get("diarization_speakers")
        return int(v) if v else None

    def is_model_ready(self) -> Dict[str, Any]:
        """当前转写器是否就绪可用。

        返回 {ready, transcriber_type, model_size, downloading, reason}：
          - 在线引擎 (groq/bcut/kuaishou)：永远 ready（不需要本地模型）
          - fast-whisper：检查 whisper-{size}/model.bin 落盘
          - mlx-whisper：检查 {repo_id}/config.json 落盘
        给 /generate_note 入口做「开始视频前先确认模型下载好」的门禁用。
        """
        cfg = self.get_config()
        ttype = cfg["transcriber_type"]
        size = cfg["whisper_model_size"]
        result = {
            "ready": True,
            "transcriber_type": ttype,
            "model_size": size,
            "downloading": False,
            "reason": "",
        }
        if ttype not in ("fast-whisper", "mlx-whisper", "funasr"):
            return result  # 在线引擎无需本地模型

        # 先确认运行环境装了对应包（模型文件在 ≠ 引擎能 import；如 mlx_whisper 是可选依赖）
        import importlib.util

        if ttype == "funasr":
            if importlib.util.find_spec("funasr") is None:
                result["ready"] = False
                result["reason"] = (
                    "funasr 不可用：未安装 funasr 包。请用 "
                    "`uv tool install --from git+https://github.com/HuangYincan/VideoNote-MCP videonote --with funasr --with torch`"
                    "（或 `uvx --from ... --with funasr --with torch`）安装；或切换转写引擎 `videonote transcriber set groq` / fast-whisper"
                )
                return result
            return result  # funasr 模型由引擎首次构造时自动下载，无需预检模型文件

        pkg = "mlx_whisper" if ttype == "mlx-whisper" else "faster_whisper"
        if importlib.util.find_spec(pkg) is None:
            result["ready"] = False
            if ttype == "mlx-whisper":
                result["reason"] = (
                    f"{ttype} 不可用：未安装 mlx_whisper 包。请用 "
                    "`uv tool install --from git+https://github.com/HuangYincan/VideoNote-MCP videonote --with mlx-whisper`"
                    "（或 `uvx --from ... --with mlx-whisper`）安装；或切换转写引擎 `videonote transcriber set groq` / fast-whisper"
                )
            else:
                result["reason"] = f"{ttype} 不可用：未安装 {pkg} 包"
            return result

        # 从 utils.model_status 取纯函数（本仓库已剥离 routers.config 的 Web 层）
        try:
            from app.utils.model_status import (
                check_whisper_model_exists,
                check_mlx_whisper_model_exists,
                is_downloading,
            )
        except Exception as e:
            # 拿不到检查函数时保守放行，不要把用户卡死
            result["reason"] = f"无法检查模型状态: {e}"
            return result

        if ttype == "fast-whisper":
            downloaded = check_whisper_model_exists(size, "whisper")
            downloading = is_downloading(size)
        else:  # mlx-whisper
            downloaded = check_mlx_whisper_model_exists(size)
            downloading = is_downloading(f"mlx-{size}")

        result["downloading"] = downloading
        if downloaded:
            return result
        result["ready"] = False
        result["reason"] = (
            f"转写模型 {ttype} / {size} 尚未下载就绪"
            + (
                "，正在下载中，请稍候"
                if downloading
                else f"，请先执行 `videonote transcriber download {size}` 下载"
            )
        )
        return result
