"""VideoNote-Mcp —— 把视频内容处理能力封装为 MCP 工具。

架构：内嵌流水线（`app/` 为 vendored 自上游的核心模块），**无需启动 FastAPI 后端**。
生成笔记为异步任务：`generate_note` 立即返回 task_id，后台线程执行
`NoteGenerator.generate()`，进度写入 note_results/{task_id}/status.json，
最终结果写入 note_results/{task_id}/result.json（任务文件夹布局）。

运行时环境（数据目录、DB、输出目录）在 import app.* 之前由 config.setup_environment()
初始化，详见 videonote_mcp/config.py。
"""
import atexit
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from videonote_mcp import __version__ as _SERVER_VERSION
from videonote_mcp.config import (
    env_bool,
    env_int,
    env_or,
    get_app_config,
    resolve_bool_config,
    resolve_default_export_formats,
    resolve_int_config,
    setup_environment,
)

DATA_DIR = setup_environment()

# MCP stdio 传输用 stdout 承载 JSON-RPC；vendored 代码里有大量裸 print()（含模块导入时）
# 会污染协议。进程级把 print 重定向到 stderr —— 必须在 import app.* 之前生效
#（FastMCP 通过 sys.stdout.buffer 写响应，不受影响）。
import builtins as _builtins

_orig_print = _builtins.print


def _print_to_stderr(*args, **kwargs):
    kwargs.setdefault("file", sys.stderr)
    _orig_print(*args, **kwargs)


_builtins.print = _print_to_stderr

# stdio MCP：把 stderr 重定向到日志文件，避免后台任务的大量输出把 stderr 管道塞满、
# 阻塞事件循环（logging 持锁跨线程阻塞 → 「第二个工具调用挂起」）。协议只用 stdin/stdout。

def _open_stderr_log(max_mb: int = 50):
    """打开 mcp_stderr.log（超限先轮转，防止长跑后日志体积失控，docs/05 #44）。

    返回文件对象；失败返回 None 并尽量把原因打印到原始 stderr（此时 dup2 尚未发生）。
    """
    path = DATA_DIR / "logs" / "mcp_stderr.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            limit = max(1, int(os.getenv("VIDEONOTE_STDERR_LOG_MAX_MB", str(max_mb)))) * 1024 * 1024
        except (TypeError, ValueError):
            # env 非法值回退默认，不把整个日志重定向弄挂（docs 审计 H 组）
            limit = max_mb * 1024 * 1024
        if path.exists() and path.stat().st_size > limit:
            path.replace(path.with_suffix(".log.1"))
        return open(path, "a", encoding="utf-8", buffering=1)
    except Exception as exc:
        print(f"[videonote] 打开 stderr 日志失败({exc}),继续直接输出", file=sys.stderr)
        return None


try:
    _stderr_log = _open_stderr_log()
    if _stderr_log is not None:
        os.dup2(_stderr_log.fileno(), 2)   # OS 层：子进程（yt-dlp/ffmpeg）的 stderr 也进文件
        sys.stderr = _stderr_log            # Python 层：logging / vendored print 进文件
except Exception:
    pass  # 重定向失败不致命，_open_stderr_log 已把原因打到原始 stderr

# app.* 相关导入必须在 setup_environment() 之后 —— 否则 VIDEONOTE_DATA_DIR/CONFIG_DIR 未设置，
# logger/配置会用 CWD 相对路径建 config/logs（在笔记目录里出现多余文件夹）。
from mcp.server.fastmcp import FastMCP

# vendored 核心流水线
from app.db.engine import get_engine
from app.db.init_db import init_db
from app.db.model_dao import (
    get_models_by_provider,
)
from app.db.provider_dao import seed_default_providers
from app.enmus.note_enums import DownloadQuality
from app.enmus.task_status_enums import TaskStatus
from app.exceptions.task import TaskCancelledError
from app.exceptions.task import check_cancel as _check_cancel
from app.services import note_cache, pipeline
from app.services.cookie_manager import CookieConfigManager
from app.services.note import NOTE_OUTPUT_DIR, NoteGenerator
from app.services.provider import ProviderService
from app.services.transcriber_config_manager import TranscriberConfigManager
from app.transcriber import model_download_state as dl_state
from app.utils.logger import get_logger
from app.utils.model_status import check_whisper_model_exists
from app.utils.task_manifest import (
    cleanup_all_files,
    cleanup_targets_inside_data_root,
    cleanup_task_files,
    get_task_paths,
    list_task_files,
    record_task_paths,
)
from app.utils.url_safety import (
    assert_public_http_url,
    sanitize_error_text,
    sanitize_error_url,
    sanitize_url,
)
from videonote_mcp.provider_probe import probe_models

logger = get_logger(__name__)

# 枚举白名单：schema 以 Literal 呈现（Agent 可见合法值），非法值显式报错而非静默降级
# （get_style_format / get_format_function 对未知值返回空串——静默无风格笔记）。
Style = Literal[
    "minimal", "detailed", "academic", "tutorial", "xiaohongshu",
    "life_journal", "task_oriented", "business", "meeting_minutes",
]
NoteFormat = Literal["toc", "link", "screenshot", "summary"]

_STYLE_VALUES = (
    "minimal", "detailed", "academic", "tutorial", "xiaohongshu",
    "life_journal", "task_oriented", "business", "meeting_minutes",
)
_FORMAT_VALUES = ("toc", "link", "screenshot", "summary")


def _check_style_and_format(style, formats) -> None:
    """显式传入的 style/format 必须命中白名单。

    schema enum 只约束客户端生成参数（MCP 服务端不做运行时校验），
    直接调用函数/老客户端仍可能传非法值——静默降级成「无风格笔记」太隐蔽，入口显式报错。
    默认值路径（setup 配置）不在校验范围：配置是用户自持的，坏值走原有行为。

    formats 非列表（如字符串 "toc"）曾穿透到 `set(formats)` 被拆成字符集——
    报「收到: ['c','o','t']」把合法格式说成非法；int/str 混排还让 sorted() 裸
    TypeError。入口显式要求字符串列表（与 export_transcript 的 #104 口径一致）。
    """
    if style is not None and style not in _STYLE_VALUES:
        raise ValueError(f"style 必须是 {' / '.join(_STYLE_VALUES)} 之一，收到: {style!r}")
    if formats:
        if not isinstance(formats, (list, tuple)):
            raise ValueError(
                f"format 必须是字符串列表（支持 {' / '.join(_FORMAT_VALUES)}），收到: {formats!r}"
            )
        unknown = sorted({str(f) for f in formats if f not in _FORMAT_VALUES})
        if unknown:
            raise ValueError(f"format 只支持 {' / '.join(_FORMAT_VALUES)}，收到: {unknown}")


def _check_grid_size(grid_size) -> None:
    """grid_size 必须是两个正整数（如 [3,3]），否则入口显式报错。

    非法值（[0,0] / [1] / [1,2,3]）会在流水线深处的 VideoReader 才炸成
    「视频处理失败」泛化错误；与 style/format 同口径，入口尽早报清楚。
    None/空走默认（调用方兜底 [2,2]/[3,3]），不拦。
    """
    if not grid_size:
        return
    if not (len(grid_size) == 2 and all(isinstance(n, int) and n >= 1 for n in grid_size)):
        raise ValueError(f"grid_size 必须是两个正整数（如 [3,3]），收到: {grid_size!r}")


def _resolve_int_config(key: str, env_name: str, default: int) -> int:
    """app_config 整数配置解析（共享实现见 config.resolve_int_config，#120）。"""
    return resolve_int_config(key, env_name, default)


def _resolve_bool_config(key: str, env_name: str, default: bool) -> bool:
    """app_config 布尔配置解析（共享实现见 config.resolve_bool_config，#130 A1）。

    `bool(get_app_config().get(...))` 的 truthy-swallow：手动写入 "false"/"0"/"no"
    会被当成 True——视频理解/弹幕/截图开关静默翻转开启。bool 词表解析见 config。
    """
    return resolve_bool_config(key, env_name, default)


def _coerce_int(value, default: int, clamp_min: Optional[int] = None) -> int:
    """显式数值参数安全转换：垃圾值打 warning 回退 default（#125 C5）。

    垃圾值统一打 warning 回退默认——裸 int("abc") 报 "invalid literal for int()"
    天书错误，Agent 无法得知合法形状。
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        logger.warning(f"数值参数 {value!r} 无法解析，回退默认 {default}")
        n = default
    # default 可为 None（可空参数如 diarization_speakers=自动检测）：
    # 解析失败回退 None 时不再 clamp（#126 C5）
    if clamp_min is not None and n is not None:
        n = max(clamp_min, n)
    return n

# 确保数据库表存在（幂等，init_db 使用 create_all）；空库时预置内置供应商
# （openai/deepseek/qwen/groq/ollama…，固定 id + 正确 base_url + 空 key，用 update_provider 填 key）
init_db()
seed_default_providers()

mcp = FastMCP("videonote")

# ---------- 后台任务 ----------

_MAX_WORKERS = max(1, env_int("VIDEONOTE_MAX_WORKERS", 3))
_pool = ThreadPoolExecutor(max_workers=_MAX_WORKERS)

# 任务注册表：task_id -> (Future, cancel_event)，供 task(action='cancel') 使用（thread-safe）
_tasks_lock = threading.Lock()
_task_futures: Dict[str, Future] = {}
_task_events: Dict[str, threading.Event] = {}
# 普通任务在提交锁内预占并发名额；标记挂在 Future 上，避免额外注册表与
# 旧测试/清理代码仅移除 _task_futures 时产生残留 reservation。batch 任务显式旁路，
# 只由 ThreadPoolExecutor 排队，不占普通任务的预占名额。
_CONCURRENCY_RESERVED_ATTR = "_videonote_concurrency_reserved"
_CONCURRENCY_BATCH_ATTR = "_videonote_batch_kind"
_batch_ctx = threading.local()  # batch_generate_notes 内部批量提交的旁路标志（#121 C1）
# 最近一次 _write_status 的写盘快照（写盘失败/文件损坏时状态查询回退，
# 避免把运行中/已完成任务误报成 PENDING，见 #118）
_status_memory: Dict[str, dict] = {}
# 内存快照上限（#123 A9）：持续写盘故障时快照只增不删会无界积累——超限按最旧淘汰
_STATUS_MEMORY_MAX = 512


def _exit_summary() -> None:
    """正常退出（sys.exit）时记录进行中任务数，便于排查孤儿 ffmpeg/whisper 子进程。

    写 sys.__stderr__（原始 fd 2，dup2 后仍指向 mcp_stderr.log）而非 logging：
    atexit 时 logging handler 可能已关闭，logger 调用会触发
    「Logging error: I/O operation on closed file」（docs 审计 P2-6）。
    已知限制（docs/05 #44）：线程池 worker 是 daemon 线程，SIGKILL 或客户端强杀时
    钩子不执行，转写/下载子进程会残留跑完；这是 Python 子进程管理的固有边界。

    额外（docs 审计 G5）：退出时 set 所有进行中/排队任务的 cancel_event——
    解释器退出前会 join 非 daemon 线程池线程，任务在阶段边界检查取消后尽快收敛，
    缩短 ffmpeg/whisper 子进程残留窗口。纯增益、零风险（任务已能处理取消）。
    """
    try:
        with _tasks_lock:
            active = len(_task_futures)
            events = list(_task_events.values())
        for ev in events:
            try:
                ev.set()
            except Exception:
                pass
        sys.__stderr__.write(f"[videonote] 退出;进行中/排队任务 {active} 个(已发送取消)\n")
    except Exception:
        pass


atexit.register(_exit_summary)


def _write_status(
    task_id: str,
    status,
    message: Optional[str] = None,
    *,
    strict: bool = False,
) -> bool:
    """写入 ``{task_dir}/status.json`` 并同步任务索引。

    文件是任务状态的发布源：只有状态文件成功落盘后才同步 SQLite，避免
    ``video_tasks.status`` 领先于磁盘状态。普通模式保留历史「尽力而为、不裸抛」
    契约；``strict=True`` 用于发布 SUCCESS 前的关键状态，任一持久化步骤失败都会
    重新抛出，由 worker 把任务收敛到 FAILED（而不是暴露一个不可信的 SUCCESS）。
    """
    task_dir = NOTE_OUTPUT_DIR / str(task_id)
    f = task_dir / "status.json"
    # 保留旧 started_at（elapsed_secs 从首次提交起算）——每次重打会让成功任务的
    # 终态 elapsed≈0（PENDING→INITIALIZING→…→SUCCESS 全由本函数写，见 #118）
    try:
        old = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
        started = old.get("started_at")
    except Exception:
        started = None
    data = {"status": status.value if isinstance(status, TaskStatus) else str(status)}
    safe_message = sanitize_error_text(message) if message else ""
    if safe_message:
        data["message"] = safe_message
    data["started_at"] = started if started is not None else time.time()
    # 写盘前更新内存快照：磁盘满/权限故障时状态查询可回退（见 #118）
    with _tasks_lock:
        _status_memory[task_id] = data
        # 上限防无界（#123 A9）：dict 保插入序，超限淘汰最旧快照
        if len(_status_memory) > _STATUS_MEMORY_MAX:
            _status_memory.pop(next(iter(_status_memory)), None)
    file_written = False
    try:
        task_dir.mkdir(parents=True, exist_ok=True)
        # tmp 唯一后缀（docs/05 第 16 轮 B9）：与 note 侧 _update_status 双写者
        # 不再共用固定 status.tmp；创建即 0600
        from app.utils.json_store import _unique_tmp, _write_bytes_with_mode

        tmp = _unique_tmp(f)
        _write_bytes_with_mode(
            tmp, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), 0o600
        )
        tmp.replace(f)
        file_written = True
        # 终态已落盘且不再变化：弹内存快照，防长生命周期 server 无界增长
        # （写盘失败时保留——快照是读盘损坏时的唯一回退，#121 C9）
        if data["status"] in {"SUCCESS", "FAILED", "CANCELLED"}:
            with _tasks_lock:
                _status_memory.pop(task_id, None)
    except Exception as exc:  # noqa: BLE001 —— 环境故障（磁盘满/只读）：不裸抛
        # （裸抛会进后台线程被吞，且 FAILED 重写循环同样失败），内存快照已可查
        logger.error(f"写状态文件失败 task_id={task_id}: {sanitize_error_text(exc)}")
        if strict:
            raise
        return False

    # 同步全局索引（文件成功后才执行，避免 DB 领先于文件）
    try:
        from app.db.video_task_dao import update_task_status

        update_task_status(str(task_id), data["status"], message=safe_message)
    except Exception as exc:
        logger.warning(
            "同步任务索引失败 task_id=%s status=%s: %s",
            task_id,
            data.get("status"),
            sanitize_error_text(exc),
        )
        if strict:
            raise
        return False
    return file_written


def _atomic_write_json(path: Path, payload) -> None:
    """原子写 JSON（tmp + replace + 唯一后缀 + 0600）：避免轮询读到半截文件（docs/05 #54）。

    #140 A5（#133 A2 登记项收尾）：固定 `<path>.json.tmp` 在 CLI 与 MCP server 双进程
    并发写时互相截断丢更新；与 json_store/status 同口径——_unique_tmp + 创建即 0600。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    from app.utils.json_store import _unique_tmp, _write_bytes_with_mode

    tmp = _unique_tmp(path)
    _write_bytes_with_mode(
        tmp, json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8"), 0o600
    )
    tmp.replace(path)


