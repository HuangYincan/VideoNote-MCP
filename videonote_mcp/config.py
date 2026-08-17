"""运行时环境初始化。

必须在 import app.*（vendored 核心流水线）之前调用 setup_environment()：
`app/db/engine.py` 与 `app/services/note.py` 在模块 import 时读取
DATABASE_URL / NOTE_OUTPUT_DIR 等环境变量。

数据根目录的解析逻辑：
  - 源码 checkout（`videonote_mcp/` 同级有 pyproject.toml）→ 仓库根 `data/`；
  - 已安装包（uvx / uv tool / pip，代码在 site-packages 或 uv 缓存里）→ 用户数据目录
    （macOS/Linux：`~/.local/share/videonote-mcp`；Windows：`%APPDATA%/videonote-mcp`），
    绝不写进 site-packages。
可用环境变量 VIDEONOTE_DATA_DIR 可显式覆盖。
"""
import logging
import os
import threading
from pathlib import Path

from videonote_mcp.crypto import decrypt_value, encrypt_value

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_IS_SOURCE_CHECKOUT = (_REPO_ROOT / "pyproject.toml").exists()

# Claude Code 插件 userConfig → MCP env 映射的全集。Claude Code 对用户跳过未填的
# userConfig 项会透传字面 `${user_config.x}`，这些值绝不能当真实配置，由
# _purge_placeholder_env() 统一剔除，让下游走默认值。
_USER_CONFIG_MAPPED_ENV = (
    "TRANSCRIBER_TYPE",
    "WHISPER_MODEL_SIZE",
    "VIDEONOTE_ENABLE_PREPROCESS",
    "VIDEONOTE_DIARIZATION",
    "VIDEONOTE_NOTES_DIR",
    "VIDEONOTE_DEFAULT_STYLE",
    "VIDEONOTE_DEFAULT_SCREENSHOT",
    "VIDEONOTE_VIDEO_UNDERSTANDING",
    "VIDEONOTE_VIDEO_INTERVAL",
    "VIDEONOTE_INCLUDE_COMMENTS",
    "VIDEONOTE_COMMENTS_LIMIT",
    "VIDEONOTE_DEFAULT_EXPORT_FORMATS",
    "VIDEONOTE_MAX_WORKERS",
    "VIDEONOTE_STDERR_LOG_MAX_MB",
)


def _purge_placeholder_env() -> None:
    """剔除 Claude Code 插件 userConfig 注入 env 的「无效值」。

    两种都会被剔除，让下游走默认值：
      1. 字面 `${...}`（Claude Code 对用户跳过未填项的原样透传）；
      2. 空字符串（部分版本对未填项传空串而非占位符）。
    必须在 setup_environment() 里 setdefault 默认值**之前**调用：pop 掉后
    setdefault 会重新填上正常默认值（如 TRANSCRIBER_TYPE=fast-whisper），
    否则坏值会一路当真实配置用（转写引擎直接挂）。
    """
    for name in _USER_CONFIG_MAPPED_ENV:
        v = os.environ.get(name)
        if not v or (v.startswith("${") and v.endswith("}")):
            os.environ.pop(name, None)


def _default_data_dir() -> Path:
    if _IS_SOURCE_CHECKOUT:
        return _REPO_ROOT / "data"
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "videonote-mcp"


def env_or(name: str) -> "str | None":
    """读取 env 字符串；未设置或空串 → None。

    供「配置文件优先、env 兜底」的读取点用（Claude Code 插件 userConfig 注入）。
    """
    v = os.environ.get(name)
    if v is None or not v.strip():
        return None
    return v


_BOOL_TRUE = ("1", "true", "yes", "on")
_BOOL_FALSE = ("0", "false", "no", "off")


def env_bool(name: str, default: bool = False) -> bool:
    """解析 env 布尔（'1'/'true'/'yes'/'on'，大小写不敏感）；未设置回 default。

    垃圾值（非已知布尔词）也回退 default——与 env_int 的「解析失败回退」同语义；
    旧实现把垃圾值一律当 False，default=True 的调用点会被手滑值静默翻反（#124 A5）。
    """
    v = env_or(name)
    if v is None:
        return default
    v = v.strip().lower()
    if v in _BOOL_TRUE:
        return True
    if v in _BOOL_FALSE:
        return False
    return default


def env_int(name: str, default: int) -> int:
    """解析 env 整数；未设置或解析失败回 default。"""
    v = env_or(name)
    if v is None:
        return default
    try:
        return int(v)
    except ValueError:
        return default


def resolve_int_config(key: str, env_name: str, default: int) -> int:
    """解析 app_config 整数配置：垃圾值 warning 后回退 env/默认（#116）。

    MCP 与 CLI 向导共用入口（#120 前 CLI 各自裸 int()，垃圾值每次进循环就崩）。
    缺省链是 参数 → app_config → env → 默认（#107 口径）。`get(...) or env_int(...)`
    的 truthy 短路有两处缺陷：垃圾值（手动编辑 app_config.json 写入 "abc"）让
    int() 裸 ValueError 遮蔽回退链；0（显式关闭，如 video_interval=0 关视频理解）
    被当 falsy 吞掉、app_config 优先级倒挂给 env。is not None 判断 + int() 防御
    两处一起修（与 #107 default_export_formats 同族）。
    """
    raw = get_app_config().get(key)
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            logger.warning("app_config.%s 非整数（%r），回退 env/默认", key, raw)
    return env_int(env_name, default)


def env_json_list(name: str, default):
    """解析 env JSON 数组（如 '["srt","vtt"]'）；未设置/非法回 default。"""
    v = env_or(name)
    if v is None:
        return default
    try:
        import json

        parsed = json.loads(v)
        return parsed if isinstance(parsed, list) else default
    except Exception:
        return default


