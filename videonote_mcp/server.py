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
from typing import Any, Callable, Dict, List, Literal, Optional

from videonote_mcp.config import env_bool, env_int, env_or, get_app_config, remove_app_config, setup_environment
from videonote_mcp import __version__ as _SERVER_VERSION

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
from app.exceptions.task import TaskCancelledError, check_cancel as _check_cancel
from videonote_mcp.provider_probe import probe_models

# vendored 核心流水线
from app.db.engine import get_engine
from app.db.init_db import init_db
from app.db.model_dao import get_models_by_provider, insert_model, delete_model as _dao_delete_model
from app.db.provider_dao import seed_default_providers
from app.enmus.note_enums import DownloadQuality
from app.enmus.task_status_enums import TaskStatus
from app.services import pipeline
from app.services.note import NOTE_OUTPUT_DIR, NoteGenerator
from app.services.provider import ProviderService
from app.services.transcriber_config_manager import TranscriberConfigManager
from app.transcriber import model_download_state as dl_state
from app.utils.logger import get_logger
from app.utils.model_status import check_whisper_model_exists
from app.utils.path_helper import get_model_dir
from app.utils.task_manifest import (
    cleanup_all_files,
    cleanup_task_files,
    get_task_paths,
    list_task_files,
    record_task_paths,
)

from mcp.server.fastmcp import FastMCP

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
    """
    if style is not None and style not in _STYLE_VALUES:
        raise ValueError(f"style 必须是 {' / '.join(_STYLE_VALUES)} 之一，收到: {style!r}")
    if formats:
        unknown = sorted(set(formats) - set(_FORMAT_VALUES))
        if unknown:
            raise ValueError(f"format 只支持 toc / link / screenshot / summary，收到: {unknown}")


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

# 确保数据库表存在（幂等，init_db 使用 create_all）；空库时预置内置供应商
# （openai/deepseek/qwen/groq/ollama…，固定 id + 正确 base_url + 空 key，用 update_provider 填 key）
init_db()
seed_default_providers()

WHISPER_MODEL_SIZES = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]

mcp = FastMCP("videonote")

# ---------- 后台任务 ----------

_MAX_WORKERS = max(1, env_int("VIDEONOTE_MAX_WORKERS", 3))
_pool = ThreadPoolExecutor(max_workers=_MAX_WORKERS)
# 模型下载独立线程池：不占笔记任务 worker 槽位，也不被并发门禁计入进行中任务
_dl_pool = ThreadPoolExecutor(max_workers=1)

# 任务注册表：task_id -> (Future, cancel_event)，供 cancel_note 使用（thread-safe）
_tasks_lock = threading.Lock()
_task_futures: Dict[str, Future] = {}
_task_events: Dict[str, threading.Event] = {}


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


def _write_status(task_id: str, status, message: Optional[str] = None) -> None:
    """写入 {task_dir}/status.json（与上游 NoteGenerator._update_status 兼容）。"""
    data = {"status": status.value if isinstance(status, TaskStatus) else str(status)}
    if message:
        data["message"] = message
    # 首次提交时打时间戳（get_task_status 的 elapsed_secs 用）；后续 _update_status 会保留它
    data.setdefault("started_at", time.time())
    task_dir = NOTE_OUTPUT_DIR / str(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)
    f = task_dir / "status.json"
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(f)
    # 同步全局索引（尽力而为）
    try:
        from app.db.video_task_dao import update_task_status

        update_task_status(str(task_id), data["status"], message=message or "")
    except Exception:
        pass


def _atomic_write_json(path: Path, payload) -> None:
    """原子写 JSON（tmp + replace）：避免轮询读到半截文件（docs/05 #54）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _status_is_terminal(task_id: str) -> bool:
    """status.json 是否已到终态（SUCCESS/FAILED/CANCELLED）。"""
    try:
        data = json.loads(
            (NOTE_OUTPUT_DIR / str(task_id) / "status.json").read_text(encoding="utf-8")
        )
        return data.get("status") in ("SUCCESS", "FAILED", "CANCELLED")
    except Exception:
        return False


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


def _run_note_task(task_id: str, cancel_event: Optional[threading.Event] = None, **params) -> None:
    """在后台线程执行 NoteGenerator.generate，并落盘最终结果。"""
    try:
        _check_cancel(cancel_event)  # 排队期间被取消 → 直接 CANCELLED，不写 INITIALIZING
        _write_status(task_id, "INITIALIZING", message="正在准备…")
        generator = NoteGenerator()
        result = generator.generate(task_id=task_id, cancel_event=cancel_event, **params)
        if result is None:
            # generate() 内部已写 FAILED 状态
            return
        material = getattr(result, "material", None)
        if material:
            # material_only 模式：不产 markdown，payload 写素材包各字段
            # （markdown 为空字符串，get_task_status 的 absolutize 分支自动跳过）
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
                "audio_meta": asdict(result.audio_meta) if result.audio_meta else None,
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
        # result.json 写进任务文件夹（替代扁平 {task_id}.json）—— 原子写：
        # generate() 内部先写 SUCCESS，若轮询在 result 落盘前读到会看到半截 JSON
        _atomic_write_json(task_dir / "result.json", payload)
        # result.json 落盘完成后再补写一次 SUCCESS，把「SUCCESS 可见但结果未就绪」
        # 的窗口压缩到毫秒级（generate() 的 SUCCESS 在 result 写盘之前，见 #54）
        _write_status(task_id, TaskStatus.SUCCESS, message="完成")
        # 记录结果/状态/任务夹到 manifest（尽力而为，失败不阻断）
        record_task_paths(task_id, [
            task_dir,
            task_dir / "result.json",
            task_dir / "status.json",
        ])
        # 按「导出格式默认」自动导出纯格式（srt/vtt/json，确定性渲染）——尽力而为，失败不阻断
        _auto_export_transcript(task_id, payload.get("transcript"))
        logger.info(f"笔记生成成功 task_id={task_id}")
    except TaskCancelledError:
        logger.info(f"任务已取消 task_id={task_id}")
        _write_status(task_id, TaskStatus.CANCELLED, message="任务已取消")
    except Exception as e:
        logger.error(f"任务异常 task_id={task_id}: {e}", exc_info=True)
        _write_status(task_id, TaskStatus.FAILED, message=str(e))
    finally:
        with _tasks_lock:
            _task_futures.pop(task_id, None)
            _task_events.pop(task_id, None)


def _auto_export_transcript(task_id: str, transcript) -> None:
    """笔记任务成功后按 `default_export_formats` 自动导出纯格式（srt/vtt/json）。

    尽力而为：任何失败只记日志，不阻断主任务成功状态。只导出确定性机械格式，
    不涉及 LLM/网络；导出文件自动记入 manifest（供 cleanup_note 清理）。
    """
    try:
        from videonote_mcp.config import env_json_list, get_app_config
        from videonote_mcp.export import export_transcript

        default_formats = get_app_config().get("default_export_formats") or env_json_list("VIDEONOTE_DEFAULT_EXPORT_FORMATS", [])
        if not default_formats or not transcript:
            return
        export_transcript(
            transcript,
            formats=default_formats,
            out_dir=NOTE_OUTPUT_DIR / task_id / "gen",
            task_id=task_id,
        )
    except Exception as exc:
        logger.warning(f"自动导出失败 task_id={task_id}: {exc}")


def _guard_concurrency() -> None:
    """并发门禁：**正在执行**的任务数达到 VIDEONOTE_MAX_WORKERS（默认 3）时拒绝新提交。

    只统计 `future.running()`（已开始执行），排队的 future 不占名额 ——
    batch_generate_notes 的「超出 worker 数则排队等待」语义依赖此判定
    （docs 审计 F7；此前 `not f.done()` 把刚提交还在排队的任务也计入，
    批量 >3 条时第 4 条起全部被拒）。
    """
    with _tasks_lock:
        active = [tid for tid, f in _task_futures.items() if f.running()]
    if len(active) >= _MAX_WORKERS:
        raise ValueError(
            f"已有 {len(active)} 个任务在同时执行（上限 {_MAX_WORKERS}）：请先等其中一些完成"
            f"（或 cancel_note 取消）再提交。"
        )