def _status_is_terminal(task_id: str) -> bool:
    """status.json 是否已到终态（SUCCESS/FAILED/CANCELLED）。"""
    try:
        data = json.loads(
            (NOTE_OUTPUT_DIR / str(task_id) / "status.json").read_text(encoding="utf-8")
        )
        return isinstance(data, dict) and data.get("status") in ("SUCCESS", "FAILED", "CANCELLED")
    except Exception:
        return False


def _read_task_status(task_id: str) -> str:
    """读任务 status.json 的 status 字段；文件缺失/损坏时返回 UNKNOWN（#123 A8 抽取）。

    get_task_transcript 的两处「读真实状态」共用——此前 segment_range 非法时硬编码
    `status:"UNKNOWN"`，SUCCESS 任务被误判成未知。
    """
    try:
        st = json.loads(
            (NOTE_OUTPUT_DIR / str(task_id) / "status.json").read_text(encoding="utf-8")
        )
        if not isinstance(st, dict):
            return "UNKNOWN"
        status = st.get("status")
        return status if isinstance(status, str) and status.strip() else "UNKNOWN"
    except Exception:
        return "UNKNOWN"


def _valid_status_data(data) -> Optional[dict]:
    """只接受形如 ``{"status": "..."}`` 的状态 JSON 根对象。"""
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if not isinstance(status, str) or not status.strip():
        return None
    return data


def _memory_status_snapshot(task_id: str) -> Optional[dict]:
    """读取一个有效的内存状态快照，返回副本避免调用方意外修改全局状态。"""
    with _tasks_lock:
        snapshot = _valid_status_data(_status_memory.get(task_id))
        return dict(snapshot) if snapshot is not None else None


def _transcript_unavailable_reason(task_id: str) -> str:
    """「任务没有可读转写」的准确原因：读 status.json 区分不存在/运行中/未成功/成功无转写。

    task 转写分支 / MCP Resource / process_media export 共用——运行中的任务此前被
    笼统报「尚未成功或已清理」，Agent 可能误向用户报告「任务失败了」（#114）。
    """
    status = _read_task_status(task_id)
    if status == "UNKNOWN":
        return "任务状态不可读（可能已清理）"
    if status == "SUCCESS":
        return "任务成功但没有转写"
    if status in ("FAILED", "CANCELLED"):
        return f"任务未成功（{status}）"
    return f"任务仍在运行（{status}）：先 task(action='status') 等终态"


def _absolutize_images(markdown: Optional[str], base_dir: Optional[str] = None) -> str:
    """把 Markdown 里的相对图片路径改写为 file:// 绝对路径。

    两类来源（note.py _insert_screenshots）：
    1. 旧全局模式：`/static/screenshots/...` → DATA_DIR/static/screenshots；
    2. 便携笔记模式：`Assets/xxx.jpg`（相对 note_dir=gen/，见 G2）→ base_dir/Assets/...。
    两者都做 resolve + 目录内校验（防路径穿越），逃逸路径原样保留。
    """
    if not markdown:
        return markdown
    base = DATA_DIR / "static" / "screenshots"

    def _repl(m):
        try:
            rel = m.group(1)
            target = (base / rel).resolve()
            target.relative_to(base.resolve())  # 防路径穿越（docs 审计 H 组）
            return f"]({target.as_uri()})"
        except Exception:
            return m.group(0)

    out = re.sub(r"\]\(/?(static/screenshots/[^)]+)\)", _repl, markdown)
    if base_dir:
        root = Path(base_dir).resolve()

        def _repl_assets(m):
            try:
                target = (root / m.group(1)).resolve()
                target.relative_to(root)
                return f"]({target.as_uri()})"
            except Exception:
                return m.group(0)

        out = re.sub(r"\]\((Assets/[^)]+)\)", _repl_assets, out)
    return out


def _public_audio_meta(audio_meta) -> Optional[dict]:
    """Project downloader metadata to fields safe to return through MCP.

    ``AudioDownloadResult.raw_info`` is an internal pipeline/cache detail.  yt-dlp
    may put signed media URLs, request headers, or cookie material in it, so it
    must never cross the task-result boundary (including results written by older
    versions, which are projected again when read).
    """
    if audio_meta is None:
        return None
    if isinstance(audio_meta, dict):
        return {
            key: audio_meta[key]
            for key in ("file_path", "title", "duration", "platform", "video_id", "video_path")
            if key in audio_meta
        }
    return {
        key: getattr(audio_meta, key, None)
        for key in ("file_path", "title", "duration", "platform", "video_id", "video_path")
    }


def _run_note_task(task_id: str, cancel_event: Optional[threading.Event] = None, **params) -> None:
    """在后台线程执行 NoteGenerator.generate，并落盘最终结果。"""
    try:
        _check_cancel(cancel_event)  # 排队期间被取消 → 直接 CANCELLED，不写 INITIALIZING
        _write_status(task_id, "INITIALIZING", message="正在准备…")
        generator = NoteGenerator()
        # MCP 的 SUCCESS 必须在 result.json 和 manifest 都落盘后发布，避免
        # 轮询看到 SUCCESS 却拿不到结果；直接调用 NoteGenerator 仍保留默认行为。
        result = generator.generate(
            task_id=task_id,
            cancel_event=cancel_event,
            publish_success=False,
            **params,
        )
        if result is None:
            # generate() 内部已写 FAILED 状态
            return
        _check_cancel(cancel_event)
        material = getattr(result, "material", None)
        if material:
            # material_only 模式：不产 markdown，payload 写素材包各字段
            # （markdown 为空字符串，状态查询的 absolutize 分支自动跳过）
            payload = {
                "kind": "material",
                "title": material.get("title"),
                "transcript": material.get("transcript"),
                "frames": material.get("frames") or [],
                "comments_danmaku": material.get("comments_danmaku"),
                "video_path": material.get("video_path"),
                "audio_path": material.get("audio_path"),
            }
        else:
            payload = {
                "markdown": result.markdown,
                "transcript": asdict(result.transcript) if result.transcript else None,
                "audio_meta": _public_audio_meta(result.audio_meta),
            }
        # 每任务文件夹统一根；payload 补语义标题（所有任务形态暴露统一 title）
        task_dir = NOTE_OUTPUT_DIR / task_id
        if "title" not in payload:
            payload["title"] = (
                (result.audio_meta.title if getattr(result, "audio_meta", None) else None)
                or (material.get("title") if material else None)
                or ""
            )
        # note_dir 契约（docs 审计 G2）：note.md 恒在 {task_id}/gen/note.md；
        # 指定 notes_dir 时便携副本路径从 manifest 取（记录在 extra_paths）
        gen_dir = task_dir / "gen"
        payload["note_dir"] = str(gen_dir) if (gen_dir / "note.md").is_file() else str(task_dir)
        try:
            _portable = [
                p for p in get_task_paths(task_id)
                if str(p).endswith("note.md") and not str(p).startswith(str(task_dir))
            ]
            if _portable:
                payload["portable_note_dir"] = str(Path(_portable[0]).parent)
        except Exception:  # noqa: BLE001 —— manifest 读取失败不影响主结果
            pass
        # result.json 写进任务文件夹（替代扁平 {task_id}.json）—— 原子写；
        # SUCCESS 延迟到 result.json 和 manifest 都落盘后，轮询不会看到半成品任务。
        _atomic_write_json(task_dir / "result.json", payload)
        # 按「导出格式默认」自动导出纯格式（srt/vtt/json，确定性渲染）——尽力而为，失败不阻断。
        # 导出产物会追加 manifest，因此必须先完成导出，再做一次严格清单持久化。
        exported = _auto_export_transcript(task_id, payload.get("transcript"))
        auto_export_paths = []
        if isinstance(exported, dict):
            for fmt, uri in exported.items():
                if fmt == "_errors" or not isinstance(uri, str) or not uri.startswith("file://"):
                    continue
                path = _coerce_local_path(uri)
                if path.is_file():
                    auto_export_paths.append(path)
        record_task_paths(
            task_id,
            [
                task_dir,
                task_dir / "result.json",
                task_dir / "status.json",
                *auto_export_paths,
            ],
            strict=True,
        )
        # 最后发布 SUCCESS：此后 result.json/manifest 不再由本流程写入。
        _write_status(task_id, TaskStatus.SUCCESS, message="完成", strict=True)
        logger.info(f"笔记生成成功 task_id={task_id}")
    except TaskCancelledError:
        logger.info(f"任务已取消 task_id={task_id}")
        _write_status(task_id, TaskStatus.CANCELLED, message="任务已取消")
    except Exception as e:
        safe_error = sanitize_error_text(e)
        logger.error("任务异常 task_id=%s: %s", task_id, safe_error)
        _write_status(task_id, TaskStatus.FAILED, message=safe_error)
    finally:
        with _tasks_lock:
            future = _task_futures.pop(task_id, None)
            # 普通任务的 admission reservation 生命周期覆盖整个 worker；
            # worker 退出前释放，随后才从注册表移除。batch Future 没有 reservation，
            # discard 语义由 False 标记自然覆盖。
            if future is not None:
                try:
                    setattr(future, _CONCURRENCY_RESERVED_ATTR, False)
                except Exception:
                    pass
            _task_events.pop(task_id, None)


def _auto_export_transcript(task_id: str, transcript) -> Dict[str, Any]:
    """笔记任务成功后按 `default_export_formats` 自动导出纯格式（srt/vtt/json）。

    尽力而为：任何失败只记日志，不阻断主任务成功状态。只导出确定性机械格式，
    不涉及 LLM/网络；导出文件自动记入 manifest（供 cleanup 清理）。返回 exporter
    的映射，供 worker 在最终 strict manifest 中再次登记实际写出的文件。
    """
    try:
        from videonote_mcp.export import export_transcript

        default_formats = resolve_default_export_formats()
        if not default_formats or not transcript:
            return {}
        exported = export_transcript(
            transcript,
            formats=default_formats,
            out_dir=NOTE_OUTPUT_DIR / task_id / "gen",
            task_id=task_id,
        )
        return exported if isinstance(exported, dict) else {}
    except Exception as exc:
        logger.warning(f"自动导出失败 task_id={task_id}: {sanitize_error_text(exc)}")
        return {}


def _future_is_reserved(future: Future) -> bool:
    """返回 Future 是否持有普通任务的并发预占名额。"""
    # 用 `is True` 而不是 truthiness：unittest.mock.Mock 的任意未知属性本身
    # 也是真值，不能让测试替身被误算成 reservation。
    return getattr(future, _CONCURRENCY_RESERVED_ATTR, None) is True


def _future_is_running(future: Future) -> bool:
    """安全读取 Future.running()，避免测试替身的 Mock 属性污染计数。"""
    try:
        return future.running() is True
    except Exception:
        return False


def _future_is_batch(future: Future) -> bool:
    """返回 Future 是否明确属于 batch 提交。"""
    # 只把明确写入的 True 当成 batch；旧 Future/测试替身没有该属性时
    # 继续走 running() 兜底，避免悄悄放过旧式普通任务。
    return getattr(future, _CONCURRENCY_BATCH_ATTR, None) is True


def _active_task_count_locked() -> int:
    """在 `_tasks_lock` 已持有时计算并发容量占用。"""
    active = 0
    for future in _task_futures.values():
        # Batch Future 明确由线程池排队，不占普通任务的 admission 名额，
        # 即使它已经 running 也不能阻塞 ordinary admission。
        if _future_is_batch(future):
            continue
        # 普通任务由 reservation 覆盖排队及执行全生命周期；旧式未标记
        # Future/测试 fake 没有 reservation 时，按 running() 兼容计数。
        if _future_is_reserved(future) or _future_is_running(future):
            active += 1
    return active


def _concurrency_error(active: int) -> ValueError:
    return ValueError(
        f"已有 {active} 个任务在同时执行（上限 {_MAX_WORKERS}）：请先等其中一些完成"
        f"（或 task(action='cancel') 取消）再提交。"
    )


def _guard_concurrency() -> None:
    """并发门禁：普通提交达到 `VIDEONOTE_MAX_WORKERS` 时拒绝新任务。

    该函数保留给调用方/回归测试做只读检查；真正的提交必须使用
    `_submit_registered_task`，在同一把 `_tasks_lock` 内完成检查、reservation、
    `_pool.submit()` 与 Future/Event 登记，避免 check-then-act 竞态。普通任务
    reservation 覆盖排队和执行整个生命周期；batch_generate_notes 仍显式旁路，
    让超出 worker 数的批量条目由线程池排队。
    """
    if getattr(_batch_ctx, "bypass_guard", False):
        return
    with _tasks_lock:
        active = _active_task_count_locked()
    if active >= _MAX_WORKERS:
        raise _concurrency_error(active)