def resolve_default_export_formats() -> list:
    """导出格式缺省链：app_config（须为列表）→ env（JSON 列表）→ []。

    非列表垃圾配置会遮蔽 env 回退（truthy 值令 `or` 短路）→ 打 warning 后回退
    （#107 口径；#124 A4：守卫从 server 抽到此处，CLI export / MCP export_transcript /
    自动导出三处同源，不再各自实现）。"""
    cfg = get_app_config().get("default_export_formats")
    if cfg is not None and not isinstance(cfg, list):
        logger.warning("app_config.default_export_formats 不是列表（%r），忽略改用环境/默认", cfg)
        cfg = None
    return cfg or env_json_list("VIDEONOTE_DEFAULT_EXPORT_FORMATS", [])


def setup_environment() -> Path:
    """解析数据目录并设置环境变量（仅在没有显式设置时填充默认值）。返回数据根目录 Path。"""
    _purge_placeholder_env()
    data_dir = Path(os.environ.get("VIDEONOTE_DATA_DIR") or _default_data_dir()).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    note_results = data_dir / "note_results"
    screenshots = data_dir / "static" / "screenshots"
    config_dir = data_dir / "config"
    models_dir = data_dir / "models"
    logs_dir = data_dir / "logs"
    for d in (note_results, screenshots, config_dir, models_dir, logs_dir):
        d.mkdir(parents=True, exist_ok=True)

    # 数据根目录本身（logger / path_helper / downloaders 会读）
    os.environ.setdefault("VIDEONOTE_DATA_DIR", str(data_dir))
    # SQLite 数据库（engine.py 在 import 时读）
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{data_dir / 'video_note.db'}")
    # 笔记/截图输出目录（note.py 在 import 时读）
    os.environ.setdefault("NOTE_OUTPUT_DIR", str(note_results))
    os.environ.setdefault("IMAGE_OUTPUT_DIR", str(screenshots))
    # Markdown 里截图 URL 前缀 —— 用 file:// 绝对路径，agent 可直接读取
    os.environ.setdefault("IMAGE_BASE_URL", screenshots.as_uri())
    # 转写引擎默认值（transcriber_config_manager 无配置文件时 fallback）
    os.environ.setdefault("TRANSCRIBER_TYPE", "fast-whisper")
    os.environ.setdefault("WHISPER_MODEL_SIZE", "small")
    # whisper/mlx 模型下载的请求超时：网络不可达时让每次下载快速失败，
    # 避免 huggingface_hub 重试 + WhisperTranscriber 自愈重下长时间阻塞任务
    # （真正需要音频转写的任务会以 FAILED + 明确错误结束，而非卡在 INITIALIZING）。
    os.environ.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "60")
    os.environ.setdefault("HF_HUB_ETAG_TIMEOUT", "10")
    # 配置目录（transcriber_config / cookie 落到这里，避免依赖 CWD）
    os.environ.setdefault("VIDEONOTE_CONFIG_DIR", str(config_dir))
    # 模型目录：已安装包时一定要指到用户数据目录（否则会写进 site-packages）。
    # 源码 checkout 保持原默认 <仓库>/models（已有下载的模型不迁移）。
    if not _IS_SOURCE_CHECKOUT:
        os.environ.setdefault("VIDEONOTE_MODEL_DIR", str(models_dir))

    return data_dir

_APP_CONFIG_LOCK = threading.Lock()

# app_config 里需要落盘加密的敏感字段（docs/05 #29）
_SENSITIVE_CONFIG_KEYS = ("hf_token",)


def get_app_config() -> dict:
    """读取持久化应用配置（如默认笔记位置），存于 VIDEONOTE_CONFIG_DIR/app_config.json。

    敏感字段（hf_token）返回解密后的明文；解密失败按缺失处理。
    """
    import json

    path = Path(os.environ.get("VIDEONOTE_CONFIG_DIR", "config")) / "app_config.json"
    if path.exists():
        try:
            cfg = json.loads(path.read_text(encoding="utf-8"))
            for k in _SENSITIVE_CONFIG_KEYS:
                if k in cfg:
                    cfg[k] = decrypt_value(cfg[k])
            return cfg
        except Exception:
            return {}
    return {}


def _write_app_config(cfg: dict) -> None:
    """原子写 app_config.json（tmp + replace），带 0600 权限；敏感字段加密落盘。"""
    import json

    payload = dict(cfg)
    for k in _SENSITIVE_CONFIG_KEYS:
        if payload.get(k):
            payload[k] = encrypt_value(payload[k])
    path = Path(os.environ.get("VIDEONOTE_CONFIG_DIR", "config")) / "app_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def set_app_config(key: str, value) -> None:
    """持久化应用配置（进程内 threading.Lock + 原子写）。

    #123 A10：锁只保证**单进程内**多线程不互相覆盖读-改-写；双进程并发写仍可能
    丢更新（各自读旧值再写回）。原子写（tmp+replace）保证文件不损坏，不保证
    跨进程合并。CLI 与 MCP server 是独立进程，不要在两端同时改同一 app_config。
    """
    with _APP_CONFIG_LOCK:
        cfg = get_app_config()
        cfg[key] = value
        _write_app_config(cfg)


def remove_app_config(key: str) -> None:
    """删除一条持久化应用配置（不存在则无操作）。

    用于「清除默认模型」等场景：必须删 key，而不是写成 null。
    """
    with _APP_CONFIG_LOCK:
        cfg = get_app_config()
        if key not in cfg:
            return
        cfg.pop(key)
        _write_app_config(cfg)