def _run_step_task(
    task_id: str,
    cancel_event: Optional[threading.Event],
    step_fn: Callable,
    **kwargs,
) -> None:
    """通用后台步骤执行器：为独立流水线步骤（转写/抽帧/LLM 总结）提供统一生命周期。

    与 _run_note_task 同一套语义：排队期间被取消 → 直接 CANCELLED，不写 INITIALIZING；
    否则写 INITIALIZING → 执行 step_fn(task_id, cancel_event, **kwargs) 得到 dict payload →
    原子落盘 {task_id}/result.json + 记入 manifest → 成功。异常 → FAILED，取消 → CANCELLED，
    finally 从 _task_futures/_task_events 弹出（释放并发槽位）。
    """
    try:
        _check_cancel(cancel_event)  # 排队期间被取消 → 直接 CANCELLED，不写 INITIALIZING
        _write_status(task_id, "INITIALIZING", message="正在准备…")
        payload = step_fn(task_id, cancel_event, **kwargs)
        task_dir = NOTE_OUTPUT_DIR / str(task_id)
        task_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(task_dir / "result.json", payload)
        # 记录结果/状态 JSON 到 manifest（尽力而为，失败不阻断）
        record_task_paths(task_id, [
            task_dir,
            task_dir / "result.json",
            task_dir / "status.json",
        ])
        _write_status(task_id, TaskStatus.SUCCESS, message="完成")
        logger.info(f"步骤任务成功 task_id={task_id}")
    except TaskCancelledError:
        logger.info(f"任务已取消 task_id={task_id}")
        _write_status(task_id, TaskStatus.CANCELLED, message="任务已取消")
    except Exception as e:
        logger.error(f"步骤任务异常 task_id={task_id}: {e}", exc_info=True)
        _write_status(task_id, TaskStatus.FAILED, message=str(e))
    finally:
        with _tasks_lock:
            _task_futures.pop(task_id, None)
            _task_events.pop(task_id, None)


def _index_step_task(task_id: str, kind: str, title: str = "") -> None:
    """步骤任务写入全局索引，让 list_tasks 能看见。"""
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
    except Exception:
        pass


def _submit_step_task(kind: str, step_fn: Callable, title: str = "", **params) -> str:
    """并发门禁 + 写 PENDING + 入索引 + 提交线程池。"""
    _guard_concurrency()
    task_id = uuid.uuid4().hex
    _write_status(task_id, TaskStatus.PENDING, message="任务排队中")
    _index_step_task(task_id, kind, title=title)
    cancel_event = threading.Event()
    future = _pool.submit(_run_step_task, task_id, cancel_event, step_fn=step_fn, **params)
    with _tasks_lock:
        _task_futures[task_id] = future
        _task_events[task_id] = cancel_event
    return task_id


def _step_transcribe(task_id: str, cancel_event: Optional[threading.Event], file_path: str) -> dict:
    """transcribe_media 的后台步骤：转写 → payload {kind: transcript, transcript: {...}}。"""
    _check_cancel(cancel_event)
    transcript = pipeline.transcribe_audio(file_path)
    return {"kind": "transcript", "transcript": transcript}


def _step_extract_frames(
    task_id: str,
    cancel_event: Optional[threading.Event],
    video_path: str,
    video_interval: int,
    grid_size: Optional[List[int]],
) -> dict:
    """extract_frames 的后台步骤：抽帧 → payload {kind: frames, frames: [file://...]}。"""
    _check_cancel(cancel_event)
    frames = pipeline.extract_frames(
        video_path,
        video_interval=video_interval,
        grid_size=grid_size,
        save_dir=str(NOTE_OUTPUT_DIR / task_id / "gen" / "frames"),
    )
    return {"kind": "frames", "frames": frames}


def _step_summarize(
    task_id: str,
    cancel_event: Optional[threading.Event],
    material: dict,
    provider_id: str,
    model_name: Optional[str],
    style: Optional[Style],
    extras: Optional[str],
    formats: Optional[List[str]],
) -> dict:
    """summarize_note 的后台步骤：LLM 总结 → payload {kind: note, markdown, title}。"""
    _check_cancel(cancel_event)
    gpt = pipeline.get_gpt(provider_id, model_name)
    markdown = pipeline.summarize_material(
        material,
        gpt,
        style=style,
        extras=extras,
        formats=formats,
        checkpoint_key=task_id,
        cancel_event=cancel_event,
    )
    return {"kind": "note", "markdown": markdown, "title": material.get("title")}


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


def _coerce_transcript(transcript) -> dict:
    """把 transcript 参数规整成 dict（兼容 dict / JSON 字符串 / None）。"""
    if isinstance(transcript, str):
        try:
            return json.loads(transcript) or {}
        except Exception:
            return {}
    return transcript or {}


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