def _submit_registered_task(task_id: str, cancel_event: threading.Event, **params) -> Future:
    """原子完成任务 admission、线程池提交和注册表登记。

    普通任务在锁内预占名额，防止两个调用线程同时通过门禁；batch 任务由
    thread-local 旁路标志控制，不预占并发名额，以保留「提交全部、由线程池排队」
    的批量语义。
    """
    bypass_guard = bool(getattr(_batch_ctx, "bypass_guard", False))
    with _tasks_lock:
        if not bypass_guard:
            active = _active_task_count_locked()
            if active >= _MAX_WORKERS:
                raise _concurrency_error(active)

        _task_events[task_id] = cancel_event
        try:
            future = _pool.submit(_run_note_task, task_id, cancel_event, **params)
        except Exception:
            _task_events.pop(task_id, None)
            raise

        try:
            setattr(future, _CONCURRENCY_BATCH_ATTR, bypass_guard)
            setattr(future, _CONCURRENCY_RESERVED_ATTR, not bypass_guard)
            # 自定义/测试 executor 可能同步返回已完成 Future；此时没有 worker
            # finally 可负责释放 reservation，直接视为已释放。真实线程池 Future
            # 在 callable 的 finally 完成前不会 done。
            if future.done() is True:
                setattr(future, _CONCURRENCY_RESERVED_ATTR, False)
        except Exception:
            # Future 是 concurrent.futures.Future（带 __dict__）；若第三方替身
            # 禁止属性写入，仍不阻断任务提交，running() 兼容计数会兜底。
            pass
        _task_futures[task_id] = future
        return future


def _rollback_unsubmitted_task(task_id: str, original_error: Optional[BaseException] = None) -> Dict[str, Any]:
    """撤销已创建但尚未可靠提交到线程池的任务。

    ``generate_note`` / ``prepare_note_material`` 会先创建任务目录、状态文件和
    全局索引，再提交后台 Future。若 admission、参数准备或线程池提交失败，不能
    把这些半成品留给 ``list_tasks`` / ``task``，否则会形成永远排队的幽灵任务。
    清理本身必须是尽力而为，绝不能覆盖触发回滚的原始异常；返回并记录结构化
    清理诊断，必要时附加到原始异常的 notes。
    """
    with _tasks_lock:
        future = _task_futures.pop(task_id, None)
        event = _task_events.pop(task_id, None)
        _status_memory.pop(task_id, None)

    if event is not None:
        try:
            event.set()
        except Exception:
            pass
    if future is not None:
        try:
            future.cancel()
        except Exception:
            pass

    try:
        diagnostics = cleanup_task_files(task_id, include_note=True)
        if not isinstance(diagnostics, dict):
            diagnostics = {
                "cleanup_error": f"清理返回了非字典诊断: {type(diagnostics).__name__}"
            }
    except Exception as exc:  # noqa: BLE001 —— 回滚不能覆盖原始异常
        diagnostics = {"cleanup_error": str(exc)}

    failures = bool(diagnostics.get("errors")) or any(
        diagnostics.get(key) for key in ("manifest_error", "index_error", "cleanup_error")
    )
    if failures:
        detail = json.dumps(diagnostics, ensure_ascii=False, default=str)
        logger.error("回滚未提交任务的清理不完整 task_id=%s: %s", task_id, detail)
        if original_error is not None:
            try:
                original_error.add_note(f"任务回滚清理诊断: {detail}")
            except Exception:
                pass
    return diagnostics


def _index_step_task(task_id: str, kind: str, title: str = "") -> None:
    """任务写入全局索引（generate_note / prepare_note_material / 步骤任务共用），让 list_tasks 能看见。

    名字保留「step」历史（曾是步骤任务专用）：#127 A1 后 note/material 提交时也先入索引。
    """
    try:
        from app.db.video_task_dao import insert_video_task

        insert_video_task(
            video_id=task_id,
            platform=kind,
            task_id=task_id,
            title=title or kind,
            status="PENDING",
            note_dir=str(NOTE_OUTPUT_DIR / task_id),
        )
    except Exception as exc:  # noqa: BLE001 —— 必需的生命周期索引失败时交给提交回滚处理
        logger.warning(f"任务入索引失败 task_id={task_id}: {sanitize_error_text(exc)}")
        raise


