"""task_manifest.py —— 任务产物路径的可追踪清单与清理。

任务生成的文件（下载的视频/音频、转写缓存、markdown 缓存、status/result JSON、
dl_{task_id} 下载目录、note_dir 便携笔记等）会被**尽力而为**地记入
`NOTE_OUTPUT_DIR/{task_id}.manifest.json`，供 AGENT：
  1. `list_task_files` 先查后清（返回 manifest 记录 + 真实存在的文件）；
  2. `cleanup_task_files` 按 task 精确清理（默认保留最终笔记）；
  3. `cleanup_all_files` 全局清理（恢复出厂，默认保留 config/ 与 models/）。

安全纪律：
  - 只删除 manifest 记录 / 明确前缀模式（note_results/{task_id}、dl_{task_id}）的路径；
  - 任何用户/manifest 给的路径在删除前都要 `resolve()` 校验落在数据目录内（防路径穿越）；
  - 删除逐条 try/except，失败跳过并统计。
记录是尽力而为：失败只记日志，绝不阻断生成流水线。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------- 目录解析（读环境变量，测试可注入） ----------------

def get_data_dir() -> Path:
    """数据根目录（config.setup_environment 设置 VIDEONOTE_DATA_DIR）。"""
    return Path(os.getenv("VIDEONOTE_DATA_DIR", "data")).expanduser().resolve()


def get_note_dir() -> Path:
    """笔记/任务产物输出目录（与 app.services.note.NOTE_OUTPUT_DIR 一致）。"""
    return Path(os.getenv("NOTE_OUTPUT_DIR", str(get_data_dir() / "note_results"))).expanduser().resolve()


def task_dir(task_id: str) -> Path:
    """每任务统一文件夹：NOTE_OUTPUT_DIR/{task_id}（数据层重构后的唯一 per-task 根）。

    一个任务的所有内容（raw/、gen/、status.json、manifest.json、result.json）都在此目录下。
    """
    return get_note_dir() / str(task_id)


def get_screenshots_dir() -> Path:
    """静态截图目录（与 app.services.note.IMAGE_OUTPUT_DIR 一致）。"""
    return Path(os.getenv("IMAGE_OUTPUT_DIR", str(get_data_dir() / "static" / "screenshots"))).expanduser().resolve()


def get_cache_dir() -> Path:
    """跨任务转写缓存目录（note_results 的兄弟目录，与 note_cache.cache_root 一致）。

    与 note_results 平级：per-task 清理以任务文件夹为边界不触碰；cleanup_all 一并清。
    """
    return get_note_dir().parent / "note_cache"


def get_config_dir() -> Path:
    """配置目录（LLM key / cookie / 转写设置 / app_config）。"""
    return Path(os.getenv("VIDEONOTE_CONFIG_DIR", str(get_data_dir() / "config"))).expanduser().resolve()


def get_logs_dir() -> Path:
    """日志目录（server 的 stderr 重定向也在此）。"""
    return get_data_dir() / "logs"


def get_models_dir() -> Path:
    """模型缓存目录（whisper/mlx；全局清理默认保留）。"""
    return Path(os.getenv("VIDEONOTE_MODEL_DIR", str(get_data_dir() / "models"))).expanduser().resolve()


def manifest_path(task_id: str) -> Path:
    """manifest 文件落在任务文件夹内（{task_dir}/manifest.json）。"""
    return task_dir(task_id) / "manifest.json"


# ---------------- manifest 记录 / 读取 ----------------

def _read_manifest(task_id: str) -> dict:
    f = manifest_path(task_id)
    if not f.exists():
        return {"task_id": task_id, "paths": [], "meta": {}}
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {"task_id": task_id, "paths": [], "meta": {}}
        data.setdefault("paths", [])
        data.setdefault("meta", {})
        return data
    except Exception:  # noqa: BLE001
        return {"task_id": task_id, "paths": [], "meta": {}}


def record_task_paths(task_id: str, paths: Sequence) -> None:
    """把 task 创建的文件/目录追加进 manifest（去重，原子写 tmp+replace，保留 meta 键）。

    尽力而为：任何失败只记日志，不抛异常、不阻断调用方。
    """
    if not task_id:
        return
    try:
        data = _read_manifest(task_id)
        seen = set(data["paths"])
        additions: List[str] = []
        for p in paths:
            if not p:
                continue
            s = str(p)
            if s not in seen:
                seen.add(s)
                additions.append(s)
        if additions:
            data["paths"] = list(data["paths"]) + additions
        f = manifest_path(task_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)
    except Exception as e:  # noqa: BLE001 —— 记录是尽力而为
        logger.warning("记录 task 路径失败 task_id=%s: %s", task_id, e)


def get_task_paths(task_id: str) -> List[str]:
    """读 manifest 的 paths；不存在或损坏返回 []。"""
    return list(_read_manifest(task_id).get("paths", []))


def record_task_meta(task_id: str, meta: dict) -> None:
    """把任务语义元数据（title/summary 等）合并进 manifest 的 meta 键。

    与 record_task_paths 同文件（保留 paths），供 get_task_files 展示。
    """
    if not task_id or not meta:
        return
    try:
        data = _read_manifest(task_id)
        data["meta"] = {**data.get("meta", {}), **meta}
        f = manifest_path(task_id)
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(f)
    except Exception as e:  # noqa: BLE001
        logger.warning("记录 task meta 失败 task_id=%s: %s", task_id, e)


def get_task_meta(task_id: str) -> dict:
    """读 manifest 的 meta 键；不存在返回 {}。"""
    return dict(_read_manifest(task_id).get("meta", {}))


def remove_manifest(task_id: str) -> None:
    """删除 manifest 文件（include_note=True 的整删收尾）。失败只记日志。"""
    try:
        manifest_path(task_id).unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        logger.warning("删除 manifest 失败 task_id=%s: %s", task_id, e)


# ---------------- 安全删除辅助 ----------------

def _safe_resolve(path, roots: Sequence[Path]) -> Optional[Path]:
    """把用户/manifest 给的路径解析为绝对路径；不在任一 root 内（或解析失败）返回 None。

    路径穿越防护的核心：任何待删路径都必须落在数据目录内。
    """
    try:
        p = Path(str(path)).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return None
    for root in roots:
        try:
            r = Path(str(root)).expanduser().resolve()
        except Exception:  # noqa: BLE001
            continue
        try:
            p.relative_to(r)
            return p
        except ValueError:
            continue
    return None


def _delete_all(paths: Sequence[Path]) -> Dict[str, List]:
    """批量删除文件/目录；返回 {deleted, missing, errors}。深路径先删、逐条容错。"""
    deleted: List[str] = []
    missing: List[str] = []
    errors: List[Dict] = []
    ordered = sorted(
        {str(p) for p in paths if p},
        key=lambda s: (s.count(os.sep), s),
        reverse=True,  # 深路径先删（子先于父）
    )
    for s in ordered:
        p = Path(s)
        try:
            exists = p.exists() or p.is_symlink()
        except Exception:  # noqa: BLE001
            exists = False
        if not exists:
            missing.append(s)
            continue
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink(missing_ok=True)
            deleted.append(s)
        except Exception as e:  # noqa: BLE001
            errors.append({"path": s, "error": str(e)})
    return {"deleted": deleted, "missing": missing, "errors": errors}


def _note_paths(task_id: str) -> set:
    """该 task 的「最终笔记」路径集合：gen/note.md 与其所在目录。"""
    tdir = task_dir(task_id)
    gen = tdir / "gen"
    notes = set()
    note_md = gen / "note.md"
    if note_md.exists():
        notes.add(note_md)
        notes.add(gen)
    # 便携笔记副本（用户指定 notes_dir 时写 <notes_dir>/<标题>/note.md）
    roots = [get_note_dir(), get_data_dir()]
    for p in get_task_paths(task_id):
        resolved = _safe_resolve(p, roots)
        if not resolved:
            continue
        if resolved.name == "note.md":
            notes.add(resolved)
        elif resolved.is_dir() and (resolved / "note.md").exists():
            notes.add(resolved)
    return notes


# ---------------- 查询 ----------------

def list_task_files(task_id: str) -> Dict:
    """列出某 task 在磁盘上相关的文件/目录（manifest 记录 + 任务文件夹扫描）。

    返回 {task_id, manifest_paths, existing, meta}，existing 是真实存在的文件/目录列表。
    """
    manifest = get_task_paths(task_id)
    roots = [get_note_dir(), get_data_dir()]
    existing: List[str] = []
    for p in manifest:
        resolved = _safe_resolve(p, roots)
        if resolved is not None and (resolved.exists() or resolved.is_symlink()):
            existing.append(str(resolved))
    # 任务文件夹（task_dir）整体 + raw/ gen/ 下的真实文件
    tdir = task_dir(task_id)
    if tdir.exists():
        existing.append(str(tdir))
        for sub in ("raw", "gen"):
            s = tdir / sub
            if s.exists():
                existing.append(str(s))
                existing.extend(str(f) for f in s.rglob("*") if f.is_file())
    # 去重保序
    existing = list(dict.fromkeys(existing))
    return {
        "task_id": task_id,
        "manifest_paths": manifest,
        "existing": existing,
        "meta": get_task_meta(task_id),
    }


# ---------------- 清理 ----------------

def cleanup_task_files(task_id: str, include_note: bool = False) -> Dict:
    """按 task 清理中间产物；include_note=True 时连最终笔记（gen/note.md）一起删。

    数据层重构后以任务文件夹 task_dir 为边界：
      - include_note=False：删 raw/（下载媒体）+ gen/ 内除 note.md 外的一切；保留 task_dir + status/manifest/result。
      - include_note=True：删整个 task_dir + manifest + 全局索引（video_tasks）记录。
    返回统计（deleted/missing/errors/note_kept）。
    """
    note_dir = get_note_dir()
    roots = [note_dir, get_data_dir()]
    notes = _note_paths(task_id)
    tdir = task_dir(task_id)

    to_delete: set = set()

    if include_note:
        # 整删：任务文件夹 + manifest + 全局索引
        if tdir.exists():
            to_delete.add(tdir)
        remove_manifest(task_id)
        try:
            from app.db.video_task_dao import delete_task

            delete_task(task_id)
        except Exception:
            pass
    else:
        # 保留 note：删 raw/ 整个 + gen/ 内非 note.md 的子项
        raw = tdir / "raw"
        if raw.exists():
            to_delete.add(raw)
        gen = tdir / "gen"
        if gen.exists():
            for child in gen.iterdir():
                if child in notes or child.name == "note.md":
                    continue
                if child.name == "Assets":
                    # note.md 用相对引用 Assets/... 指向截图；保留笔记时必须保留，
                    # 否则 cleanup_note 之后笔记里全是悬空图片
                    continue
                to_delete.add(child)

    stats = _delete_all(to_delete)
    return {
        "task_id": task_id,
        "include_note": include_note,
        "note_kept": (not include_note) and bool(notes),
        **stats,
    }


def cleanup_all_files(include_config: bool = False, include_models: bool = False) -> Dict:
    """全局清理（恢复出厂）：清空 note_results / static/screenshots / logs 的所有任务产物。

    默认保留 config/（LLM key / cookie / 转写设置）与 models/（模型可复用、重下成本高）；
    include_config=True 时连 config/ 一起清；include_models=True 时连 models/ 一起清。
    同步清空 video_tasks 全局索引（任务目录删了，索引记录一并清）。
    """
    result: Dict = {"cleaned": {}, "kept": []}

    def _empty(d: Path, key: str) -> None:
        if not d.exists() or not d.is_dir():
            result["cleaned"][key] = {"deleted": [], "missing": [], "errors": []}
            return
        result["cleaned"][key] = _delete_all(list(d.iterdir()))

    _empty(get_note_dir(), "note_results")
    _empty(get_screenshots_dir(), "static/screenshots")
    _empty(get_logs_dir(), "logs")
    _empty(get_cache_dir(), "note_cache")
    # 同步清空全局任务索引（尽力而为）
    try:
        from app.db.video_task_dao import list_tasks as _list
        from app.db.video_task_dao import delete_task

        for t in _list():
            delete_task(t["task_id"])
    except Exception:
        pass

    if include_config:
        _empty(get_config_dir(), "config")
    else:
        result["kept"].append("config")

    if include_models:
        _empty(get_models_dir(), "models")
    else:
        result["kept"].append("models")

    return result