def _fetch_live_models(provider: Dict) -> Optional[List[str]]:
    """尝试实时请求供应商的 /v1/models 列表。失败返回 None。"""
    r = probe_models(
        provider.get("api_key"),
        provider.get("base_url"),
        name=provider.get("name", ""),
        timeout=15.0,
    )
    if not r["ok"]:
        logger.warning(f"实时拉取模型列表失败（回退到本地数据库）: {r['error']}")
        return None
    return r["models"]


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
    """提交一个视频链接/本地文件，异步生成 AI Markdown 笔记。

    - video_url: 必填，B 站/YouTube/抖音/快手链接或本地文件路径；
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
    - notes_dir: 便携笔记的输出目录（可选；缺省 VIDEONOTE_NOTES_DIR 环境变量，再缺省 note_results/{task_id}/）。

    返回 {task_id, status, platform}。之后用 get_task_status 轮询（不要用 wait_for_note，
    会卡住 MCP 事件循环）。SUCCESS 时 result.note_dir 指向 note.md 所在目录（{task_id}/gen/，
    指定 notes_dir 时另有 result.portable_note_dir 指向便携副本）。

    只需素材（转写/帧/评论，不调 LLM 总结）供自行写笔记时，用 prepare_note_material。
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
    _check_style_and_format(style, format or [])
    _check_grid_size(grid_size)
    if not provider_id:
        provider_id = _resolve_default_provider_id()
    if not provider_id:
        raise ValueError(
            "需要 provider_id：先 list_providers 查看，或跑 `/videonote-setup` / "
            "`! videonote providers set <id> --api-key '...'` 配好默认供应商"
        )

    try:
        q = DownloadQuality(quality)
    except ValueError:
        raise ValueError(f"quality 必须为 fast / medium / slow，收到: {quality}")

    if not model_name:
        model_name = get_app_config().get(f"default_model:{provider_id}") or ""
    if not model_name:
        models = get_models_by_provider(provider_id)
        if models:
            model_name = models[0]["model_name"]
    if not model_name:
        raise ValueError(
            f"供应商 {provider_id} 还没有可用模型：请先 list_models 查看，或 add_model 添加模型名"
        )

    # 视频理解默认：参数没传（None）时用 setup ③ 配置的默认（默认关 / 0→6s）；
    # 显式传 False/0/具体秒数仍是显式值，覆盖默认
    if video_understanding is None:
        video_understanding = bool(get_app_config().get("video_understanding", env_bool("VIDEONOTE_VIDEO_UNDERSTANDING", False)))
    if video_interval is None:
        video_interval = int(get_app_config().get("video_interval") or env_int("VIDEONOTE_VIDEO_INTERVAL", 0))
    video_interval = max(0, int(video_interval or 0))  # 下限钳制，避免 0/负值进流水线

    # 弹幕/评论默认：参数没传（None）时用 setup 配置的默认（默认关 / 20 条）
    if include_comments is None:
        include_comments = bool(get_app_config().get("include_comments", env_bool("VIDEONOTE_INCLUDE_COMMENTS", False)))
    if comments_limit is None:
        comments_limit = int(get_app_config().get("comments_limit") or env_int("VIDEONOTE_COMMENTS_LIMIT", 20))
    comments_limit = max(1, int(comments_limit or 20))  # 下限钳制

    # 风格/截图默认：参数没传（None）时用 setup ③ 配置的默认（默认 detailed / 关）
    if style is None:
        style = get_app_config().get("default_style") or env_or("VIDEONOTE_DEFAULT_STYLE") or "detailed"
    if screenshot is None:
        screenshot = bool(get_app_config().get("default_screenshot", env_bool("VIDEONOTE_DEFAULT_SCREENSHOT", False)))

    # 并发上限：最多 VIDEONOTE_MAX_WORKERS 个进行中任务（默认 3）
    _guard_concurrency()

    task_id = uuid.uuid4().hex
    _write_status(task_id, TaskStatus.PENDING, message="任务排队中")
    notes_dir_out = notes_dir or get_app_config().get("notes_dir") or os.environ.get("VIDEONOTE_NOTES_DIR") or None
    # 便携笔记可写数据目录外（用户显式意图），只提示不拦截（与 export/merge 同口径，docs/05 #45）
    if notes_dir_out and not Path(notes_dir_out).resolve().is_relative_to(DATA_DIR.resolve()):
        logger.warning("generate_note 便携笔记输出到数据目录外: %s", notes_dir_out)
    params = dict(
        video_url=video_url,
        platform=platform,
        quality=q,
        model_name=model_name,
        provider_id=provider_id,
        link=link,
        screenshot=screenshot,
        _format=format or [],
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
    future = _pool.submit(_run_note_task, task_id, cancel_event, **params)
    with _tasks_lock:
        _task_futures[task_id] = future
        _task_events[task_id] = cancel_event
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

    - video_url: 必填，B 站/YouTube/抖音/快手链接或本地文件路径；
    - platform: 可省略，自动识别；
    - video_understanding / video_interval / grid_size: 是否抽帧 + 截帧间隔（秒）+ 网格大小
      （如 [3,3]）；默认关（不抽帧）。开启后 result.frames 是持久化帧图片的 file:// 绝对路径；
    - include_comments / comments_limit: 是否抓取 B 站弹幕+热门评论（仅 B 站视频生效；默认关 / 20 条）。

    不需要配置 LLM 供应商/模型。返回 {task_id, status: PENDING, kind: material}。
    之后用 get_task_status 轮询（不要 wait_for_note）；SUCCESS 时 result 含
    {kind: material, title, transcript, frames, comments_danmaku, video_path, audio_path}。
    需要 AI 生成结构化 Markdown 笔记请用 generate_note。
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

    # 视频理解（抽帧）默认：参数没传（None）时用 setup ③ 配置的默认（默认关 / 0→6s）；
    # 显式传 False/0/具体秒数仍是显式值，覆盖默认
    if video_understanding is None:
        video_understanding = bool(get_app_config().get("video_understanding", env_bool("VIDEONOTE_VIDEO_UNDERSTANDING", False)))
    if video_interval is None:
        video_interval = int(get_app_config().get("video_interval") or env_int("VIDEONOTE_VIDEO_INTERVAL", 0))
    video_interval = max(0, int(video_interval or 0))  # 下限钳制，避免 0/负值进流水线

    # 弹幕/评论默认：参数没传（None）时用 setup 配置的默认（默认关 / 20 条）
    if include_comments is None:
        include_comments = bool(get_app_config().get("include_comments", env_bool("VIDEONOTE_INCLUDE_COMMENTS", False)))
    if comments_limit is None:
        comments_limit = int(get_app_config().get("comments_limit") or env_int("VIDEONOTE_COMMENTS_LIMIT", 20))
    comments_limit = max(1, int(comments_limit or 20))  # 下限钳制

    # 并发上限：与 generate_note 一致
    _check_grid_size(grid_size)
    _guard_concurrency()

    task_id = uuid.uuid4().hex
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
    future = _pool.submit(_run_note_task, task_id, cancel_event, **params)
    with _tasks_lock:
        _task_futures[task_id] = future
        _task_events[task_id] = cancel_event
    logger.info(f"已提交素材任务 task_id={task_id} platform={platform}")
    return json.dumps(
        {"task_id": task_id, "status": "PENDING", "kind": "material", "platform": platform},
        ensure_ascii=False,
    )


@mcp.tool()
def _stage_label(status: str) -> str:
    """状态枚举 → 人类可读阶段（Agent 轮询汇报用，如「转写中，已 3 分钟」）。

    未知状态原样返回（不抛错）；配合 get_task_status 的 stage 字段使用。
    """
    return {
        "PENDING": "排队中",
        "INITIALIZING": "准备中",
        "PARSING": "解析中",
        "DOWNLOADING": "下载中",
        "TRANSCRIBING": "转写中",
        "SUMMARIZING": "总结中",
        "SAVING": "保存中",
        "SUCCESS": "已完成",
        "FAILED": "失败",
        "CANCELLED": "已取消",
        "NOT_FOUND": "不存在",
    }.get(status, status)


@mcp.tool()
def get_task_status(task_id: str, include_transcript: bool = False) -> str:
    """查询笔记生成任务进度（轻量快照）。SUCCESS 时 result 含 markdown / note_dir / title。

    默认**不含完整转写**——转写可能数万 token，一次调用就会撑爆 context。需要转写文本：
    用 `get_task_transcript(task_id)` 按需取（支持按段切片）；或本调用传
    `include_transcript=True` 一次性拿全量（长视频慎用）。"""
    task_id = _validate_task_id(task_id)
    task_dir = NOTE_OUTPUT_DIR / str(task_id)
    status_file = task_dir / "status.json"
    if not status_file.exists():
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
    try:
        data = json.loads(status_file.read_text(encoding="utf-8"))
    except Exception:
        data = {"status": "PENDING", "message": "状态文件读取失败"}

    status = data.get("status", "PENDING")
    started = data.get("started_at")
    try:
        elapsed = round(time.time() - float(started), 1) if started else None
    except (TypeError, ValueError):
        # started_at 损坏（旧版本/手工编辑）不影响状态查询（docs 审计 H 组）
        elapsed = None
    result = None
    result_file = task_dir / "result.json"
    if status == "SUCCESS" and result_file.exists():
        try:
            result = json.loads(result_file.read_text(encoding="utf-8"))
            if result and not include_transcript:
                # 轻量结果：默认剥掉完整转写/评论，避免一次工具调用灌入数十万 token
                # （步骤任务除外——transcribe_media / prepare_note_material 的转写就是主产物，
                #  剥掉后 get_task_status 对它们只剩空壳；docs 审计 G3）
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
            logger.error(f"读取结果文件失败 task_id={task_id}: {e}")

    return json.dumps(
        {
            "status": status,
            "stage": _stage_label(status),
            "elapsed_secs": elapsed,
            "message": data.get("message", ""),
            "task_id": task_id,
            "result": result,
        },
        ensure_ascii=False,
    )


@mcp.tool()
def wait_for_note(task_id: str, timeout: int = 120, poll_interval: int = 3, include_transcript: bool = False) -> str:
    """**已废弃**：会卡住整个 MCP 事件循环。请用 `get_task_status` 轮询。

    本工具不再 sleep。终态（SUCCESS/FAILED/CANCELLED/NOT_FOUND）立刻返回快照；
    进行中任务也立刻返回当前快照，并带 deprecated 提示。timeout / poll_interval 已忽略。
    """
    task_id = _validate_task_id(task_id)
    resp = json.loads(get_task_status(task_id, include_transcript=include_transcript))
    if resp["status"] not in ("SUCCESS", "FAILED", "CANCELLED", "NOT_FOUND"):
        resp["deprecated"] = True
        resp["message"] = (
            (resp.get("message") or "")
            + " wait_for_note 已废弃（会阻塞事件循环），请改用 get_task_status 轮询。"
        ).strip()
    return json.dumps(resp, ensure_ascii=False)


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
    供 get_task_transcript 工具与 videonote://task/{id}/transcript Resource 共用（docs/05 #16）。
    """
    task_dir = NOTE_OUTPUT_DIR / str(task_id)
    cache = task_dir / "gen" / "transcript.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取转写缓存失败 task_id={task_id}: {e}")
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
    get_task_transcript 工具保留用于分段切片/结构化场景。
    """
    try:
        task_id = _validate_task_id(task_id)
    except Exception as e:
        return f"task_id 无效: {e}"
    transcript = _load_task_transcript(task_id)
    if not transcript:
        status = "UNKNOWN"
        try:
            st = json.loads((NOTE_OUTPUT_DIR / task_id / "status.json").read_text(encoding="utf-8"))
            status = st.get("status", "UNKNOWN")
        except Exception:
            pass
        return f"该任务没有可读转写（status={status}，尚未成功或已清理）"
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


@mcp.tool()
def get_task_transcript(task_id: str, segment_range: str = "") -> str:
    """读取已完成任务的转写文本（不耗 LLM，从磁盘按需取，避免撑爆 context）。

    - `segment_range` 空（默认）：只返回前 50 段（meta.truncated=true 时用 `"50-"` / `"all"` 续取）；
    - `"all"`：全文；`"0-50"` / `"50-"` / `"150-200"` 按段切片。
    返回 `{task_id, ok, language, segments, full_text, meta:{total_segments,
    returned_segments, total_chars, returned_chars, truncated}}`。任务未成功/无转写时
    `ok:false`。"""
    task_id = _validate_task_id(task_id)
    task_dir = NOTE_OUTPUT_DIR / str(task_id)

    # 规范来源：gen/transcript.json（note.py 每次成功都会写）；缺失则退 result.json
    transcript = _load_task_transcript(task_id)
    if not transcript:
        status = "UNKNOWN"
        try:
            st = json.loads((task_dir / "status.json").read_text(encoding="utf-8"))
            status = st.get("status", "UNKNOWN")
        except Exception:
            pass
        return json.dumps(
            {
                "task_id": task_id,
                "ok": False,
                "status": status,
                "message": "该任务没有可读转写（尚未成功或已清理）",
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
            {"task_id": task_id, "ok": False, "status": "UNKNOWN", "message": str(e)},
            ensure_ascii=False,
        )
    if (lo, hi) == (0, total):
        out_segments = segments
        out_text = full_text
    else:
        out_segments = segments[lo:hi]
        out_text = "\n".join(seg.get("text", "") for seg in out_segments)

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


@mcp.tool()
def cancel_note(task_id: str) -> str:
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
        # 任务恰好在取消时完成：终态（SUCCESS）已写，覆盖会变成「已取消且无结果」
        logger.info(f"任务已进入终态，取消不生效 task_id={task_id}")
        return json.dumps(
            {"ok": True, "task_id": task_id, "status": "DONE",
             "message": "任务已完成，取消不生效"},
            ensure_ascii=False,
        )
    _write_status(task_id, TaskStatus.CANCELLED, message="任务已取消")
    if cancelled:
        # 排队中（未启动）任务：_run_note_task/_run_step_task 不会执行，
        # finally 里的 pop 不跑 → 手动弹注册表，避免条目泄漏
        with _tasks_lock:
            _task_futures.pop(task_id, None)
            _task_events.pop(task_id, None)
    logger.info(f"已取消任务 task_id={task_id}")
    return json.dumps({"ok": True, "task_id": task_id, "status": "CANCELLED"}, ensure_ascii=False)


@mcp.tool()
def list_tasks() -> str:
    """列出全部任务（全局索引 video_tasks 表），按创建时间倒序。

    返回 [{task_id, title, status, summary, platform, created_at, note_dir}]——
    Agent 据此枚举任务、按语义标题识别，无需预先知道 task_id。
    """
    from app.db.video_task_dao import list_tasks as _list

    return json.dumps(_list(), ensure_ascii=False)


@mcp.tool()
def get_task_files(task_id: str) -> str:
    """列出某任务在磁盘上生成的相关文件/目录（manifest 记录 + 任务文件夹扫描）。

    返回 {task_id, manifest_paths, existing}，existing 是真实存在的文件/目录列表。
    清理前先用它查看该任务占了哪些存储。
    """
    task_id = _validate_task_id(task_id)
    return json.dumps(list_task_files(task_id), ensure_ascii=False)


@mcp.tool()
def cleanup_note(task_id: str, include_note: bool = False) -> str:
    """清理某个任务生成的中间产物（下载的视频/音频、转写、截图、临时文件、dl 目录等）。

    - include_note=False（默认）：保留最终笔记（note.md / note_dir / 便携笔记目录）；
    - include_note=True：连最终笔记一起删（含 manifest）。

    只删除 manifest 记录 / note_results/{task_id}* / dl_{task_id} 前缀的文件，
    且 resolve 校验在数据目录内（防路径穿越）。返回 {deleted, missing, errors, note_kept}。
    """
    task_id = _validate_task_id(task_id)
    return json.dumps(cleanup_task_files(task_id, include_note=include_note), ensure_ascii=False)


@mcp.tool()
def cleanup_all(include_config: bool = False, include_models: bool = False) -> str:
    """全局清理（类似恢复出厂）：清空 note_results / static/screenshots / logs 的所有任务产物。

    - include_config=False（默认）：保留 config/（LLM key / cookie / 转写设置）；
      include_config=True 时连 config/ 一起清；
    - include_models=False（默认）：保留 models/（已下载模型可复用，重下成本高）；
      include_models=True 时连 models/ 一起清。
    数据库记录（video_note.db）不动。返回各目录清理统计 + 保留项。
    """
    return json.dumps(cleanup_all_files(include_config=include_config, include_models=include_models), ensure_ascii=False)


@mcp.tool()
def fetch_comments(video_url: str, limit: int = 20) -> str:
    """抓取 B 站视频的热门评论（供生成笔记前预览/参考，不生成笔记）。

    返回 {ok, source, bvid, aid, comments: [{user, content, likes, ctime}], error}。
    可用 fetch_danmaku 看弹幕汇总；generate_note 的 include_comments 可把二者注入笔记 prompt。
    """
    try:
        from app.downloaders.bilibili_comment import BilibiliCommentFetcher

        limit = max(1, int(limit))  # limit<=0 会让 fetcher 的 `len(seen) >= limit` 恒真，静默返回空
        result = BilibiliCommentFetcher().fetch_comments(video_url, limit=limit)
    except Exception as exc:
        logger.warning(f"fetch_comments 失败: {exc}")
        result = {"ok": False, "source": "bilibili", "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
def fetch_danmaku(video_url: str) -> str:
    """抓取 B 站视频的弹幕汇总（供生成笔记前预览/参考，不生成笔记）。

    返回 {ok, source, bvid, cid, danmaku_summary, error}。
    可用 fetch_comments 看热门评论；generate_note 的 include_comments 可把二者注入笔记 prompt。
    """
    try:
        from app.downloaders.bilibili_comment import BilibiliCommentFetcher

        result = BilibiliCommentFetcher().fetch_danmaku(video_url)
    except Exception as exc:
        logger.warning(f"fetch_danmaku 失败: {exc}")
        result = {"ok": False, "source": "bilibili", "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


# ---------- 独立流水线步骤（复用 app/services/pipeline.py 的解耦步骤） ----------


@mcp.tool()
def fetch_subtitles(video_url: str, platform: Optional[str] = None) -> str:
    """只取平台字幕（人工/自动字幕），不下载音视频、不转写、不调 LLM。

    - video_url: 必填，B 站/YouTube/抖音/快手链接或本地文件路径；
    - platform: 可省略，自动识别。

    同步、快，适合快速预览字幕。成功返回
    {language, full_text, segments}（segments 每项含 start/end/text）；
    无字幕或获取失败返回 {ok: False, error}，不会抛异常。
    需要语音转写（ASR，把音频变成字幕）用 transcribe_media；
    需要完整 AI 笔记用 generate_note。
    """
    try:
        transcript = pipeline.fetch_subtitles(video_url, platform)
        if transcript is None:
            return json.dumps(
                {"ok": False, "error": "该视频没有可用平台字幕（人工/自动字幕）或获取失败"},
                ensure_ascii=False,
            )
        return json.dumps({"ok": True, **transcript}, ensure_ascii=False)
    except Exception as exc:
        logger.warning(f"fetch_subtitles 失败: {exc}")
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@mcp.tool()
def transcribe_media(file_path: str) -> str:
    """对本地音频/视频文件做语音识别（ASR），异步返回转写全文 + 分段。

    - file_path: 必填，本地文件路径（mp3/mp4/webm/wav/flac 等，需 ffmpeg 可解析）。

    不下载、不抓字幕、不调 LLM。后台执行（长音频可能较慢），立即返回
    {task_id, status: PENDING, kind: transcript}；用 get_task_status 轮询，
    SUCCESS 时 result 含 {kind: transcript, transcript: {language, full_text, segments}}。
    转写引擎由 get_transcriber_config / set_transcriber 配置（本地 fast-whisper 或云端 groq/bcut 等）。
    只要平台自带字幕（不转写）用 fetch_subtitles。
    """
    p = _coerce_local_path(file_path)
    if not p.is_file():
        raise ValueError(f"本地文件不存在: {file_path}")
    task_id = _submit_step_task("transcript", _step_transcribe, title=p.name, file_path=str(p))
    logger.info(f"已提交转写任务 task_id={task_id}")
    return json.dumps(
        {"task_id": task_id, "status": "PENDING", "kind": "transcript"}, ensure_ascii=False
    )


@mcp.tool()
def extract_frames(
    video_path: str,
    video_interval: int = 6,
    grid_size: Optional[List[int]] = None,
) -> str:
    """对本地视频按间隔抽关键帧（画面理解素材），异步返回持久化的帧图片 file:// 路径。

    - video_path: 必填，本地视频文件路径（mp4/mov/webm 等，需 ffmpeg 可解析）；
    - video_interval: 截帧间隔（秒），默认 6；
    - grid_size: 拼图网格尺寸（如 [3,3]），默认 [3,3]，会把截帧拼成网格图 + 单帧图。

    后台执行（较慢），立即返回 {task_id, status: PENDING, kind: frames}；
    用 get_task_status 轮询，SUCCESS 时 result 含
    {kind: frames, frames: [file://...]}；帧图片可被多模态模型 Read，或直接传给
    summarize_note 的 frames 参数参与笔记总结。
    """
    p = _coerce_local_path(video_path)
    if not p.is_file():
        raise ValueError(f"本地视频文件不存在: {video_path}")
    if grid_size is None:
        grid_size = [3, 3]
    try:
        interval = max(1, int(video_interval))
    except (TypeError, ValueError):
        interval = 6
    _check_grid_size(grid_size)
    task_id = _submit_step_task(
        "frames",
        _step_extract_frames,
        title=p.name,
        video_path=str(p),
        video_interval=interval,
        grid_size=grid_size,
    )
    logger.info(f"已提交抽帧任务 task_id={task_id}")
    return json.dumps(
        {"task_id": task_id, "status": "PENDING", "kind": "frames"}, ensure_ascii=False
    )


@mcp.tool()
def summarize_note(
    transcript: dict,
    frames: Optional[List[str]] = None,
    comments_danmaku: Optional[str] = None,
    title: Optional[str] = None,
    style: Optional[Style] = None,
    extras: Optional[str] = None,
    format: Optional[List[NoteFormat]] = None,
    provider_id: Optional[str] = None,
    model_name: Optional[str] = None,
) -> str:
    """用 LLM 把已有素材总结成 AI Markdown 笔记（不下载、不转写、不抽帧）。

    - transcript: 必填，转写结果 dict {language, full_text, segments}（transcribe_media /
      fetch_subtitles 的返回，或 prepare_note_material 的 result.transcript）；
    - frames: 可选，帧图片 file:// 路径列表（extract_frames 的返回），传了且模型多模态时参与总结；
    - comments_danmaku: 可选，B 站弹幕+评论参考文本（`fetch_comments` / `fetch_danmaku` 的返回）；
    - title: 可选，视频标题（默认空）；
    - style: 输出风格（minimal 精简/detailed 详细/academic 学术/tutorial 教程/xiaohongshu 小红书/
      life_journal 生活向/task_oriented 任务导向/business 商业风格/meeting_minutes 会议纪要）；
      不传时用 setup ③ 配置的默认（默认 detailed），显式传入始终覆盖；
    - extras: 附加到 prompt 末尾的自定义指令（自定义风格用 extras）；
    - format: 附加内容，如 ["toc","link","screenshot","summary"]；
    - provider_id: LLM 供应商 id；省略时取 setup 已配默认，或唯一一个已填 key 的供应商；
    - model_name: 省略时取已配置的默认模型（setup 向导设置），否则取该供应商第一个可用模型。

    后台执行（LLM 总结较慢），立即返回 {task_id, status: PENDING, kind: note}；
    用 get_task_status 轮询，SUCCESS 时 result 含
    {kind: note, markdown, title}。
    只想要素材（转写/帧/评论）自行写笔记时用 prepare_note_material；一步到位用 generate_note。
    """
    _check_style_and_format(style, format or [])
    if not provider_id:
        provider_id = _resolve_default_provider_id()
    if not provider_id:
        raise ValueError(
            "需要 provider_id：先 list_providers 查看，或跑 `/videonote-setup` / "
            "`! videonote providers set <id> --api-key '...'` 配好默认供应商"
        )
    if not model_name:
        model_name = get_app_config().get(f"default_model:{provider_id}") or ""
    if not model_name:
        models = get_models_by_provider(provider_id)
        if models:
            model_name = models[0]["model_name"]
    if not model_name:
        raise ValueError(
            f"供应商 {provider_id} 还没有可用模型：请先 list_models 查看，或 add_model 添加模型名"
        )
    if style is None:
        style = get_app_config().get("default_style") or env_or("VIDEONOTE_DEFAULT_STYLE") or "detailed"
    material = {
        "title": title,
        "transcript": _coerce_transcript(transcript),
        "frames": frames or [],
        "comments_danmaku": comments_danmaku,
        "video_path": None,
        "audio_path": None,
    }
    task_id = _submit_step_task(
        "note",
        _step_summarize,
        title=title or "note",
        material=material,
        provider_id=provider_id,
        model_name=model_name,
        style=style,
        extras=extras,
        formats=format or [],
    )
    logger.info(f"已提交总结任务 task_id={task_id} provider={provider_id} model={model_name}")
    return json.dumps(
        {"task_id": task_id, "status": "PENDING", "kind": "note"}, ensure_ascii=False
    )


@mcp.tool()
def list_providers() -> str:
    """列出已配置的 LLM 供应商（id、名称、类型、启用状态、api_key 掩码）。"""
    rows = ProviderService.get_all_providers_safe()
    return json.dumps(rows, ensure_ascii=False)


@mcp.tool()
def add_provider(name: str, api_key: str = "", base_url: str = "", type: str = "custom") -> str:
    """登记一个 LLM 供应商（**不要把 api_key 经本工具传入**，会进对话上游）。

    填 key 请在本会话终端执行：
    `! videonote providers add --name NAME --base-url URL --type custom`
    或对已有供应商 `! videonote providers set <id> --api-key '...'`。

    本工具只接受 name / base_url / type，api_key 必须留空；传了非空 key 会直接拒绝。
    """
    if api_key:
        raise ValueError(_SENSITIVE_VIA_MCP)
    if not name or not base_url:
        raise ValueError("name / base_url 必填；api_key 请走 CLI，不要经本工具传入")
    provider_id = ProviderService.add_provider(
        name=name, api_key="", base_url=base_url, logo="custom", type_=type
    )
    return json.dumps({"id": provider_id, "name": name}, ensure_ascii=False)


@mcp.tool()
def update_provider(
    provider_id: str,
    api_key: Optional[str] = None,
    name: Optional[str] = None,
    base_url: Optional[str] = None,
    enabled: Optional[int] = None,
) -> str:
    """更新 LLM 供应商配置（base_url / name / enabled 等非敏感字段）。

    **不要经本工具传 api_key**（会进对话上游）。填 key：
    `! videonote providers set <provider_id> --api-key '...'`。
    """
    if api_key is not None:
        raise ValueError(_SENSITIVE_VIA_MCP)
    data = {}
    if name is not None:
        data["name"] = name
    if base_url is not None:
        data["base_url"] = base_url
    if enabled is not None:
        data["enabled"] = enabled
    if not data:
        raise ValueError("至少提供 name / base_url / enabled 之一；api_key 请走 CLI")
    updated = ProviderService.update_provider(provider_id, data)
    if not updated:
        raise ValueError(f"更新失败：供应商 {provider_id} 不存在")
    return json.dumps({"updated": provider_id, "enabled": updated.get("enabled")}, ensure_ascii=False)


@mcp.tool()
def list_models(provider_id: str) -> str:
    """列出某 LLM 供应商可用的模型。

    优先实时请求供应商的 /v1/models 接口；接口不可用时回退到本地数据库已添加的模型。
    统一形状：{ok, source, models:[{id, name}, ...]}。
    """
    provider = ProviderService.get_provider_by_id(provider_id)
    if not provider:
        raise ValueError(f"供应商不存在: {provider_id}（先 add_provider 新增）")
    live = _fetch_live_models(provider)
    if live:
        models = [{"id": m, "name": m} for m in sorted(live)]
        return json.dumps({"ok": True, "source": "provider_api", "models": models}, ensure_ascii=False)
    db_models = get_models_by_provider(provider_id) or []
    models = []
    for row in db_models:
        if isinstance(row, dict):
            mid = row.get("model_name") or row.get("id") or row.get("name") or ""
        else:
            mid = str(row)
        if mid:
            models.append({"id": mid, "name": mid})
    return json.dumps({"ok": True, "source": "database", "models": models}, ensure_ascii=False)


@mcp.tool()
def add_model(provider_id: str, model_name: str) -> str:
    """手动把一个模型名添加为某供应商的可用模型（供应商 /v1/models 接口不可用时用）。"""
    if not model_name:
        raise ValueError("model_name 必填")
    insert_model(provider_id=provider_id, model_name=model_name)
    return json.dumps(
        {"added": True, "provider_id": provider_id, "model_name": model_name}, ensure_ascii=False
    )


@mcp.tool()
def delete_provider(provider_id: str) -> str:
    """删除一个 LLM 供应商配置（同时清掉它的 default_model 默认设置）。

    仅删除配置；不影响已生成的任务。删除前确认没有进行中的任务依赖它。
    """
    provider = ProviderService.get_provider_by_id(provider_id)
    if not provider:
        raise ValueError(f"供应商不存在: {provider_id}")
    ProviderService.delete_provider(provider_id)
    remove_app_config(f"default_model:{provider_id}")
    return json.dumps({"deleted": True, "id": provider_id, "name": provider.get("name")}, ensure_ascii=False)


@mcp.tool()
def delete_model(provider_id: str, model_name: str) -> str:
    """删除某供应商手动添加的模型名（list_models 里 database 来源的模型）。"""
    if not model_name:
        raise ValueError("model_name 必填")
    rows = get_models_by_provider(provider_id) or []
    target = next((r for r in rows if r.get("model_name") == model_name), None)
    if target is None:
        raise ValueError(f"模型不存在: {provider_id}/{model_name}（可 list_models 查看）")
    _dao_delete_model(target["id"])
    remove_app_config(f"default_model:{provider_id}")
    return json.dumps({"deleted": True, "provider_id": provider_id, "model_name": model_name}, ensure_ascii=False)


@mcp.tool()
def test_provider(provider_id: str) -> str:
    """测试供应商连接并列出可用模型。

    用已保存的 key 探测 /v1/models（**不接受 key 参数**，填 key 走 CLI）。
    """
    provider = ProviderService.get_provider_by_id(provider_id)
    if not provider:
        raise ValueError(f"供应商不存在: {provider_id}（先 add_provider 或 CLI providers add）")
    r = probe_models(
        provider.get("api_key"),
        provider.get("base_url"),
        name=provider.get("name", ""),
    )
    if not r["ok"]:
        return json.dumps(
            {"ok": False, "provider_id": provider_id, "error": r.get("error", "连接失败")},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": True,
            "provider_id": provider_id,
            "count": len(r["models"]),
            "models": sorted(set(r["models"]))[:50],
        },
        ensure_ascii=False,
    )


@mcp.tool()
def read_app_config() -> str:
    """读取 setup 持久化的应用配置默认值（非敏感）。

    Agent 在生成前可据此推断默认：默认供应商/模型（default_provider_id +
    default_model:{id}）、视频理解/弹幕开关、风格、导出格式、notes_dir 等。
    敏感项（hf_token / cookie / api_key 等）一律不返回。
    """
    raw = get_app_config()
    _blocked = ("token", "cookie", "api_key", "secret", "password")
    safe = {
        k: v for k, v in raw.items() if not any(s in k.lower() for s in _blocked)
    }
    default_provider = _resolve_default_provider_id()
    if default_provider:
        safe["default_provider_id"] = default_provider
    return json.dumps(safe, ensure_ascii=False, indent=2)


@mcp.tool()
def get_transcriber_config() -> str:
    """查看当前转写引擎配置（fast-whisper 本地 / groq / bcut / kuaishou / mlx-whisper 云端）与模型就绪状态。"""
    mgr = TranscriberConfigManager()
    cfg = mgr.get_config()
    ready = mgr.is_model_ready()
    return json.dumps(
        {
            **cfg,
            "ready": ready["ready"],
            "downloading": ready["downloading"],
            "reason": ready["reason"],
        },
        ensure_ascii=False,
    )


@mcp.tool()
def set_transcriber(
    transcriber_type: str,
    whisper_model_size: Optional[str] = None,
    enable_preprocess: Optional[bool] = None,
    diarization: Optional[bool] = None,
    diarization_speakers: Optional[int] = None,
) -> str:
    """切换转写引擎 / 音频增强配置。

    transcriber_type: fast-whisper（本地，需下载模型）/ groq（云端）/ bcut / kuaishou / mlx-whisper / funasr。
    - whisper_model_size: 切到 fast-whisper 时的模型尺寸；
    - enable_preprocess: 音频预处理开关（16kHz 归一 + 超长分块，默认关）；
    - diarization: 说话人分离开关（pyannote 可选，默认关）；
    - diarization_speakers: 说话人数提示（可选，自动检测时省略）。
    """
    mgr = TranscriberConfigManager()
    cfg = mgr.update_config(
        transcriber_type,
        whisper_model_size,
        enable_preprocess=enable_preprocess,
        diarization=diarization,
        diarization_speakers=diarization_speakers,
    )
    return json.dumps(cfg, ensure_ascii=False)


@mcp.tool()
def list_transcriber_models() -> str:
    """列出本地 whisper 模型（fast-whisper）的下载状态。"""
    rows = []
    for size in WHISPER_MODEL_SIZES:
        downloaded = check_whisper_model_exists(size, "whisper")
        state = dl_state.get_status(size) or ("done" if downloaded else "none")
        rows.append({"size": size, "downloaded": downloaded, "state": state})
    return json.dumps({"whisper_models": rows}, ensure_ascii=False)


@mcp.tool()
def download_transcriber_model(model_size: str, transcriber_type: str = "fast-whisper") -> str:
    """在后台下载 whisper 模型（仅本地引擎需要）。下载中/完成后用 list_transcriber_models 查询。"""
    if model_size not in WHISPER_MODEL_SIZES:
        raise ValueError(
            f"未知模型尺寸: {model_size}（可选: {', '.join(WHISPER_MODEL_SIZES)}）"
        )
    size = model_size.strip().lower()
    if transcriber_type == "fast-whisper":
        key = size

        def _dl():
            try:
                dl_state.mark_downloading(key)
                from app.transcriber.whisper_models import resolve_whisper_model
                from faster_whisper import WhisperModel

                target = resolve_whisper_model(size)
                WhisperModel(
                    model_size_or_path=target,
                    device="cpu",
                    compute_type="int8",
                    download_root=get_model_dir("whisper"),
                )
                dl_state.mark_done(key)
                logger.info(f"whisper 模型 {size} 下载完成")
            except Exception as e:
                dl_state.mark_failed(key, str(e))
                logger.error(f"whisper 模型 {size} 下载失败: {e}", exc_info=True)

        _dl_pool.submit(_dl)
        return json.dumps(
            {"started": True, "model_size": size, "transcriber_type": "fast-whisper"},
            ensure_ascii=False,
        )

    if transcriber_type == "mlx-whisper":
        if not (sys.platform == "darwin"):
            raise ValueError("mlx-whisper 仅在 macOS 可用，请改用 fast-whisper")

        def _dl_mlx():
            try:
                dl_state.mark_downloading(f"mlx-{size}")
                from app.transcriber.mlx_whisper_transcriber import MLX_MODEL_MAP
                from huggingface_hub import snapshot_download

                repo_id = MLX_MODEL_MAP.get(size)
                if not repo_id:
                    raise ValueError(f"未找到 mlx 模型映射: {size}")
                snapshot_download(
                    repo_id=repo_id,
                    local_dir=os.path.join(get_model_dir("mlx-whisper"), repo_id),
                )
                dl_state.mark_done(f"mlx-{size}")
            except Exception as e:
                dl_state.mark_failed(f"mlx-{size}", str(e))
                logger.error(f"mlx 模型 {size} 下载失败: {e}", exc_info=True)

        _dl_pool.submit(_dl_mlx)
        return json.dumps(
            {"started": True, "model_size": size, "transcriber_type": "mlx-whisper"},
            ensure_ascii=False,
        )

    raise ValueError(f"仅支持本地模型下载：fast-whisper / mlx-whisper，收到: {transcriber_type}")


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
def health_check() -> str:
    """检查 MCP 运行环境：版本、FFmpeg、数据库、转写器、可选依赖、队列长度。"""
    ffmpeg_ok = shutil.which("ffmpeg") is not None
    db_ok, db_err = True, ""
    try:
        with get_engine().connect():
            pass
    except Exception as e:
        db_ok, db_err = False, str(e)

    cfg = TranscriberConfigManager().get_config()
    ready = TranscriberConfigManager().is_model_ready()
    # 每个模型行区分 downloaded / downloading / failed(+error) / missing：
    # 首次下载大模型被超时/断网打断时，failed 原因不再被吞（docs/05 #34）。
    # 保持历史字段名 size/downloaded，新增 downloading/failed/error（向后兼容）。
    models = [
        {
            **dl_state.status_row(s, check_whisper_model_exists(s, "whisper"), key=s),
            "size": s,
        }
        for s in WHISPER_MODEL_SIZES
    ]
    # 引擎建议：fast-whisper 配 tiny/base 对中文/长视频质量不足（docs/05 #34/#39）。
    # 不做语言自动切换（见 docs/05 #39 评估结论），改为显式提示。
    engine_advice = ""
    if cfg.get("transcriber_type") == "fast-whisper" and cfg.get(
        "whisper_model_size"
    ) in ("tiny", "base"):
        engine_advice = (
            "当前 fast-whisper 模型为 tiny/base，中文内容质量一般。"
            "建议 set_transcriber(whisper_model_size='small')，"
            "或中文优先场景 set_transcriber('funasr')（需安装 funasr 依赖）。"
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
    return json.dumps(
        {
            "server_version": _SERVER_VERSION,
            "plugin_version": _installed_plugin_version(),
            "ffmpeg": "ok" if ffmpeg_ok else "missing",
            "db": "ok" if db_ok else f"error: {db_err}",
            "transcriber": {
                **cfg,
                "ready": ready["ready"],
                "downloading": ready["downloading"],
                "reason": ready["reason"],
            },
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
        },
        ensure_ascii=False,
    )


def _skill_refresh_advice() -> str:
    """插件/Skill 刷新提示（docs/05 #24）：server 与插件版本不一致时点名提示。"""
    base = (
        "MCP（uvx）跟 git HEAD；Skill/插件不自动更新。"
        "工作流对不上时：`claude plugin disable videonote@videonote` "
        "然后 `claude plugin install videonote@videonote`，再开新会话。"
    )
    plugin_version = _installed_plugin_version()
    if plugin_version and plugin_version != _SERVER_VERSION:
        return (
            f"检测到插件版本 {plugin_version} 落后于 server {_SERVER_VERSION}：{base}"
        )
    return base


@mcp.tool()
def validate_url(url: str) -> str:
    """判断视频链接属于哪个平台，以及是否受支持。

    内置平台：bilibili（含 b23.tv）、youtube（含 youtu.be）、douyin、tiktok、kuaishou、本地文件路径。
    其他 URL 返回 platform: "generic"（会尝试 yt-dlp 通用提取，覆盖 1800+ 站点）。
    仅显式传 platform="unsupported" 时返回 {supported: False, handoff: True, ...}。
    """
    try:
        platform = _detect_platform(url)
        if platform == "unsupported":
            return json.dumps(
                {"supported": False, **pipeline.handoff_result(url)},
                ensure_ascii=False,
            )
        reason = (
            "识别为 generic：将尝试 yt-dlp 通用提取（可能需登录/代理）"
            if platform == "generic"
            else f"识别为 {platform}"
        )
        return json.dumps(
            {"supported": True, "platform": platform, "reason": reason},
            ensure_ascii=False,
        )
    except ValueError as e:
        return json.dumps({"supported": False, "reason": str(e)}, ensure_ascii=False)


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
    except Exception:
        row = None
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
        return False, f"供应商 {pid} 没有可用模型：先 list_models(provider_id) 或 add_model"
    return True, f"{pid}（key 已填，默认模型 {model}）"


@mcp.tool()
def preflight(
    url: str = "",
    platform: Optional[str] = None,
    provider_id: Optional[str] = None,
) -> str:
    """提交任务前的轻量体检（建议 generate_note / prepare_note_material 前调用）。

    检查项：ffmpeg、磁盘剩余、转写器就绪（本地模型已下载/云端 key）、供应商 key 与
    模型、任务队列；url 非空时顺带预解析视频时长（仅参考，不拦截）。
    返回 {ok, checks:[{name, ok, detail}], duration_secs?}。ok=false 时先解决
    detail 里的问题再提交，避免长任务跑到半路才因模型未下载 / 磁盘满失败。
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
        checks.append({"name": "disk", "ok": False, "detail": f"无法读取磁盘信息: {e}"})

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

    p_ok, p_detail = _preflight_provider(provider_id)
    checks.append({"name": "provider", "ok": p_ok, "detail": p_detail})

    with _tasks_lock:
        queue_len = len(_task_futures)
    queue_ok = queue_len < _MAX_WORKERS
    checks.append({
        "name": "queue",
        "ok": queue_ok,
        "detail": f"{queue_len}/{_MAX_WORKERS} 进行中"
        + ("" if queue_ok else "（已满，请等任务完成或 cancel_note 后再提交）"),
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
                            "detail": f"多集共 {info.get('total', len(entries))} 条（建议逐条生成，分 P 用 entries[].url）",
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
                    "detail": f"无法预解析时长（{e}）；提交后任务内会重试",
                }
            )

    failed = [c for c in checks if not c["ok"]]
    return json.dumps(
        {"ok": not failed, "checks": checks, "duration_secs": duration_secs},
        ensure_ascii=False,
    )


@mcp.tool()
def inspect_video(url: str, platform: Optional[str] = None) -> str:
    """解析视频链接，列出可独立生成笔记的条目（B 站分 P / YouTube 播放列表 / 单集）。

    **只解析、不下载、不提交任务。** 多集时 entries[].url 可直接喂给
    `generate_note` / `prepare_note_material`；Agent 按单视频流程处理（多集用
    subagent，不要在同一消息里并行塞多个 generate_note）。

    返回 {ok, platform, kind: single|multi, title, video_id, current_p?,
    total, truncated, entries:[{p, title, duration, url, video_id}]}。
    超过 200 条截断（truncated=true）。失败 {ok:false, error}。
    """
    from app.services.inspect import inspect_video as _inspect

    return json.dumps(_inspect(url, platform=platform), ensure_ascii=False)


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
    """对播放列表/合集/分 P 链接批量提交笔记任务（服务端逐个排队，遵守并发门禁）。

    - video_url: 必填，B 站分 P / YouTube 播放列表等可展开为多集的链接；
    - max_entries: 最多提交条数（默认 10，防 200 集播放列表一次全排；超出截断并标记 truncated）；
    - 其余参数与 generate_note 一致（quality/provider_id/model_name/style/format/screenshot/
      extras/link/video_understanding/video_interval/grid_size/include_comments/
      comments_limit/notes_dir），批量共享同一套风格与格式设置；单集链接退化为单个
      generate_note 任务。

    内部先 inspect_video 展开条目再逐条提交（同一并发门禁，超出 worker 数的排队等待；
    与 SKILL「多集用 subagent 逐个提交」纪律不同——本工具把展开+排队收敛到服务端）。
    返回 {ok, total, submitted, truncated?, errors:[{p, title, url, error}],
    tasks:[{p, title, duration, url, task_id, status}]}。
    单条失败不阻断其余；全部失败时 ok=false。之后逐个 get_task_status 轮询。
    """
    from app.services.inspect import inspect_video as _inspect

    # 上界钳制：防止一条 MCP 调用内做上千次串行解析/排队（docs 审计 F7）
    max_entries = max(1, min(int(max_entries or 10), 50))
    parsed = _inspect(video_url, platform=platform)
    if not parsed.get("ok"):
        return json.dumps(parsed, ensure_ascii=False)
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
        # 单集链接：退化为单任务，不引入条目语义
        task = _submit({"p": 1, "title": parsed.get("title"), "url": video_url, "duration": None})
        return json.dumps(
            {"ok": True, "total": 1, "submitted": 1, "errors": [], "tasks": [task]},
            ensure_ascii=False,
        )

    truncated = len(entries) > max_entries
    total = int(parsed.get("total") or len(entries))
    entries = entries[:max_entries]
    submitted, errors, tasks = 0, [], []
    for e in entries:
        try:
            tasks.append(_submit(e))
            submitted += 1
        except Exception as exc:  # noqa: BLE001 —— 单条失败收集继续
            errors.append(
                {"p": e.get("p"), "title": e.get("title"), "url": e.get("url"), "error": str(exc)}
            )
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


@mcp.tool()
def set_downloader_cookie(platform: str, cookie: str = "") -> str:
    """**不要经本工具传 Cookie**（SESSDATA 会进对话上游）。

    请在本会话终端执行：
    `! videonote login bilibili`（扫码）或 `! videonote setup`。
    传入非空 cookie 会直接拒绝。本工具仅保留签名兼容，不再写入。
    """
    if cookie:
        raise ValueError(_SENSITIVE_VIA_MCP)
    raise ValueError(
        "请用 CLI 配置 Cookie：`! videonote login bilibili` 或 `! videonote setup`。"
        "不要把 SESSDATA / Cookie 经 MCP 工具传入。"
    )


@mcp.tool()
def export_transcript(
    task_id: str,
    formats: Optional[List[str]] = None,
    out_dir: Optional[str] = None,
) -> str:
    """把已完成任务的转写导出为纯格式文件（SRT/VTT/JSON），返回文件路径。

    - task_id: 必填，已完成任务的 task_id（generate_note / prepare_note_material 返回）；
    - formats: 可选，要导出的格式列表（srt/vtt/json），缺省取 setup 配置的「导出格式默认」；
    - out_dir: 可选，输出目录（缺省为 note_results/{task_id}/）。

    只做确定性机械渲染（时间轴换算），不调用 LLM。返回
    {task_id, formats: {fmt: "file://绝对路径"}, errors: {}}，供 Agent 直接 Read。
    """
    from videonote_mcp.export import export_transcript as _export

    task_id = _validate_task_id(task_id)
    task_dir = NOTE_OUTPUT_DIR / str(task_id)
    result_json = task_dir / "result.json"
    transcript = None
    if result_json.exists():
        try:
            transcript = json.loads(result_json.read_text(encoding="utf-8")).get("transcript")
        except Exception:
            transcript = None
    if transcript is None:
        # fallback：{task_dir}/gen/transcript.json 缓存
        cache = task_dir / "gen" / "transcript.json"
        if cache.exists():
            try:
                transcript = json.loads(cache.read_text(encoding="utf-8"))
            except Exception:
                transcript = None
    if transcript is None:
        return json.dumps(
            {"task_id": task_id, "error": f"找不到任务 {task_id} 的转写结果（任务可能未成功）"},
            ensure_ascii=False,
        )

    if formats is None:
        from videonote_mcp.config import env_json_list, get_app_config

        formats = get_app_config().get("default_export_formats") or env_json_list(
            "VIDEONOTE_DEFAULT_EXPORT_FORMATS", []
        )
        if not formats:
            formats = ["srt"]

    out = out_dir or str(task_dir / "gen")
    # 数据目录外输出只提示不拦截：显式导出到别处是用户意图（docs/05 #45）
    if not Path(out).resolve().is_relative_to(DATA_DIR.resolve()):
        logger.warning("export_transcript 输出到数据目录外: %s", out)
    written = _export(transcript, formats=formats, out_dir=out, task_id=task_id)
    errors = written.pop("_errors", {}) if isinstance(written, dict) else {}
    return json.dumps(
        {"ok": True, "task_id": task_id, "formats": written, "errors": errors},
        ensure_ascii=False,
    )


@mcp.tool()
def merge_audio(files: List[str], out_dir: Optional[str] = None) -> str:
    """把多个音频/视频文件合并为一个 16kHz mono wav（FFmpeg concat）。

    - files: 必填，至少 2 个本地文件路径（mp3/wav/m4a/mp4 等，编码可不同——自动统一转 16kHz mono）；
    - out_dir: 可选，输出目录（缺省数据目录 note_results/merged/），输出为 merged.wav。

    用途：多段录音/会议分段/多个本地视频拼成一段再转写。返回
    {ok, path: "file://绝对路径"} 或 {ok: false, error}。
    """
    from app.services.merge import merge_audio as _merge

    try:
        out = out_dir or str(NOTE_OUTPUT_DIR / "merged")
        if out_dir and not Path(out_dir).resolve().is_relative_to(DATA_DIR.resolve()):
            logger.warning("merge_audio 输出到数据目录外: %s", out_dir)
        merged = _merge(files, out_dir=out)
        return json.dumps({"ok": True, "path": Path(merged).as_uri()}, ensure_ascii=False)
    except Exception as exc:
        logger.warning(f"merge_audio 失败: {exc}")
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


@mcp.tool()
def diarize_media(
    audio_file: str,
    num_speakers: Optional[int] = None,
    hf_token: Optional[str] = None,
) -> str:
    """对音频做说话人分离（pyannote，可选依赖），返回说话人时间段。

    - audio_file: 必填，本地音频/视频文件（自动归一化为 16kHz mono wav 再分离）；
    - num_speakers: 可选，说话人数提示（缺省自动检测）。

    **不要传 hf_token**（会进对话上游）。token 只从环境变量
    `HUGGINGFACE_HUB_TOKEN` 或 `! videonote setup` 写入的 app_config.hf_token 读取。
    传入非空 hf_token 会直接拒绝。
    """
    if hf_token:
        raise ValueError(_SENSITIVE_VIA_MCP)
    try:
        from app.services.diarization import diarize_audio
        from app.transcriber.audio_preprocess import normalize_to_wav

        p = _coerce_local_path(audio_file)
        if not p.exists():
            raise FileNotFoundError(f"本地文件不存在: {audio_file}")
        wav = normalize_to_wav(str(p))
        turns = diarize_audio(wav, hf_token=None, num_speakers=num_speakers)
        return json.dumps(
            {"ok": True, "turns": turns, "num_speakers": len({t["speaker"] for t in turns})},
            ensure_ascii=False,
        )
    except Exception as exc:
        logger.warning(f"diarize_media 失败: {exc}")
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


# ---------- 入口 ----------


def main() -> None:
    """MCP server 入口。CLI（providers）由 videonote_mcp.cli:main 分发，本函数只跑 MCP stdio。"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s - %(message)s")
    init_db()
    try:
        from app.events import register_handler as _register_handler

        _register_handler()
        logger.info("已注册转写完成清理事件")
    except Exception as e:
        logger.warning(f"注册事件监听器失败: {e}")
    logger.info(f"VideoNote-Mcp 启动 | 数据目录: {DATA_DIR}")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