def _coerce_local_path(p: str) -> Path:
    """把 file:// URI 或普通路径规整为本地 Path（expanduser 展开 ~）。

    必须 unquote：Path.as_uri() 会把空格/非 ASCII 编码成 %20/百分号，不解码
    Path.exists() 永远 False（含空格/中文的文件会被静默判为不存在）。
    Windows `file:///C:/x` 的 urlparse.path 是 `/C:/x`，要去掉多余前导斜杠。
    """
    s = str(p or "").strip()
    if s.startswith("file://"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(s)
        s = unquote(parsed.path or "")
        # Windows：/C:/Users/... → C:/Users/...
        if os.name == "nt" and len(s) >= 3 and s[0] == "/" and s[2] == ":":
            s = s[1:]
    return Path(s).expanduser()


def _local_video_exists(video_url: str) -> bool:
    """本地路径 / file:// 是否是存在的文件（generate_note / prepare_note_material 共用）。

    只认文件不认目录：空串会 coerce 成当前目录（Path("") == Path(".")），
    目录路径也应报「不是视频文件」而非「存在」（docs 审计 H 组）。
    """
    return _coerce_local_path(video_url).is_file()


def _external_paths_allowed() -> bool:
    """数据目录外路径开关（VIDEONOTE_ALLOW_EXTERNAL_PATHS，默认关，#142 A1）。

    MCP 工具面可被 Agent 以任意输入驱动：本地文件输入会经转写把内容原样返回给
    Agent（不可信内容/prompt 注入场景 = 本地文件外泄通道），输出目录可写到任意
    位置。默认只允许数据目录内路径，越界调用报错并指明放行开关；开关打开后回到
    「只提示不拦截」（docs/05 #45/#99 口径——放行即用户显式意图）。
    """
    return env_bool("VIDEONOTE_ALLOW_EXTERNAL_PATHS", False)


def _destructive_cleanup_allowed() -> bool:
    """全局清理 include_config/include_models 门禁（VIDEONOTE_ALLOW_DESTRUCTIVE_CLEANUP，默认关，#142 A1）。

    清理 config/（LLM key / cookie / 转写设置）与 models/（已下载模型）不可逆——
    被注入的 Agent 可直接清空用户凭据与模型。默认拒绝执行（dry_run 如实标注将
    拒绝），env 显式放行后仍受运行中任务 / 下载中模型 / 越界目录三层守卫约束。
    """
    return env_bool("VIDEONOTE_ALLOW_DESTRUCTIVE_CLEANUP", False)


def _inside_data_dir(p: Path) -> bool:
    """路径是否在数据目录内。resolve 跟随符号链接：目录内软链到外部的路径算外。"""
    try:
        return p.resolve().is_relative_to(DATA_DIR.resolve())
    except OSError:
        return False


def _guard_data_boundary(p: Path, what: str) -> None:
    """数据目录边界校验（#142 A1）：目录外路径默认拒绝，开关放行后仅告警。

    与 SSRF 守卫同构（默认收紧、显式放行）；报错消息带放行开关名，Agent 可转告用户。
    """
    if _external_paths_allowed():
        return
    if not _inside_data_dir(p):
        raise ValueError(
            f"{what} 必须在数据目录内（数据目录: {DATA_DIR}；收到: {p}）。"
            "为防止本地文件被误读/误写，默认只允许数据目录内的路径；"
            "确实需要时可设置 VIDEONOTE_ALLOW_EXTERNAL_PATHS=1"
            "（或插件设置 allow_external_paths）后重启 MCP"
        )


def _guard_remote_url(url: str, platform: str) -> None:
    """非本地平台入口统一 SSRF 校验（#133 A1）。

    #132 A1 只在 generic/youtube 下载器内部校验——显式传 platform=bilibili/
    kuaishou/douyin 时，下载器/短链解析直接对 URL 发出站请求（yt-dlp
    extract_info / requests.head），恶意/被注入的 agent 可打内网/云元数据
    （169.254.169.254）。本地路径已在上游分流（local 分支），不在此校验。
    """
    if platform != "local":
        assert_public_http_url(url)


def _resolve_default_provider_id() -> Optional[str]:
    """从 app_config / 已填 key 的供应商推断默认 provider_id。

    优先 `default_model:{id}` 已配置的供应商；否则取唯一一个有非空 key 的启用供应商。
    """
    cfg = get_app_config()
    keyed: List[str] = []
    try:
        rows = ProviderService.get_all_providers() or []
    except Exception:
        rows = []
    for row in rows:
        pid = row.get("id")
        if not pid:
            continue
        key = (row.get("api_key") or "").strip()
        if cfg.get(f"default_model:{pid}"):
            # 默认模型分支也必须校验 key：key 被清空/过期时不选中（否则跑到总结才 401）
            if key and "*" not in key:
                return pid
            continue
        if key and "*" not in key:
            keyed.append(pid)
    if len(keyed) == 1:
        return keyed[0]
    return None


_SENSITIVE_VIA_MCP = (
    "API key / Cookie / HF token 不能经 MCP 工具传入（会进对话上游）。"
    "请在本会话终端执行：`! videonote providers set <id> --api-key '...'` "
    "或 `! videonote login bilibili` / `! videonote setup`。"
)


def _detect_platform(url: str) -> str:
    """从 URL / 本地路径识别平台（与 pipeline.detect_platform 一致）。

    未知 URL 返回 `"generic"`（走 yt-dlp 通用提取）；空 url 仍 raise ValueError。
    yt-dlp 也失败时，任务层用 handoff 提示让 Agent 接手。
    """
    return pipeline.detect_platform(url)


_TRANSCRIPT_DEFAULT_SEGMENTS = 50

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _validate_task_id(task_id: str) -> str:
    """校验 task_id 是安全 token，防路径穿越。

    task_id 会被直接拼进 `NOTE_OUTPUT_DIR / task_id` 路径，`../evil` 之类
    可穿透数据目录读取/写入外部文件。server 生成的 task_id 是 uuid4 hex；
    这里收紧到字母数字 + `-`/`_`。
    """
    tid = str(task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(tid):
        raise ValueError(f"非法 task_id（只允许字母数字/下划线/连字符，最长 64）: {task_id!r}")
    return tid


# ---------- MCP 工具 ----------


@mcp.tool()
def generate_note(
    video_url: str,
    platform: Optional[str] = None,
    quality: str = "medium",
    provider_id: Optional[str] = None,
    model_name: Optional[str] = None,
    format: Optional[List[NoteFormat]] = None,
    style: Optional[Style] = None,
    screenshot: Optional[bool] = None,
    link: bool = False,
    video_understanding: Optional[bool] = None,
    video_interval: Optional[int] = None,
    grid_size: Optional[List[int]] = None,
    notes_dir: Optional[str] = None,
    extras: Optional[str] = None,
    include_comments: Optional[bool] = None,
    comments_limit: Optional[int] = None,
) -> str:
    """提交一个视频链接/本地文件，用配置 LLM 异步生成 AI Markdown 笔记。

    后备路径：当前对话 Agent 无法看图，或用户明确要求配置 LLM 时才用。
    默认路径是 prepare_note_material，由 Agent 自己写笔记。

    - video_url: 必填，B 站/YouTube/抖音/快手/小宇宙/小红书链接或本地文件路径；
    - platform: 可省略，自动识别；
    - quality: fast / medium / slow；
    - provider_id: LLM 供应商 id；省略时取 setup 已配默认模型的供应商，或唯一一个已填 key 的供应商；
    - model_name: 省略时取已配置的默认模型（setup 向导设置），否则取该供应商第一个可用模型；
    - format: 附加内容，如 ["toc","link","screenshot","summary"]；
    - style: 输出风格（minimal 精简/detailed 详细/academic 学术/tutorial 教程/xiaohongshu 小红书/life_journal 生活向/task_oriented 任务导向/business 商业风格/meeting_minutes 会议纪要）；不传时用 setup ③ 配置的默认（默认 detailed）；显式传入始终覆盖；
    - extras: 附加到 prompt 末尾的自定义指令（如自定义笔记风格要求）；内置风格用 style，自定义风格用 extras；
    - include_comments / comments_limit: 是否抓取 B 站弹幕+热门评论作为参考注入 prompt（仅 B 站视频生效）；不传时用 setup 默认（默认关 / 20 条）；显式传入始终覆盖；
    - video_understanding / video_interval / grid_size: 视频理解（需多模态模型）；不传时用 setup ③ 配置的默认（默认关 / 6s）；显式传入始终覆盖；
    - screenshot + format 含 "screenshot": 插入图片，产出便携笔记 note.md + Assets/（相对引用）；不传时用 setup ③ 配置的默认（默认关）；显式传入始终覆盖；
    - notes_dir: 便携笔记的输出目录（可选；缺省 VIDEONOTE_NOTES_DIR 环境变量，再缺省 note_results/{task_id}/；支持 file:// URI）。
      安全边界：数据目录内的笔记/本地视频路径始终允许；数据目录外默认拒绝（报错注明放行方式），
      设 VIDEONOTE_ALLOW_EXTERNAL_PATHS=1（或插件设置 allow_external_paths）后放行并只提示。

    转写素材来源（无需配置，自动优先）：平台官方字幕（YouTube/B 站人工+自动字幕、
    小宇宙官方文稿——需先 `! videonote login xiaoyuzhou`）总是先用；无字幕或获取失败才
    下载音轨走转写引擎（fast-whisper/groq 等）。小红书无官方字幕，走转写引擎，登录墙请先
    `! videonote login xiaohongshu`。因此 YouTube/小宇宙有官方字幕的内容不会走本地 Whisper。

    返回 {task_id, status, platform}。之后用 task(task_id) 轮询。SUCCESS 时 result.note_dir 指向 note.md 所在目录（{task_id}/gen/，
    指定 notes_dir 时另有 result.portable_note_dir 指向便携副本）。
    """
    # 先做平台检测/handoff/本地校验——handoff 和「本地文件不存在」不需要 provider
    # （docs 审计 H 组：此前 provider 解析在前，unsupported 链接也会先撞 provider 报错）
    if platform is None:
        platform = _detect_platform(video_url)
    if platform == "unsupported":
        # 仅显式传 platform="unsupported" 时触发 handoff（detect_platform 现在返回 "generic"）
        return json.dumps(pipeline.handoff_result(video_url), ensure_ascii=False)
    if platform == "local":
        if not _local_video_exists(video_url):
            raise ValueError(f"本地文件不存在: {video_url}")
        video_url = str(_coerce_local_path(video_url))
        # 本地文件入口边界（#142 A1）：默认只允许数据目录内文件，防本地文件外泄
        _guard_data_boundary(Path(video_url), "本地视频路径")
    # SSRF 入口校验（#133 A1）：显式 platform=bilibili/kuaishou/douyin 曾绕过
    # 下载器内部的 #132 A1 检查（url_parser 短链解析另已内置守卫）
    _guard_remote_url(video_url, platform)
    _check_style_and_format(style, format or [])
    _check_grid_size(grid_size)
    try:
        q = DownloadQuality(quality)
    except ValueError:
        raise ValueError(f"quality 必须为 fast / medium / slow，收到: {quality}")
    # 默认解析分支（#52）只返回已填 key 的供应商，无须重复校验；
    # 显式 provider_id 则校验 key 已填（#133 B1）——否则空 key 的内置行
    # （openai/groq seed）显式传入会在下载+转写全跑完后才在 SUMMARIZING 报
    # 「API Key 未配置」，浪费整轮流水线。与 _preflight_provider 的 key 口径
    # 一致（provider 存在 + key 非空 + 不含 `*`）；模型检查不在这里做——
    # model_name 可显式传入（health_check 工具无此参数，才连带检查模型）。
    _explicit_provider = bool(provider_id)
    if not provider_id:
        provider_id = _resolve_default_provider_id()
    if not provider_id:
        raise ValueError(
            "需要 provider_id：用 `! videonote providers list` 查看，或跑 `/videonote-setup` / "
            "`! videonote providers set <id> --api-key '...'` 配好默认供应商"
        )
    if _explicit_provider:
        try:
            _prow = ProviderService.get_provider_by_id(provider_id)
        except Exception as exc:
            raise ValueError(f"读取供应商失败: {sanitize_error_text(exc)}") from exc
        if not _prow:
            raise ValueError(f"供应商不存在: {provider_id}")
        _pkey = (_prow.get("api_key") or "").strip()
        if not _pkey or "*" in _pkey:
            raise ValueError(
                f"供应商 {provider_id} 的 key 为空：请用 `! videonote providers set {provider_id} --api-key '...'`"
                "（不要经 MCP 传 key）"
            )

    if not model_name:
        model_name = get_app_config().get(f"default_model:{provider_id}") or ""
    if not model_name:
        models = get_models_by_provider(provider_id)
        if models:
            model_name = models[0]["model_name"]
    if not model_name:
        raise ValueError(
            f"供应商 {provider_id} 还没有可用模型：请先 `! videonote providers test {provider_id}` 探测模型；"
            f"如需指定默认模型，追加 `--default <model>`"
        )

    # 视频理解默认：参数没传（None）时用 setup ③ 配置的默认（默认关 / 0→6s）；
    # 显式传 False/0/具体秒数仍是显式值，覆盖默认
    if video_understanding is None:
        video_understanding = _resolve_bool_config("video_understanding", "VIDEONOTE_VIDEO_UNDERSTANDING", False)
    if video_interval is None:
        video_interval = _resolve_int_config("video_interval", "VIDEONOTE_VIDEO_INTERVAL", 0)
    video_interval = _coerce_int(video_interval or 0, 0, clamp_min=0)  # 下限钳制，避免 0/负值进流水线

    # 弹幕/评论默认：参数没传（None）时用 setup 配置的默认（默认关 / 20 条）
    if include_comments is None:
        include_comments = _resolve_bool_config("include_comments", "VIDEONOTE_INCLUDE_COMMENTS", False)
    if comments_limit is None:
        comments_limit = _resolve_int_config("comments_limit", "VIDEONOTE_COMMENTS_LIMIT", 20)
    # 显式 comments_limit=0（配 include_comments=True 想限 0 条）不能被 `or 20` 吞掉（#130 A2）
    comments_limit = _coerce_int(comments_limit if comments_limit is not None else 20, 20, clamp_min=1)  # 下限钳制

    # 风格/截图默认：参数没传（None）时用 setup ③ 配置的默认（默认 detailed / 关）
    if style is None:
        style = get_app_config().get("default_style") or env_or("VIDEONOTE_DEFAULT_STYLE") or "detailed"
    if screenshot is None:
        screenshot = _resolve_bool_config("default_screenshot", "VIDEONOTE_DEFAULT_SCREENSHOT", False)

    # 并发检查、预占和提交在 _submit_registered_task 的同一把锁内完成。

    task_id = uuid.uuid4().hex
    try:
        # 提交时先入全局索引（#127 A1）：note 任务运行期/失败后 list_tasks 可见，
        # 不再每次 _write_status 刷「不在全局索引」warning；SUCCESS 时 _save_metadata 再更新 title
        _index_step_task(task_id, platform or "generic")
        _write_status(task_id, TaskStatus.PENDING, message="任务排队中")
        notes_dir_out = notes_dir or get_app_config().get("notes_dir") or os.environ.get("VIDEONOTE_NOTES_DIR") or None
        # 输出目录与输入文件同口径：file:// URI 先规整，否则 Path("file:///…") 会在 CWD 下建字面 `file:` 目录
        if notes_dir_out is not None:
            notes_dir_out = str(_coerce_local_path(notes_dir_out))
            # 输出目录边界（#142 A1）：数据目录外默认拒绝（原 #99 只告警，扫描判定不够）
            _guard_data_boundary(Path(notes_dir_out), "便携笔记输出目录（notes_dir）")
        # 开关放行后：便携笔记可写数据目录外（用户显式意图），只提示不拦截（docs/05 #45）
        if notes_dir_out and not Path(notes_dir_out).resolve().is_relative_to(DATA_DIR.resolve()):
            logger.warning("generate_note 便携笔记输出到数据目录外: %s", notes_dir_out)
        # 布尔开关并入 format 列表：screenshot/link=True 时自动追加对应 format 项
        # （否则 prompt 不注入标记指令 → LLM 不输出标记 → 视频白下载但笔记无图，#120）
        _format = list(format or [])
        if screenshot and "screenshot" not in _format:
            _format.append("screenshot")
        if link and "link" not in _format:
            _format.append("link")
        params = dict(
            video_url=video_url,
            platform=platform,
            quality=q,
            model_name=model_name,
            provider_id=provider_id,
            link=link,
            screenshot=screenshot,
            _format=_format,
            style=style,
            extras=extras,
            include_comments=include_comments,
            comments_limit=comments_limit,
            video_understanding=video_understanding,
            video_interval=video_interval,
            grid_size=grid_size or [],
            notes_dir=notes_dir_out,
        )
        cancel_event = threading.Event()
        _submit_registered_task(task_id, cancel_event, **params)
    except Exception as exc:
        _rollback_unsubmitted_task(task_id, original_error=exc)
        raise
    logger.info(f"已提交任务 task_id={task_id} platform={platform} model={model_name}")
    return json.dumps(
        {"task_id": task_id, "status": "PENDING", "platform": platform, "model_name": model_name},
        ensure_ascii=False,
    )


@mcp.tool()
def prepare_note_material(
    video_url: str,
    platform: Optional[str] = None,
    video_understanding: Optional[bool] = None,
    video_interval: Optional[int] = None,
    grid_size: Optional[List[int]] = None,
    include_comments: Optional[bool] = None,
    comments_limit: Optional[int] = None,
) -> str:
    """提交一个视频链接/本地文件，异步产出「素材包」：转写全文+分段、可选视频帧（file:// 图片）、
    可选 B 站弹幕/评论、音视频本地路径。不调用 LLM 总结，供 AGENT（Claude Code）读取素材自行写笔记。

    - video_url: 必填，B 站/YouTube/抖音/快手/小宇宙/小红书链接或本地文件路径；
    - platform: 可省略，自动识别；
    - video_understanding / video_interval / grid_size: 是否抽帧 + 截帧间隔（秒）+ 网格大小
      （如 [3,3]）；默认关（不抽帧）。开启后 result.frames 是持久化帧图片的 file:// 绝对路径；
    - include_comments / comments_limit: 是否抓取 B 站弹幕+热门评论（仅 B 站视频生效；默认关 / 20 条）。

    不需要配置 LLM 供应商/模型。返回 {task_id, status: PENDING, kind: material}。
    之后用 task(task_id) 轮询；SUCCESS 时 result 含
    {kind: material, title, transcript, frames, comments_danmaku, video_path, audio_path}。
    transcript 优先来自平台官方字幕（YouTube/B 站人工+自动字幕、小宇宙官方文稿），无字幕才走转写引擎。
    这是默认路径（当前对话 Agent 写笔记）。配置 LLM 后备请用 generate_note。
    """
    if platform is None:
        platform = _detect_platform(video_url)
    if platform == "unsupported":
        # 仅显式传 platform="unsupported" 时触发 handoff（detect_platform 现在返回 "generic"）
        return json.dumps(pipeline.handoff_result(video_url), ensure_ascii=False)
    if platform == "local":
        if not _local_video_exists(video_url):
            raise ValueError(f"本地文件不存在: {video_url}")
        video_url = str(_coerce_local_path(video_url))
        # 本地文件入口边界（#142 A1）：默认只允许数据目录内文件，防本地文件外泄
        _guard_data_boundary(Path(video_url), "本地视频路径")
    # SSRF 入口校验（#133 A1，与 generate_note 同口径）
    _guard_remote_url(video_url, platform)

    # 视频理解（抽帧）默认：参数没传（None）时用 setup ③ 配置的默认（默认关 / 0→6s）；
    # 显式传 False/0/具体秒数仍是显式值，覆盖默认
    if video_understanding is None:
        video_understanding = _resolve_bool_config("video_understanding", "VIDEONOTE_VIDEO_UNDERSTANDING", False)
    if video_interval is None:
        video_interval = _resolve_int_config("video_interval", "VIDEONOTE_VIDEO_INTERVAL", 0)
    video_interval = _coerce_int(video_interval or 0, 0, clamp_min=0)  # 下限钳制，避免 0/负值进流水线

    # 弹幕/评论默认：参数没传（None）时用 setup 配置的默认（默认关 / 20 条）
    if include_comments is None:
        include_comments = _resolve_bool_config("include_comments", "VIDEONOTE_INCLUDE_COMMENTS", False)
    if comments_limit is None:
        comments_limit = _resolve_int_config("comments_limit", "VIDEONOTE_COMMENTS_LIMIT", 20)
    # 显式 comments_limit=0 不能被 `or 20` 吞掉（#130 A2）
    comments_limit = _coerce_int(comments_limit if comments_limit is not None else 20, 20, clamp_min=1)  # 下限钳制

    # 并发检查、预占和提交在 _submit_registered_task 的同一把锁内完成。
    _check_grid_size(grid_size)

    task_id = uuid.uuid4().hex
    try:
        # 提交时先入全局索引（#127 A1）：material 任务运行期/失败后 list_tasks 可见
        _index_step_task(task_id, platform or "generic")
        _write_status(task_id, TaskStatus.PENDING, message="任务排队中")
        params = dict(
            video_url=video_url,
            platform=platform,
            material_only=True,
            include_comments=include_comments,
            comments_limit=comments_limit,
            video_understanding=video_understanding,
            video_interval=video_interval,
            grid_size=grid_size or [],
        )
        cancel_event = threading.Event()
        _submit_registered_task(task_id, cancel_event, **params)
    except Exception as exc:
        _rollback_unsubmitted_task(task_id, original_error=exc)
        raise
    logger.info(f"已提交素材任务 task_id={task_id} platform={platform}")
    return json.dumps(
        {"task_id": task_id, "status": "PENDING", "kind": "material", "platform": platform},
        ensure_ascii=False,
    )


def _stage_label(status: str) -> str:
    """状态枚举 → 人类可读阶段（Agent 轮询汇报用，如「转写中，已 3 分钟」）。

    未知状态原样返回（不抛错）；配合 task 状态分支的 stage 字段使用。
    """
    return {
        "PENDING": "排队中",
        "INITIALIZING": "准备中",
        "PARSING": "解析中",
        "DOWNLOADING": "下载中",
        "TRANSCRIBING": "转写中",
        "SUMMARIZING": "总结中",
        "FORMATTING": "格式化中",
        "SAVING": "保存中",
        "SUCCESS": "已完成",
        "FAILED": "失败",
        "CANCELLED": "已取消",
        "NOT_FOUND": "不存在",
        "UNKNOWN": "状态未知",
    }.get(status, status)


def _task_status(task_id: str) -> str:
    """查询任务进度（轻量快照）。SUCCESS 时 result 含 markdown / note_dir / title。

    默认**不含完整转写**——转写可能数万 token，一次调用就会撑爆 context。需要转写文本：
    用 `task(task_id, action="transcript")` 按需取（支持按段切片 / "all" 全文）。
    """
    task_id = _validate_task_id(task_id)
    task_dir = NOTE_OUTPUT_DIR / str(task_id)
    status_file = task_dir / "status.json"
    data = None
    status_file_exists = status_file.exists()
    if status_file_exists:
        try:
            data = _valid_status_data(json.loads(status_file.read_text(encoding="utf-8")))
        except Exception:
            data = None
        if data is None:
            # 终态快照在成功落盘后会主动弹出（#123 C9），此时无法安全推断任务是否
            # PENDING；优先回退有效内存快照，否则明确返回 UNKNOWN，而不是让 list/string
            # 等合法 JSON 根类型在下方调用 .get() 抛异常（#148）。
            data = _memory_status_snapshot(task_id)
            if data is None:
                data = {"status": "UNKNOWN", "message": "状态文件无效，无法确认任务终态"}
    else:
        # 缺失文件也先查快照（#127 A5）：_write_status 先写内存快照再写盘，若提交时
        # 首写就失败（磁盘满/只读）任务在跑但 status.json 不存在——直接报 NOT_FOUND
        # 会与 list_tasks 显示的 PENDING 矛盾。快照命中 → 任务在跑，返回快照内容。
        data = _memory_status_snapshot(task_id)
        if data is None:
            return json.dumps(
                {
                    "status": "NOT_FOUND",
                    "stage": "不存在",
                    "elapsed_secs": None,
                    "message": "任务不存在（id 拼错或已被 cleanup）。用 list_tasks 查看。",
                    "task_id": task_id,
                    "result": None,
                },
                ensure_ascii=False,
            )

    status = data["status"]
    started = data.get("started_at")
    try:
        elapsed = round(time.time() - float(started), 1) if started else None
    except (TypeError, ValueError):
        # started_at 损坏（旧版本/手工编辑）不影响状态查询（docs 审计 H 组）
        elapsed = None
    result = None
    result_error = None
    result_pending = False
    result_file = task_dir / "result.json"
    if status == "SUCCESS" and result_file.exists():
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
            if isinstance(result, dict) and isinstance(result.get("audio_meta"), dict):
                # 兼容旧任务：历史 result.json 可能含完整 yt-dlp raw_info。
                # 读取时投影而不改写磁盘，避免旧签名 URL 再经 MCP 返回。
                result["audio_meta"] = _public_audio_meta(result["audio_meta"])
            if result:
                # 轻量结果：默认剥掉完整转写/评论，避免一次工具调用灌入数十万 token
                # （prepare_note_material 的转写就是主产物，剥掉后状态查询对它只剩空壳；
                #  要全文走 task(action="transcript")，#138）
                kind = result.get("kind")
                if kind not in ("transcript", "material"):
                    result.pop("transcript", None)
                    result.pop("comments_danmaku", None)
            # raw（whisper 原始 API 响应）恒剥——不管转写保不保留，raw 都可能是数 MB
            tr = result.get("transcript")
            if isinstance(tr, dict):
                tr.pop("raw", None)
            if result and result.get("markdown"):
                # 便携笔记模式 markdown 里是 Assets/ 相对引用（相对 note_dir=gen/，见 G2）；
                # base_dir=note_dir 让 Agent 拿到的 markdown 图片可被直接 Read
                result["markdown"] = _absolutize_images(result["markdown"], base_dir=result.get("note_dir"))
            if result and "title" not in result:
                # 补语义标题（旧任务 result 可能无 title；从 audio_meta 兜底）
                am = result.get("audio_meta") or {}
                result["title"] = am.get("title") or ""
        except Exception as e:
            # result.json 损坏（写盘中断/磁盘故障）：status 是 SUCCESS 但内容不可读。
            # 与 status.json 的 #118 快照回退不同，result 无法重建——显式标 result_error，
            # 让 Agent 能区分「成功但内容不可读」与「任务不存在」（#124 A10）
            logger.error(f"读取结果文件失败 task_id={task_id}: {sanitize_error_text(e)}")
            result_error = f"结果文件读取失败（可能写盘中断）: {sanitize_error_text(e)}"
    elif status == "SUCCESS":
        # SUCCESS 但 result.json 不存在（结果尚未落盘/被手动删/旧版本任务）：
        # 不静默 result:null——Agent 无法区分「无结果」与「任务失败」（#125 C3）
        result_pending = True

    payload = {
        "status": status,
        "stage": _stage_label(status),
        "elapsed_secs": elapsed,
        "message": sanitize_error_text(data.get("message", "")),
        "task_id": task_id,
        "result": result,
    }
    if result_error:
        payload["result_error"] = result_error
    if result_pending:
        payload["result_pending"] = True
    return json.dumps(payload, ensure_ascii=False)



def _parse_segment_range(spec: str, total: int) -> tuple:
    """解析 segment_range 字符串为 [lo, hi)（0 基，hi 开区间），越界自动钳制。

    支持 "a-b"（a 起 b 止，含 a 不含 b）、"a-"（a 起到末尾）、"-b"（开头到 b）、
    "a"（单段）、"all"（全文）。空 → 前 _TRANSCRIPT_DEFAULT_SEGMENTS 段。
    """
    spec = (spec or "").strip()
    if spec.lower() == "all":
        return (0, total)
    if not spec:
        return (0, min(_TRANSCRIPT_DEFAULT_SEGMENTS, total))
    m = re.fullmatch(r"(\d*)\s*-\s*(\d*)|(\d+)", spec)
    if not m:
        raise ValueError(
            f"segment_range 非法: {spec!r}（支持 'a-b' / 'a-' / '-b' / 'a' / 'all' / 空=前 {_TRANSCRIPT_DEFAULT_SEGMENTS} 段）"
        )
    if m.group(3) is not None:  # 单段
        a = int(m.group(3))
        a = max(0, min(a, total))
        return (a, min(a + 1, total))
    a_str, b_str = m.group(1), m.group(2)
    a = int(a_str) if a_str else 0
    b = int(b_str) if b_str else total
    a = max(0, min(a, total))
    b = max(a, min(b, total))
    return (a, b)


def _load_task_transcript(task_id: str) -> Optional[dict]:
    """读任务的转写 dict（{language, full_text, segments}）；无转写/未成功返回 None。

    规范来源：gen/transcript.json（note.py 每次成功都会写）；缺失则退 result.json。
    供 task(action='transcript') 工具与 videonote://task/{id}/transcript Resource 共用（docs/05 #16）。
    只对 SUCCESS 任务返回（#122 A3）：运行中任务转写缓存可能已写（转写完成、
    LLM 总结前），FAILED 任务转写也常留档——docstring 承诺「未成功返回 None」，
    否则 Agent 把失败任务读到的转写当成功产出去向用户汇报。
    """
    task_dir = NOTE_OUTPUT_DIR / str(task_id)
    try:
        status = json.loads((task_dir / "status.json").read_text(encoding="utf-8")).get("status", "")
    except Exception:
        status = ""
    if status != "SUCCESS":
        return None
    cache = task_dir / "gen" / "transcript.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取转写缓存失败 task_id={task_id}: {sanitize_error_text(e)}")
    result_file = task_dir / "result.json"
    if result_file.exists():
        try:
            return json.loads(result_file.read_text(encoding="utf-8")).get("transcript")
        except Exception:
            return None
    return None


@mcp.resource(
    "videonote://task/{task_id}/transcript",
    title="任务转写文本",
    description="读取任务转写（带时间轴的纯文本，agent 可直接读）。任务未成功/无转写时返回错误说明。",
    mime_type="text/plain",
)
def transcript_resource(task_id: str) -> str:
    """MCP Resource：按任务读转写全文（带时间轴文本，非 JSON）。

    工具面收敛（docs/05 #16）：Agent 读转写走 Resource（文本直读），
    task 的 transcript 分支保留用于分段切片/结构化场景。
    """
    try:
        task_id = _validate_task_id(task_id)
    except Exception as e:
        return f"task_id 无效: {e}"
    transcript = _load_task_transcript(task_id)
    if not transcript:
        return f"该任务没有可读转写（{_transcript_unavailable_reason(task_id)}）"
    segments = transcript.get("segments") or []
    if not segments:
        return transcript.get("full_text") or ""
    lines = []
    for seg in segments:
        start = seg.get("start") or 0
        mm, ss = int(start // 60), int(start % 60)
        hh, mm = divmod(mm, 60)
        stamp = f"{hh:02d}:{mm:02d}:{ss:02d}" if hh else f"{mm:02d}:{ss:02d}"
        speaker = seg.get("speaker")
        prefix = f"[{speaker}] " if speaker else ""
        lines.append(f"{stamp} - {prefix}{seg.get('text', '').strip()}")
    return "\n".join(lines)


def _task_transcript(task_id: str, segment_range: str = "") -> str:
    """读取已完成任务的转写文本（不耗 LLM，从磁盘按需取，避免撑爆 context）。

    - `segment_range` 空（默认）：只返回前 50 段（meta.truncated=true 时用 `"50-"` / `"all"` 续取）；
    - `"all"`：全文；`"0-50"` / `"50-"` / `"150-200"` 按段切片。
    返回 `{task_id, ok, language, segments, full_text, meta:{total_segments,
    returned_segments, total_chars, returned_chars, truncated}}`。任务未成功/无转写时
    `ok:false`。"""
    task_id = _validate_task_id(task_id)

    # 规范来源：gen/transcript.json（note.py 每次成功都会写）；缺失则退 result.json
    transcript = _load_task_transcript(task_id)
    if not transcript:
        status = _read_task_status(task_id)
        return json.dumps(
            {
                "task_id": task_id,
                "ok": False,
                "status": status,
                "message": f"该任务没有可读转写（{_transcript_unavailable_reason(task_id)}）",
            },
            ensure_ascii=False,
        )

    segments = transcript.get("segments") or []
    language = transcript.get("language")
    full_text = transcript.get("full_text") or ""
    total = len(segments)

    try:
        lo, hi = _parse_segment_range(segment_range, total)
    except ValueError as e:
        return json.dumps(
            {"task_id": task_id, "ok": False, "status": _read_task_status(task_id), "message": sanitize_error_text(e)},
            ensure_ascii=False,
        )
    if (lo, hi) == (0, total):
        out_segments = segments
        out_text = full_text
    else:
        out_segments = segments[lo:hi]
        # 与全量 full_text（缓存，空格分隔）同一分隔符（#127 A8）：切片不再 \n 重拼，
        # Agent 对比「all」与「0-N」的字节数/分词时不失真
        out_text = " ".join(seg.get("text", "") for seg in out_segments)

    return json.dumps(
        {
            "task_id": task_id,
            "ok": True,
            "language": language,
            "segments": out_segments,
            "full_text": out_text,
            "meta": {
                "total_segments": total,
                "returned_segments": len(out_segments),
                "total_chars": len(full_text),
                "returned_chars": len(out_text),
                "truncated": (lo, hi) != (0, total),
            },
        },
        ensure_ascii=False,
    )


def _task_cancel(task_id: str) -> str:
    """取消一个进行中/排队的笔记生成任务（协作式：在下一阶段边界生效，LLM 总结时每 chunk 检查）。

    返回 {ok, task_id, status, message?}。
    """
    task_id = _validate_task_id(task_id)
    with _tasks_lock:
        future = _task_futures.get(task_id)
        event = _task_events.get(task_id)
    if event is None:
        return json.dumps(
            {"ok": False, "task_id": task_id, "status": "NOT_FOUND", "message": "任务不存在或已结束"},
            ensure_ascii=False,
        )
    # 排队中：future.cancel() 可释放 worker 槽；运行中：靠 event 协作式停止（下一阶段边界）
    cancelled = False
    if future is not None and not future.done():
        cancelled = future.cancel()
    event.set()
    if _status_is_terminal(task_id):
        # 任务恰好在取消时到达终态：终态已写，覆盖会变成「已取消且无结果」。
        # 措辞按终态区分（#122 A8）：FAILED 报「任务已失败」——此前一律
        # 「任务已完成」，Agent 收到误以为成功产出了笔记
        try:
            st = json.loads(
                (NOTE_OUTPUT_DIR / str(task_id) / "status.json").read_text(encoding="utf-8")
            ).get("status", "")
        except Exception:
            st = ""
        if st == "FAILED":
            return json.dumps(
                {"ok": True, "task_id": task_id, "status": "DONE",
                 "message": "任务已失败，无需取消"},
                ensure_ascii=False,
            )
        if st == "CANCELLED":
            return json.dumps(
                {"ok": True, "task_id": task_id, "status": "DONE",
                 "message": "任务已取消"},
                ensure_ascii=False,
            )
        logger.info(f"任务已进入终态，取消不生效 task_id={task_id}")
        return json.dumps(
            {"ok": True, "task_id": task_id, "status": "DONE",
             "message": "任务已完成，取消不生效"},
            ensure_ascii=False,
        )
    if cancelled:
        # 排队中（未启动）任务：worker 不会执行，状态由本函数收尾写（无并发写方）
        _write_status(task_id, TaskStatus.CANCELLED, message="任务已取消")
        with _tasks_lock:
            _task_futures.pop(task_id, None)
            _task_events.pop(task_id, None)
        logger.info(f"已取消排队任务 task_id={task_id}")
        return json.dumps({"ok": True, "task_id": task_id, "status": "CANCELLED"}, ensure_ascii=False)
    # 运行中任务：只发协作式取消信号，终态由 worker 在阶段边界收尾写（TaskCancelledError
    # → CANCELLED）。此前 cancel 直接写盘 CANCELLED——检查与写入之间 worker 完成时
    # 把刚写的 SUCCESS 覆盖成「已取消」，而 result.json 已有完整笔记（#121 C5）
    logger.info(f"已发送取消信号 task_id={task_id}")
    return json.dumps(
        {"ok": True, "task_id": task_id, "status": "CANCELLING",
         "message": "取消请求已发送，任务将在下一阶段边界停止"},
        ensure_ascii=False,
    )


@mcp.tool()
def task(
    task_id: str,
    action: Literal["status", "transcript", "cancel"] = "status",
    segment_range: str = "",
) -> str:
    """查询/取消单个任务（任务控制面，合并自 get_task_status / get_task_transcript / cancel_note，#138）。

    - action="status"（默认）：轻量状态快照——SUCCESS 时 result 含 markdown / note_dir / title。
      默认不含完整转写（数万 token 会撑爆 context）；需转写走 action="transcript"；
    - action="transcript"：按需分段读取已完成任务的转写（segment_range 空=前 50 段、
      "all"=全文、"50-"续取，meta.truncated 提示续取）；任务未成功/无转写时 ok:false；
    - action="cancel"：协作式取消进行中/排队任务（在下一阶段边界生效，LLM 总结每 chunk 检查）。

    segment_range 仅 transcript 分支生效，其余分支忽略。返回结构随 action 不同：
    status → {status, stage, elapsed_secs, message, task_id, result}；
    transcript → {task_id, ok, language, segments, full_text, meta}；
    cancel → {ok, task_id, status, message?}。
    """
    task_id = _validate_task_id(task_id)
    if action == "status":
        return _task_status(task_id)
    if action == "transcript":
        return _task_transcript(task_id, segment_range=segment_range)
    if action == "cancel":
        return _task_cancel(task_id)
    # schema Literal 已约束 action；直接调用/老客户端仍可能传非法值（#138 入口显式报错）
    raise ValueError(f"action 必须是 status / transcript / cancel，收到: {action!r}")


@mcp.tool()
def list_tasks(limit: Optional[int] = None, offset: int = 0) -> str:
    """列出任务（全局索引 video_tasks 表），按创建时间倒序。

    - limit: 可选，最多返回条数（缺省全部）；offset: 可选，跳过条数（配合 limit 分页）。
    返回 [{task_id, title, status, summary, platform, created_at, note_dir}]——
    Agent 据此枚举任务、按语义标题识别，无需预先知道 task_id。
    """
    from app.db.video_task_dao import list_tasks as _list

    offset = _coerce_int(offset or 0, 0, clamp_min=0)
    # limit==0 显式「取 0 条」→ 空列表（不再被钳成 1、误导「没有任务」的判断，#127 A6）
    if limit == 0:
        return json.dumps([], ensure_ascii=False)
    limit = _coerce_int(limit, 1, clamp_min=1) if limit is not None else None
    try:
        tasks = _list(limit=limit, offset=offset)
    except Exception as exc:
        raise ValueError(f"读取任务索引失败: {sanitize_error_text(exc)}") from exc
    return json.dumps(tasks, ensure_ascii=False)


@mcp.tool()
def cleanup(
    task_id: Optional[str] = None,
    dry_run: bool = False,
    include_note: bool = False,
    include_config: bool = False,
    include_models: bool = False,
) -> str:
    """清理任务产物（合并自 cleanup_note / cleanup_all，#138）。

    - task_id 非空 = 清理单个任务的中间产物（下载的视频/音频、转写、截图、临时文件、
      dl 目录等）：include_note=True 时连最终笔记一起删（含 manifest 记录 + 全局索引
      video_tasks 该任务记录，否则 list_tasks 出现 note_dir 悬空的任务）；
    - task_id 为空 = 全局清理（类似恢复出厂）：清空 note_results / static/screenshots /
      static/cover / covers / note_cache 与 video_tasks 全局索引；include_config=True 时连
      config/（LLM key / cookie / 转写设置）一起清；include_models=True 时连 models/
      （已下载模型）一起清；**logs/ 刻意不清**（#121 C3：MCP 进程持有日志文件 fd）。
      include_config / include_models 不可逆，**默认拒绝**：设 VIDEONOTE_ALLOW_DESTRUCTIVE_CLEANUP=1
      （或插件设置 allow_destructive_cleanup）后放行（dry_run 会标注「将拒绝清理」）。
    - dry_run=True：**先查后清**——单任务列出该任务占用的文件，全局预览
      would_clean/would_keep/running，都不删任何东西；确认后再去掉 dry_run 执行。

    安全红线（与合并前一致）：单任务仍在运行（或排队中）时拒绝；全局清理有进行中/
    排队任务时拒绝、include_models=True 且仍有模型后台下载时拒绝（删 models/ 会打断
    下载线程，#123 A1）。数据目录**外**的便携笔记副本（用户指定 notes_dir 时常见）
    不删除（沙箱红线），路径经 notes_kept_outside 列出，不会成为无人知晓的孤儿。
    全局清理的**所有**目标目录（note_results/static/covers/note_cache/config/models）
    落在数据根外（环境变量指向外部/符号链接到外部）时同样拒绝清理，路径经
    kept_outside 列出（#140，复扫 A1 修复）。

    参数冲突显式报错（避免静默忽略误导）：单任务模式传 include_config/include_models、
    全局模式传 include_note 都会 ValueError。
    """
    if task_id:
        if include_config or include_models:
            raise ValueError("include_config/include_models 仅全局清理生效（不传 task_id 时）")
        tid = _validate_task_id(task_id)
        if dry_run:
            data = list_task_files(tid)
            if isinstance(data, dict) and "ok" not in data:
                data["ok"] = True
            data["dry_run"] = True
            return json.dumps(data, ensure_ascii=False)
        with _tasks_lock:
            future = _task_futures.get(tid)
            if future is not None and not future.done():
                return json.dumps(
                    {
                        "ok": False,
                        "task_id": tid,
                        "error": "任务仍在运行（或排队中）：先 task(action='cancel') 取消，或等待终态后再清理",
                    },
                    ensure_ascii=False,
                )
        result = cleanup_task_files(tid, include_note=include_note)
        result["ok"] = True  # 与拒绝路径 {ok:false} 对称（#125 A11）
        return json.dumps(result, ensure_ascii=False)
    # ---- 全局清理 ----
    if include_note:
        raise ValueError("include_note 仅单任务清理生效（传 task_id 时）")
    # 破坏性清理门禁（#142 A1）：删 key/cookie/转写配置与模型不可逆，默认拒绝执行
    # （dry_run 走预览分支，只标注「将拒绝清理」，与数据根外标注同口径）
    destructive = _destructive_cleanup_allowed()
    if (include_config or include_models) and not destructive and not dry_run:
        return json.dumps(
            {
                "ok": False,
                "error": "删除配置/模型（include_config / include_models）不可逆，需显式授权："
                "设置 VIDEONOTE_ALLOW_DESTRUCTIVE_CLEANUP=1"
                "（或插件设置 allow_destructive_cleanup）后重试",
            },
            ensure_ascii=False,
        )
    with _tasks_lock:
        running = [tid for tid, f in _task_futures.items() if not f.done()]
    if dry_run:
        # 全部清理目标（含 base 五类）经 cleanup_targets_inside_data_root 判定——
        # 落在数据根外时 dry_run 如实标注「将拒绝清理」（#140 复扫 A1）
        inside = cleanup_targets_inside_data_root()
        would_clean: List = []
        would_keep = ["logs/（运行日志，刻意不清）"]
        for key, label in (
            ("note_results", "note_results/"),
            ("static/screenshots", "static/screenshots/"),
            ("static/cover", "static/cover/"),
            ("covers", "covers/"),
            ("note_cache", "note_cache/"),
        ):
            if inside[key]:
                would_clean.append(label)
            else:
                would_keep.append(f"{label}（数据根外，将拒绝清理）")
        would_clean.append("video_tasks 全局索引")
        if include_config:
            if inside["config"] and destructive:
                would_clean.append("config/（LLM key / cookie / 转写设置）")
            elif inside["config"]:
                would_keep.append("config/（需 VIDEONOTE_ALLOW_DESTRUCTIVE_CLEANUP=1，将拒绝清理）")
            else:
                would_keep.append("config/（数据根外，将拒绝清理）")
        if include_models:
            if inside["models"] and destructive:
                would_clean.append("models/（已下载模型）")
            elif inside["models"]:
                would_keep.append("models/（需 VIDEONOTE_ALLOW_DESTRUCTIVE_CLEANUP=1，将拒绝清理）")
            else:
                would_keep.append("models/（数据根外，将拒绝清理）")
        if not include_config:
            would_keep.append("config/")
        if not include_models:
            would_keep.append("models/")
        return json.dumps(
            {
                "ok": True,
                "dry_run": True,
                "running": len(running),
                "running_task_ids": running,
                "would_clean": would_clean,
                "would_keep": would_keep,
                "note": "dry_run 未删除任何文件；确认后去掉 dry_run 执行",
            },
            ensure_ascii=False,
        )
    if running:
        return json.dumps(
            {
                "ok": False,
                "running": len(running),
                "running_task_ids": running,
                "error": f"有 {len(running)} 个进行中/排队任务：先 task(action='cancel') 取消或等终态，再进行全局清理",
            },
            ensure_ascii=False,
        )
    if include_models:
        dl_keys = dl_state.downloading_keys()
        if dl_keys:
            return json.dumps(
                {
                    "ok": False,
                    "downloading_models": dl_keys,
                    "error": f"仍有 {len(dl_keys)} 个模型正在后台下载（{', '.join(dl_keys)}）："
                    "先等下载完成再清 models/，或等下载失败/结束后重试",
                },
                ensure_ascii=False,
            )
    result = cleanup_all_files(include_config=include_config, include_models=include_models)
    result["ok"] = True  # 与拒绝路径 {ok:false} 对称（#125 A11 同口径，C2）
    return json.dumps(result, ensure_ascii=False)


def _installed_plugin_version() -> Optional[str]:
    """读本机已装 videonote 插件的 version（marketplace 按 git commit 缓存目录）。

    多个缓存版本时取最近安装的（mtime 最大）；读不到返回 None（非插件方式安装）。
    """
    import glob

    pattern = os.path.expanduser(
        "~/.claude/plugins/cache/videonote/videonote/*/.claude-plugin/plugin.json"
    )
    try:
        hits = sorted(
            glob.glob(pattern),
            key=lambda f: Path(f).stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None
    for p in hits:
        try:
            data = json.loads(Path(p).read_text(encoding="utf-8"))
            if data.get("version"):
                return str(data["version"])
        except Exception:
            continue
    return None


@mcp.tool()
def health_check(
    need_provider: bool = False,
    provider_id: Optional[str] = None,
    url: str = "",
    platform: Optional[str] = None,
) -> str:
    """体检（合并自 health_check / preflight，#138）：检查 MCP 运行环境与提交前就绪状态。

    检查项（checks 数组）：ffmpeg、数据库、磁盘剩余、转写器就绪（本地模型已下载/云端 key）、
    供应商 key 与模型（仅 need_provider=True）、任务队列；url 非空时顺带预解析视频时长
    （仅参考，不拦截），返回额外 duration_secs。

    返回 {ok, server_version, plugin_version, whisper_models, engine_advice,
    audio_enhance, keyed_providers, queue_length, max_workers, data_dir, skill_refresh,
    checks: [{name, ok, detail}], duration_secs?}。ok=false 时先解决 detail 里的问题
    再提交，避免长任务跑到半路才因模型未下载 / 磁盘满失败。

    - need_provider: 默认 False（#151：默认路径是 Agent 写笔记，不需要配置 LLM）。
      走 generate_note / batch_generate_notes 后备时传 True，才检查供应商 key/模型。
    """
    checks: List[Dict[str, Any]] = []

    ffmpeg_path = shutil.which("ffmpeg")
    checks.append(
        {
            "name": "ffmpeg",
            "ok": ffmpeg_path is not None,
            "detail": ffmpeg_path or "未找到 ffmpeg（视频下载/合并可能失败）",
        }
    )

    db_ok, db_err = True, ""
    try:
        with get_engine().connect():
            pass
    except Exception as e:
        db_ok, db_err = False, sanitize_error_text(e)
    checks.append(
        {"name": "db", "ok": db_ok, "detail": "" if db_ok else f"error: {db_err}"}
    )

    # 加密状态（#140 复扫 A4）：Fernet 不可用时拒绝敏感值写入——如实暴露，便于排障
    from videonote_mcp.crypto import encrypt_status as _crypto_status

    _crypto = _crypto_status()
    checks.append(
        {
            "name": "encryption",
            "ok": _crypto == "fernet",
            "detail": (
                "Fernet 加密正常"
                if _crypto == "fernet"
                else "Fernet key 创建/加密失败，已拒绝敏感值写入——检查 config/ 目录可写性后重试"
            ),
        }
    )

    try:
        usage = shutil.disk_usage(str(DATA_DIR))
        free_gb = usage.free / (1024**3)
        checks.append(
            {
                "name": "disk",
                "ok": free_gb >= _PREFLIGHT_MIN_DISK_GB,
                "detail": f"{free_gb:.1f} GB 可用（最低要求 {_PREFLIGHT_MIN_DISK_GB} GB）",
            }
        )
    except OSError as e:
        checks.append({"name": "disk", "ok": False, "detail": f"无法读取磁盘信息: {sanitize_error_text(e)}"})

    ready = TranscriberConfigManager().is_model_ready()
    checks.append(
        {
            "name": "transcriber",
            "ok": bool(ready["ready"]),
            "detail": (
                f"{ready['transcriber_type']}/{ready['model_size']} 就绪"
                if ready["ready"]
                else f"{ready['transcriber_type']}: {ready['reason']}"
            ),
        }
    )

    if need_provider:
        p_ok, p_detail = _preflight_provider(provider_id)
        checks.append({"name": "provider", "ok": p_ok, "detail": p_detail})

    with _tasks_lock:
        running_list = [tid for tid, f in _task_futures.items() if f.running()]
        queued = len(_task_futures) - len(running_list)
    # 与 _guard_concurrency 同源（只算 running()）：排队任务不占名额（batch 语义），
    # 此前用 len(_task_futures) 会把「3 个排队」误报成已满（#115）
    queue_ok = len(running_list) < _MAX_WORKERS
    queue_detail = f"{len(running_list)}/{_MAX_WORKERS} 运行中"
    if queued:
        queue_detail += f"（另 {queued} 排队）"
    checks.append({
        "name": "queue",
        "ok": queue_ok,
        "detail": queue_detail
        + ("" if queue_ok else "（已满，请等任务完成或 task(action='cancel') 取消后再提交）"),
    })

    duration_secs: Optional[float] = None
    if url:
        try:
            from app.services.inspect import inspect_video as _inspect

            info = _inspect(url, platform=platform)
            if info.get("ok"):
                entries = info.get("entries") or []
                if entries:
                    duration_secs = entries[0].get("duration")
                if info.get("kind") == "multi":
                    checks.append(
                        {
                            "name": "duration",
                            "ok": True,
                            "detail": f"多集共 {info.get('total', len(entries))} 条（默认每集 prepare_note_material 由当前 Agent 写；后备 LLM 才用 batch_generate_notes；只要一集用对应 entries[].url）",
                        }
                    )
                else:
                    checks.append(
                        {"name": "duration", "ok": True, "detail": _fmt_duration(duration_secs)}
                    )
            else:
                checks.append(
                    {
                        "name": "duration",
                        "ok": True,
                        "detail": f"无法预解析时长（{info.get('error', '未知原因')}）；提交后任务内会重试",
                    }
                )
        except Exception as e:
            checks.append(
                {
                    "name": "duration",
                    "ok": True,
                    "detail": f"无法预解析时长（{sanitize_error_text(e)}）；提交后任务内会重试",
                }
            )

    # ---- 元信息（原 health_check 保留字段；扁平 ffmpeg/db/transcriber 已并入 checks[]，#138）----
    cfg = TranscriberConfigManager().get_config()
    # 模型列表与 list_transcriber_models 同源（#128 A6）：遍历 registry 的可见模型
    # 而非硬编码 6 档——否则自定义注册模型在 list 显示已下载、health 永远缺席，
    # 两工具给 Agent 互相矛盾的就绪信号
    from app.transcriber.whisper_models import get_registry

    # 每个模型行区分 downloaded / downloading / failed(+error) / missing：
    # 首次下载大模型被超时/断网打断时，failed 原因不再被吞（docs/05 #34）。
    # 保持历史字段名 size/downloaded，新增 downloading/failed/error（向后兼容）。
    models = [
        {
            **dl_state.status_row(s, check_whisper_model_exists(s, "whisper"), key=s),
            "size": s,
        }
        for s in get_registry().visible_model_names()
    ]
    # 引擎建议：fast-whisper 配 tiny/base 对中文/长视频质量不足（docs/05 #34/#39）。
    # 不做语言自动切换（见 docs/05 #39 评估结论），改为显式提示。
    engine_advice = ""
    if cfg.get("transcriber_type") == "fast-whisper" and cfg.get(
        "whisper_model_size"
    ) in ("tiny", "base"):
        engine_advice = (
            "当前 fast-whisper 模型为 tiny/base，中文内容质量一般。"
            "建议 `! videonote transcriber set --size small`，"
            "或中文优先场景 `! videonote transcriber set --engine funasr`（需安装 funasr 依赖）。"
        )
    # 音频增强可选依赖就绪状态
    import importlib.util

    noisereduce_ok = importlib.util.find_spec("noisereduce") is not None
    pyannote_ok = importlib.util.find_spec("pyannote") is not None
    funasr_ok = importlib.util.find_spec("funasr") is not None
    mlx_ok = importlib.util.find_spec("mlx_whisper") is not None
    keyed_providers = 0
    try:
        for row in ProviderService.get_all_providers() or []:
            key = (row.get("api_key") or "").strip()
            if key and "*" not in key:
                keyed_providers += 1
    except Exception:
        keyed_providers = 0
    with _tasks_lock:
        queue_len = len(_task_futures)

    failed = [c for c in checks if not c["ok"]]
    payload = {
        "ok": not failed,
        "server_version": _SERVER_VERSION,
        "plugin_version": _installed_plugin_version(),
        "whisper_models": models,
        "engine_advice": engine_advice,
        "audio_enhance": {
            "enable_preprocess": bool(cfg.get("enable_preprocess")),
            "diarization": bool(cfg.get("diarization")),
            "noisereduce_installed": noisereduce_ok,
            "pyannote_installed": pyannote_ok,
            "funasr_installed": funasr_ok,
            "mlx_whisper_installed": mlx_ok,
        },
        "keyed_providers": keyed_providers,
        "queue_length": queue_len,
        "max_workers": _MAX_WORKERS,
        "data_dir": str(DATA_DIR),
        "skill_refresh": _skill_refresh_advice(),
        "checks": checks,
    }
    if url:
        payload["duration_secs"] = duration_secs
    return json.dumps(payload, ensure_ascii=False)


def _skill_refresh_advice() -> str:
    """插件/Skill 刷新提示（docs/05 #24）：server 与插件版本不一致时点名提示。"""
    base = (
        "MCP（启动命令 uvx videonote@latest）每次会话启动自动取 PyPI 最新版；"
        "Skill/插件不自动更新。"
        "工作流对不上时：`claude plugin disable videonote@videonote` "
        "然后 `claude plugin install videonote@videonote`，再开新会话。"
    )
    plugin_version = _installed_plugin_version()
    if plugin_version and plugin_version != _SERVER_VERSION:
        return (
            f"检测到插件版本 {plugin_version} 落后于 server {_SERVER_VERSION}：{base}"
        )
    return base


_PREFLIGHT_MIN_DISK_GB = 1.0


def _fmt_duration(secs: Optional[float]) -> str:
    """秒数 → 可读时长；None/0 → '未知'。"""
    if not secs or secs <= 0:
        return "未知"
    total = int(secs)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _preflight_provider(provider_id: Optional[str]) -> "tuple[bool, str]":
    """预检供应商：key 已填、模型可解析（与 generate_note 的解析逻辑一致）。"""
    pid = provider_id or _resolve_default_provider_id()
    if not pid:
        return False, "无已填 key 的供应商：先 add_provider 再 `! videonote providers set <id> --api-key '...'`，或跑 /videonote-setup"
    try:
        row = ProviderService.get_provider_by_id(pid)
    except Exception as exc:
        return False, f"读取供应商失败: {sanitize_error_text(exc)}"
    if not row:
        return False, f"供应商不存在: {pid}"
    key = (row.get("api_key") or "").strip()
    if not key or "*" in key:
        return False, f"供应商 {pid} 的 key 为空：`! videonote providers set {pid} --api-key '...'`（不要经 MCP 传 key）"
    model = get_app_config().get(f"default_model:{pid}") or ""
    if not model:
        rows = get_models_by_provider(pid) or []
        if rows:
            model = rows[0]["model_name"]
    if not model:
        return False, (
            f"供应商 {pid} 没有可用模型：先 `! videonote providers test {pid}` 探测模型；"
            "如需指定默认模型，追加 `--default <model>`"
        )
    return True, f"{pid}（key 已填，默认模型 {model}）"


@mcp.tool()
def inspect_video(url: str, platform: Optional[str] = None) -> str:
    """解析视频链接：识别平台 + 检查链接有效性 + 列出可独立生成笔记的条目。

    **只解析、不下载、不提交任务。** 提交前先用它确认链接（原 validate_url 的
    角色，#136 合并）：空 url / 本地文件不存在 / 内网 SSRF / 平台解析失败 →
    {ok: false, platform?, error}——generate_note 内部也会校验，这里提前给原因。
    generic（未知站点）会走 yt-dlp 展开确认（较慢，几秒）。

    单视频 {ok, platform, kind: single, title, video_id, total, entries}；
    多集（B 站分 P / YouTube 播放列表）kind: multi，entries[].url 可直接喂给
    `prepare_note_material`（默认：当前 Agent 写笔记）或 `generate_note`（后备 LLM）。
    只要一集 → 用对应 `entries[].url`。要全出：默认每集 `prepare_note_material`；
    仅当当前 Agent 无法看图或用户要求配置 LLM 时才一条 `batch_generate_notes`。
    互相独立的链接各开 subagent。

    返回 {ok, platform, kind: single|multi, title, video_id, current_p?,
    total, truncated, entries:[{p, title, duration, url, video_id}]}。
    超过 200 条截断（truncated=true）。失败 {ok:false, error}。
    """
    from app.services.inspect import inspect_video as _inspect

    return json.dumps(_inspect(url, platform=platform), ensure_ascii=False)


@mcp.tool()
def get_config(provider_id: str = "") -> str:
    """读取当前配置（只读，不做任何修改；敏感项一律不返回）。

    汇总返回：
    - app_config: setup 持久化的默认值（默认供应商/模型、风格、视频理解/弹幕开关、
      导出格式等，已过滤敏感键）；
    - providers: 已配置供应商（api_key 掩码）与默认供应商 id；
    - transcriber: 转写引擎配置与模型就绪状态；
    - cookie_configured: 已配置 Cookie 的平台名列表（只给布尔状态，不给值）；
    - transcript_source: 转写素材来源（固定 platform_subtitles_first——平台官方字幕优先，
      YouTube/B 站人工+自动字幕、小宇宙官方文稿可用时直接用官方字幕，无字幕/获取失败才下载音轨走转写引擎；
      小宇宙文稿需 `! videonote login xiaoyuzhou`；官方字幕通常比本地 Whisper 更准，且不耗转写引擎资源）；
    - note_cache: 跨任务转写缓存策略（ttl_days / max_mb / policy=sliding-lru）。

    传 provider_id 时额外对该供应商做连通性探测（用已保存的 key 请求
    /v1/models，15s 超时；**不接受 key 参数**），返回 {probe: {ok, models, error}}。

    配置修改一律走 CLI（MCP 面不提供写配置工具，凭证红线最干净）：
    `! videonote providers set <id> --api-key '...'` / `! videonote login bilibili` /
    `! videonote login xiaohongshu` / `! videonote transcriber set ...` / `! videonote transcriber download ...`
    """
    raw = get_app_config()
    _blocked = ("token", "cookie", "api_key", "secret", "password")
    safe = {k: v for k, v in raw.items() if not any(s in k.lower() for s in _blocked)}
    default_provider = _resolve_default_provider_id()
    if default_provider:
        safe["default_provider_id"] = default_provider
    mgr = TranscriberConfigManager()
    cfg = mgr.get_config()
    ready = mgr.is_model_ready()
    cookie_platforms = sorted(CookieConfigManager().list_all().keys())
    # providers：api_key 已掩码（get_all_providers_safe）；base_url 可能带 user:pass@
    # （中转站鉴权常见形式）——剥离凭据后再进 MCP 输出（#140 A5）
    providers = [
        {**p, "base_url": sanitize_url(p.get("base_url"))}
        for p in ProviderService.get_all_providers_safe()
    ]
    result = {
        "app_config": safe,
        "providers": providers,
        "transcriber": {
            **cfg,
            "ready": ready["ready"],
            "downloading": ready["downloading"],
            "reason": ready["reason"],
        },
        "cookie_configured": cookie_platforms,
        # 转写素材来源优先级：平台官方字幕（YouTube/B 站人工+自动字幕）总是优先，
        # 无字幕/获取失败才下载音轨走转写引擎（#C3 让 Agent 知道有官方字幕可用）。
        "transcript_source": "platform_subtitles_first",
        "note_cache": {
            "ttl_days": note_cache.cache_ttl_days(),
            "max_mb": note_cache.cache_max_mb(),
            "policy": "sliding-lru",
        },
    }
    if provider_id:
        provider = ProviderService.get_provider_by_id(provider_id)
        if not provider:
            raise ValueError(
                f"供应商不存在: {provider_id}（用 CLI `! videonote providers list` 查看）"
            )
        r = probe_models(
            provider.get("api_key"),
            provider.get("base_url"),
            name=provider.get("name", ""),
        )
        result["probe"] = {"ok": r["ok"], "provider_id": provider_id}
        if r["ok"]:
            result["probe"]["models"] = sorted(set(r["models"]))[:50]
        else:
            result["probe"]["error"] = sanitize_error_text(r.get("error", "连接失败"))
    return json.dumps(result, ensure_ascii=False, indent=2)


@mcp.tool()
def batch_generate_notes(
    video_url: str,
    max_entries: int = 10,
    platform: Optional[str] = None,
    quality: str = "medium",
    provider_id: Optional[str] = None,
    model_name: Optional[str] = None,
    format: Optional[List[NoteFormat]] = None,
    style: Optional[Style] = None,
    screenshot: Optional[bool] = None,
    extras: Optional[str] = None,
    link: bool = False,
    video_understanding: Optional[bool] = None,
    video_interval: Optional[int] = None,
    grid_size: Optional[List[int]] = None,
    include_comments: Optional[bool] = None,
    comments_limit: Optional[int] = None,
    notes_dir: Optional[str] = None,
) -> str:
    """对播放列表/合集/分 P 链接批量提交「配置 LLM」笔记任务（服务端逐个排队）。

    这是后备路径：当前对话 Agent 无法看图，或用户明确要求配置 LLM 时才用。
    默认路径是每集 `prepare_note_material`，由 Agent 自己写笔记。

    参数策略：除 video_url 外全部可选——不传即套 setup ③ 配置的默认
    （与 generate_note 同款；仅需覆盖时显式传）。每条任务同样优先用平台官方字幕
    （YouTube/B 站人工+自动字幕），无字幕才走转写引擎。

    - video_url: 必填，B 站分 P / YouTube 播放列表等可展开为多集的链接；
    - max_entries: 最多提交条数（默认 10，防 200 集播放列表一次全排；超出截断并标记 truncated）；
    - 其余参数与 generate_note 一致（quality/provider_id/model_name/style/format/screenshot/
      extras/link/video_understanding/video_interval/grid_size/include_comments/
      comments_limit/notes_dir），批量共享同一套风格与格式设置；单集链接退化为单个
      generate_note 任务。

    内部先 inspect_video 展开条目再逐条提交（单次最多 50 条；batch 显式绕过普通
    admission，由线程池负责排队；不要并发调用多个 batch）。与默认路径「每集
    prepare_note_material、Agent 自己写」不同——本工具把配置 LLM 的展开+排队
    收敛到服务端。
    返回 {ok, total, submitted, truncated?, errors:[{p, title, url, error}],
    tasks:[{p, title, duration, url, task_id, status}]}。
    单条失败不阻断其余；全部失败时 ok=false。之后逐个 task(task_id) 轮询。
    """
    from app.services.inspect import inspect_video as _inspect

    # 上界钳制：防止一条 MCP 调用内做上千次串行解析/排队（docs 审计 F7）。
    # #133 B8：max_entries=0 曾被 `or 10` 吞成提交 10 条——显式 0 与 list_tasks
    # 同口径（提交 0 条，仅展开）；负数钳到 0。
    max_entries = _coerce_int(max_entries if max_entries is not None else 10, 10, clamp_min=0)
    max_entries = min(max_entries, 50)  # 上界钳制
    parsed = _inspect(video_url, platform=platform)
    if not parsed.get("ok"):
        # inspect 失败归一为批量形状（#121 C6）：此前直接透传 inspect 的
        # {ok:false, platform, kind, error}——Agent 拿不到 total/submitted/tasks
        # 无法统一判读；工具文档只声明一种形状
        return json.dumps(
            {
                "ok": False,
                "total": 0,
                "submitted": 0,
                "errors": [{"p": None, "title": None, "url": sanitize_error_url(video_url), "error": sanitize_error_text(parsed.get("error") or "解析视频失败")}],
                "tasks": [],
                "platform": parsed.get("platform"),
                "kind": parsed.get("kind"),
            },
            ensure_ascii=False,
        )
    entries = list(parsed.get("entries") or [])
    plat = platform or parsed.get("platform")

    def _submit(entry: dict) -> dict:
        raw = generate_note(
            entry["url"],
            platform=plat,
            quality=quality,
            provider_id=provider_id,
            model_name=model_name,
            format=format,
            style=style,
            screenshot=screenshot,
            extras=extras,
            link=link,
            video_understanding=video_understanding,
            video_interval=video_interval,
            grid_size=grid_size,
            include_comments=include_comments,
            comments_limit=comments_limit,
            notes_dir=notes_dir,
        )
        r = json.loads(raw)
        return {**entry, "task_id": r.get("task_id"), "status": r.get("status")}

    if parsed.get("kind") == "single" or not entries:
        # 显式 max_entries=0 与多集路径同口径：只完成解析，不提交任务。
        # 单集退化也不能绕过「最多 0 条」契约（#146 B4）。
        if max_entries == 0:
            total = int(parsed.get("total") or 1)
            return json.dumps(
                {
                    "ok": False,
                    "total": total,
                    "submitted": 0,
                    "truncated": total > 0,
                    "remaining": total,
                    "platform": parsed.get("platform"),
                    "kind": parsed.get("kind"),
                    "errors": [],
                    "tasks": [],
                },
                ensure_ascii=False,
            )
        # 单集链接：退化为单任务，不引入条目语义；失败与多条目同形状（#109：
        # 此前 raise 裸传，Agent 拿不到结构化 errors）
        try:
            _batch_ctx.bypass_guard = True  # 批量语义：排队由线程池承担（#121 C1）
            try:
                task = _submit({"p": 1, "title": parsed.get("title"), "url": video_url, "duration": None})
            finally:
                _batch_ctx.bypass_guard = False
        except Exception as exc:  # noqa: BLE001 —— 与多条目收集口径一致
            return json.dumps(
                {
                    "ok": False,
                    "total": 1,
                    "submitted": 0,
                    # 形状与多条目分支完全一致（truncated/remaining/platform/kind 齐），
                    # Agent 按一种形状解析，不再因单集分支缺键 KeyError（#124 A11）
                    "truncated": False,
                    "remaining": 0,
                    "platform": parsed.get("platform"),
                    "kind": parsed.get("kind"),
                    "errors": [{"p": 1, "title": parsed.get("title"), "url": sanitize_error_url(video_url), "error": sanitize_error_text(exc)}],
                    "tasks": [],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {"ok": True, "total": 1, "submitted": 1, "truncated": False, "remaining": 0,
             "platform": parsed.get("platform"), "kind": parsed.get("kind"),
             "errors": [], "tasks": [task]},
            ensure_ascii=False,
        )

    truncated = len(entries) > max_entries
    total = int(parsed.get("total") or len(entries))
    entries = entries[:max_entries]
    submitted, errors, tasks = 0, [], []
    _batch_ctx.bypass_guard = True  # 批量语义：超出 worker 数排队等待而非拒绝（#121 C1）
    try:
        for e in entries:
            try:
                tasks.append(_submit(e))
                submitted += 1
            except Exception as exc:  # noqa: BLE001 —— 单条失败收集继续
                errors.append(
                    {
                        "p": e.get("p"),
                        "title": e.get("title"),
                        "url": sanitize_error_url(e.get("url")),
                        "error": sanitize_error_text(exc),
                    }
                )
    finally:
        _batch_ctx.bypass_guard = False
    return json.dumps(
        {
            "ok": submitted > 0,
            "total": total,          # 解析出的真实总集数（截断前），配合 truncated/remaining 判断是否续跑
            "submitted": submitted,
            "truncated": truncated,
            "remaining": max(0, total - len(entries)),
            "errors": errors,
            "tasks": tasks,
        },
        ensure_ascii=False,
    )


def _export_transcript(
    task_id: str,
    formats: Optional[List[str]] = None,
    out_dir: Optional[str] = None,
) -> str:
    """把已完成任务的转写导出为纯格式文件（SRT/VTT/JSON），返回文件路径。

    - task_id: 必填，已完成任务的 task_id（generate_note / prepare_note_material 返回）；
    - formats: 可选，要导出的格式列表（srt/vtt/json），缺省取 setup 配置的「导出格式默认」；
    - out_dir: 可选，输出目录（缺省为 note_results/{task_id}/gen/；支持 file:// URI）。

    只做确定性机械渲染（时间轴换算），不调用 LLM。返回
    {task_id, formats: {fmt: "file://绝对路径"}, errors: {}}，供 Agent 直接 Read。
    """
    from videonote_mcp.export import FORMATS
    from videonote_mcp.export import export_transcript as _export

    task_id = _validate_task_id(task_id)

    if formats is None:
        formats = resolve_default_export_formats()
        if not formats:
            formats = ["srt"]
    if formats == []:
        # 显式空列表：零导出还报 ok:true 与 #122 A1「任何格式没导出必须 ok:false」矛盾
        # （CLI 版对 not written 显式退出报错）——入口显式报错，省略参数才走默认（#124 A8）
        raise ValueError("formats 为空列表，未导出任何格式（省略该参数以用默认格式）")
    if not isinstance(formats, list):
        raise ValueError(f"formats 必须是字符串列表（支持 {' / '.join(FORMATS)}），收到: {formats!r}")
    unknown = sorted({str(f) for f in formats if str(f) not in FORMATS})
    if unknown:
        # 未知格式曾只写 stderr 警告后静默丢弃——Agent 以为导出成功实际缺文件
        raise ValueError(f"formats 只支持 {' / '.join(FORMATS)}，收到未知格式: {unknown!r}")

    task_dir = NOTE_OUTPUT_DIR / str(task_id)
    if out_dir is not None:
        # 输出目录同输入文件：file:// URI 先规整，否则 Path("file:///…") 建字面 `file:` 目录（#107）
        out_dir = str(_coerce_local_path(out_dir))
    out = out_dir or str(task_dir / "gen")
    # 输出目录边界（#142 A1）：数据目录外默认拒绝；开关放行后回到「只提示不拦截」
    if out_dir:
        _guard_data_boundary(Path(out), "导出输出目录（out_dir）")
    if out_dir and not Path(out).resolve().is_relative_to(DATA_DIR.resolve()):
        logger.warning("process_media export 输出到数据目录外: %s", out)

    # SUCCESS 门禁（#126 C1）：转写在 SUMMARIZING 前已落盘缓存，运行中/FAILED 任务
    # 也能读出转写——不设门禁会与转写读取（task action="transcript"，#122 A3）同任务给
    # Agent 相反结论（export ok:true vs transcript ok:false）。与 #122 A3 同口径，
    # 非 SUCCESS 一律拒绝。
    # 位置：参数校验之后（可修复的参数错误优先报，见 #124 A8 契约）；读转写之前。
    # UNKNOWN（任务不存在/状态不可读）不在此拦截——转写读不到时由下方
    # 「找不到转写」路径带准确原因（"任务状态不可读"）报错，而不是误报「任务未成功」。
    status = _read_task_status(task_id)
    if status not in ("SUCCESS", "UNKNOWN"):
        return json.dumps(
            {
                "ok": False,
                "task_id": task_id,
                "error": f"任务未成功（当前状态 {status}）：只有 SUCCESS 任务能导出转写"
                f"（{_transcript_unavailable_reason(task_id)}）",
            },
            ensure_ascii=False,
        )
    # gen/transcript.json 是转写规范来源（#122 A2），result.json 兜底——
    # 与 _load_task_transcript / CLI export 同口径（#127 A3，此前 server 出口反了）
    cache = task_dir / "gen" / "transcript.json"
    transcript = None
    if cache.exists():
        try:
            transcript = json.loads(cache.read_text(encoding="utf-8"))
        except Exception:
            transcript = None
    if transcript is None:
        result_json = task_dir / "result.json"
        if result_json.exists():
            try:
                transcript = json.loads(result_json.read_text(encoding="utf-8")).get("transcript")
            except Exception:
                transcript = None
    if transcript is None:
        return json.dumps(
            {
                "ok": False,  # 与成功路径形状对齐（此前缺 ok，Agent 判读 KeyError，#121 C4）
                "task_id": task_id,
                "error": f"找不到任务 {task_id} 的转写结果（{_transcript_unavailable_reason(task_id)}）",
            },
            ensure_ascii=False,
        )
    written = _export(transcript, formats=formats, out_dir=out, task_id=task_id)
    errors = written.pop("_errors", {}) if isinstance(written, dict) else {}
    # 任一格式落盘失败 → ok:False（此前全部失败仍 ok:True + formats:{}，
    # Agent 以为导出成功实际零文件，与 CLI 版对 not written 显式退出的行为不一致，#122 A1）
    ok = not errors
    if errors:
        logger.warning(f"导出部分失败: {errors}")
    return json.dumps(
        {"ok": ok, "task_id": task_id, "formats": written, "errors": errors},
        ensure_ascii=False,
    )


def _merge_audio(files: List[str], out_dir: Optional[str] = None) -> str:
    """把多个音频/视频文件合并为一个 16kHz mono wav（FFmpeg concat）。

    - files: 必填，至少 2 个本地文件路径（mp3/wav/m4a/mp4 等，编码可不同——自动统一转 16kHz mono）；
    - out_dir: 可选，输出目录（缺省数据目录 note_results/merged/；支持 file:// URI），输出为 merged.wav。

    用途：多段录音/会议分段/多个本地视频拼成一段再转写。返回
    {ok, path: "file://绝对路径"} 或 {ok: false, error}。
    """
    from app.services.merge import merge_audio as _merge

    try:
        if out_dir is not None:
            # 输出目录同输入文件：file:// URI 先规整，否则 Path("file:///…") 建字面 `file:` 目录（#107）
            out_dir = str(_coerce_local_path(out_dir))
        out = out_dir or str(NOTE_OUTPUT_DIR / "merged")
        # 与 process_media 的本地路径处理同口径：file:// URI 先规整（app 层只认普通路径，
        # 直接传 file:// 会误报「文件不存在」）
        paths = [_coerce_local_path(f) for f in files]
        # 目录输入穿透（merge.py 用 os.path.exists 对目录为 True）会到 ffmpeg 深处才炸
        # 「转换失败」泛化错误——与 diarize 分支同口径入口 is_file（#109）
        not_files = [str(p) for p in paths if not p.is_file()]
        if not_files:
            return json.dumps(
                {"ok": False, "error": f"文件不存在或不是文件: {', '.join(not_files)}"},
                ensure_ascii=False,
            )
        # 输入/输出目录边界（#142 A1）：数据目录外默认拒绝（先报「不是文件/不存在」再报边界）
        for p in paths:
            _guard_data_boundary(p, "合并输入文件")
        if out_dir:
            _guard_data_boundary(Path(out), "合并输出目录（out_dir）")
        # 开关放行后：数据目录外输出只提示不拦截（用户显式意图，docs/05 #45）
        if out_dir and not Path(out_dir).resolve().is_relative_to(DATA_DIR.resolve()):
            logger.warning("merge_audio 输出到数据目录外: %s", out_dir)
        merged = _merge([str(p) for p in paths], out_dir=out)
        return json.dumps({"ok": True, "path": Path(merged).as_uri()}, ensure_ascii=False)
    except Exception as exc:
        logger.warning(f"merge_audio 失败: {sanitize_error_text(exc)}")
        return json.dumps({"ok": False, "error": sanitize_error_text(exc)}, ensure_ascii=False)


def _diarize_media(audio_file: str, num_speakers: Optional[int] = None) -> str:
    """对音频做说话人分离（pyannote，可选依赖），返回说话人时间段。

    - audio_file: 必填，本地音频/视频文件（自动归一化为 16kHz mono wav 再分离）；
    - num_speakers: 可选，说话人数提示（缺省自动检测）。

    **不要传 hf_token**（会进对话上游）。token 只从环境变量
    `HUGGINGFACE_HUB_TOKEN` 或 `! videonote setup` 写入的 app_config.hf_token 读取。
    传入非空 hf_token 会被 process_media 入口直接拒绝。
    """
    try:
        from app.services.diarization import diarize_audio
        from app.transcriber.audio_preprocess import (
            cleanup_preprocess_files,
            normalize_to_wav,
        )

        p = _coerce_local_path(audio_file)
        if not p.is_file():
            raise FileNotFoundError(f"本地文件不存在: {audio_file}")
        # 输入文件边界（#142 A1）：默认只允许数据目录内文件
        _guard_data_boundary(p, "说话人分离输入文件")
        wav = None
        try:
            # normalize 也进 try/finally：ffmpeg 失败会留下半成品 <原名>_16k.wav（#133 B4）
            wav = normalize_to_wav(str(p))
            turns = diarize_audio(wav, hf_token=None, num_speakers=num_speakers)
        finally:
            # 归一化产物写在源文件旁（audio_preprocess 缺省路径），用完即清——
            # 否则每次调用在用户目录永久残留 <原名>_16k.wav（小时级视频数百 MB，#121 C2）
            cleanup_preprocess_files(str(wav or p))
        return json.dumps(
            {"ok": True, "turns": turns, "num_speakers": len({t["speaker"] for t in turns})},
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.warning(f"diarize 分支失败: {sanitize_error_text(exc)}")
        return json.dumps({"ok": False, "error": sanitize_error_text(exc)}, ensure_ascii=False)


@mcp.tool()
def process_media(
    action: Literal["export", "merge", "diarize"] = "export",
    task_id: str = "",
    formats: Optional[List[str]] = None,
    out_dir: Optional[str] = None,
    files: Optional[List[str]] = None,
    audio_file: str = "",
    num_speakers: Optional[int] = None,
    hf_token: Optional[str] = None,
) -> str:
    """媒体/转写加工（合并自 export_transcript / merge_audio / diarize_media，#138）。

    - action="export"（默认）：把已完成任务的转写导出为纯格式文件（SRT/VTT/JSON，
      确定性机械渲染，不调 LLM）。需 task_id；formats 可选（srt/vtt/json，缺省取
      setup「导出格式默认」）；out_dir 可选（缺省 note_results/{task_id}/gen/）。
      返回 {ok, task_id, formats: {fmt: "file://绝对路径"}, errors: {}}，供 Agent 直接 Read；
    - action="merge"：把多个音频/视频文件合并为一个 16kHz mono wav（FFmpeg concat）。
      需 files（至少 2 个本地路径，编码可不同）；out_dir 可选（缺省 note_results/merged/）。
      同 generate_note 安全边界：本地文件/输出目录默认限数据目录内，数据目录外
      需 VIDEONOTE_ALLOW_EXTERNAL_PATHS=1 放行（#142 A1）。
      返回 {ok, path: "file://绝对路径"} 或 {ok: false, error}；
    - action="diarize"：对音频做说话人分离（pyannote，可选依赖），返回说话人时间段。
      需 audio_file（本地音频/视频文件，自动归一化）；num_speakers 可选（缺省自动检测）。
      返回 {ok, turns: [{speaker, start, end}], num_speakers} 或 {ok: false, error}。

    **不要传 hf_token**（会进对话上游）：token 只从环境变量 `HUGGINGFACE_HUB_TOKEN`
    或 `! videonote setup` 写入的 app_config.hf_token 读取；传入非空 hf_token 直接拒绝
    （与 action 无关——蜜罐参数，防切换 action 绕过凭证红线）。

    参数冲突显式报错：export 缺 task_id、merge 缺 files、diarize 缺 audio_file 各自
    ValueError；其余参数按 action 分支生效（如 formats 仅 export 用），不强制。
    """
    if hf_token:
        raise ValueError(_SENSITIVE_VIA_MCP)
    if action == "export":
        if not task_id:
            raise ValueError("action=export 需要 task_id（已完成任务的 id）")
        return _export_transcript(task_id, formats=formats, out_dir=out_dir)
    if action == "merge":
        if not files:
            raise ValueError("action=merge 需要 files（至少 2 个本地文件路径）")
        return _merge_audio(files, out_dir=out_dir)
    if action == "diarize":
        if not audio_file:
            raise ValueError("action=diarize 需要 audio_file（本地音频/视频文件路径）")
        return _diarize_media(audio_file, num_speakers=num_speakers)
    # schema Literal 已约束 action；直接调用/老客户端仍可能传非法值（#138 入口显式报错）
    raise ValueError(f"action 必须是 export / merge / diarize，收到: {action!r}")


# ---------- 入口 ----------


def main() -> None:
    """MCP server 入口。CLI（providers）由 videonote_mcp.cli:main 分发，本函数只跑 MCP stdio。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    init_db()
    logger.info(f"VideoNote-Mcp 启动 | 数据目录: {DATA_DIR}")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
