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


def record_task_paths(task_id: str, paths: Sequence, *, strict: bool = False) -> bool:
    """把 task 创建的文件/目录追加进 manifest（去重，原子写 tmp+replace，保留 meta 键）。

    默认尽力而为：失败只记日志，返回 ``False``，不阻断历史调用方。
    ``strict=True`` 用于发布成功前的关键路径：写入或回读校验失败会抛出
    异常，调用方可以把任务标记为 FAILED，而不是暴露没有完整清单的 SUCCESS。
    """
    if not task_id:
        if strict:
            raise ValueError("task_id 不能为空")
        return False
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
        # 唯一 tmp + 创建即 0600（#140 A5，#133 A2 登记项收尾：与 json_store/status 同口径）——
        # 固定 <path>.tmp 在 CLI 与 MCP server 双进程并发写时互相截断丢更新
        from app.utils.json_store import _unique_tmp, _write_bytes_with_mode

        tmp = _unique_tmp(f)
        _write_bytes_with_mode(
            tmp, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"), 0o600
        )
        tmp.replace(f)
        if strict:
            persisted = json.loads(f.read_text(encoding="utf-8"))
            persisted_paths = persisted.get("paths") if isinstance(persisted, dict) else None
            if not isinstance(persisted_paths, list) or any(
                path not in persisted_paths for path in data["paths"]
            ):
                raise IOError(f"manifest 写入后校验失败: {f}")
        return True
    except Exception as e:  # noqa: BLE001 —— 默认记录是尽力而为
        logger.warning("记录 task 路径失败 task_id=%s: %s", task_id, e)
        if strict:
            raise
        return False


def get_task_paths(task_id: str) -> List[str]:
    """读 manifest 的 paths；不存在或损坏返回 []。"""
    return list(_read_manifest(task_id).get("paths", []))


def get_task_meta(task_id: str) -> dict:
    """读 manifest 的 meta 键（cleanup_note dry_run 响应契约字段）；不存在返回 {}。

    写端 record_task_meta 已删（#134 死代码）——生产不再写 meta，此读函数
    保留仅为 list_task_files 的响应形状稳定。
    """
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
      - include_note=True：删整个 task_dir + 数据目录内的便携笔记副本（manifest 记录）+ manifest + 全局索引（video_tasks）记录。
        数据目录**外**的便携笔记副本（用户指定 notes_dir 时常见）不删——沙箱红线只清数据目录内；
        其路径经 notes_kept_outside 列出，避免 manifest 删除后成为无人知晓的孤儿。
    返回统计（deleted/missing/errors/note_kept/notes_kept_outside）。
    """
    notes = _note_paths(task_id)
    tdir = task_dir(task_id)

    to_delete: set = set()
    kept_outside: List[str] = []

    if include_note:
        # 整删：任务文件夹 + manifest + 全局索引
        if tdir.exists():
            to_delete.add(tdir)
        # 便携笔记副本：<notes_dir>/<标题>/note.md（或含 note.md 的目录）
        roots = [get_note_dir(), get_data_dir()]
        for p in get_task_paths(task_id):
            try:
                resolved = Path(str(p)).expanduser().resolve()
            except Exception:  # noqa: BLE001
                continue
            if not (
                resolved.name == "note.md"
                or (resolved.is_dir() and (resolved / "note.md").exists())
            ):
                continue
            if _safe_resolve(p, roots) is not None:
                # 数据目录内 → 连目录一起删（note.md 文件则删其所在目录）
                to_delete.add(resolved if resolved.is_dir() else resolved.parent)
            else:
                # 数据目录外 → 沙箱红线不删，列出目录路径供报告
                kept_outside.append(str(resolved if resolved.is_dir() else resolved.parent))
        kept_outside = sorted(set(kept_outside))
        remove_manifest(task_id)
        try:
            from app.db.video_task_dao import delete_task

            delete_task(task_id)
        except Exception as exc:
            # 磁盘已清但索引删除失败 → list_tasks 出现 note_dir 悬空幽灵任务；
            # 不能静默吞（曾 except: pass 连哪个任务失败都丢，与 #126 B7 同口径）
            logger.warning(f"清理任务 {task_id} 全局索引失败（文件已清理，索引可能残留）: {exc}")
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
        "notes_kept_outside": kept_outside,
        **stats,
    }


def cleanup_targets_inside_data_root() -> dict:
    """全局清理各目标目录是否落于数据根内（#140 复扫 A1：不只 config/models）。

    NOTE_OUTPUT_DIR / IMAGE_OUTPUT_DIR / VIDEONOTE_CONFIG_DIR / VIDEONOTE_MODEL_DIR
    均可由环境变量指向数据根外；data/ 下的默认目录（note_results/static/covers/
    note_cache/config/models）也可能是指向外部的符号链接——Path.resolve() 跟随
    符号链接给出真实目标。越界目录若被 `_empty` 清空即误伤用户数据（config 含
    key/cookie、models 重下成本高、note_results 可能是用户指定的产物仓库）。
    """
    root = [get_data_dir()]
    targets = {
        "note_results": get_note_dir(),
        "static/screenshots": get_screenshots_dir(),
        "static/cover": get_screenshots_dir().parent / "cover",
        "covers": get_data_dir() / "covers",
        "note_cache": get_cache_dir(),
        "config": get_config_dir(),
        "models": get_models_dir(),
    }
    return {key: _safe_resolve(d, root) is not None for key, d in targets.items()}


def cleanup_all_files(include_config: bool = False, include_models: bool = False) -> Dict:
    """全局清理（恢复出厂）：清空 note_results / static/screenshots / static/cover / covers / note_cache 的任务产物。

    **logs/ 刻意不清**（#121 C3）：MCP 进程持有 mcp_stderr.log 的打开 fd，unlink 后
    日志写入进入已删除的 inode——文件消失、磁盘不回收、无报错直到重启；日志也不属
    于任务产物，计入 kept。
    默认保留 config/（LLM key / cookie / 转写设置）与 models/（模型可复用、重下成本高）；
    include_config=True 时连 config/ 一起清；include_models=True 时连 models/ 一起清。
    同步清空 video_tasks 全局索引（任务目录删了，索引记录一并清）。

    安全红线（#140，复扫 A1 修复）：**所有**被 `_empty` 的目标目录——note_results /
    static/screenshots / static/cover / covers / note_cache / config / models——若落在
    数据根**外**（环境变量指向外部 / 符号链接到外部），一律拒绝清空并列入 kept_outside：
    沙箱红线只清数据目录内，与便携笔记副本同口径（绝不清用户指定目录下与任务无关的内容）。
    """
    result: Dict = {"cleaned": {}, "kept": [], "kept_outside": []}
    inside = cleanup_targets_inside_data_root()

    def _empty(d: Path, key: str) -> None:
        if not d.exists() or not d.is_dir():
            result["cleaned"][key] = {"deleted": [], "missing": [], "errors": []}
            return
        result["cleaned"][key] = _delete_all(list(d.iterdir()))

    def _empty_guarded(key: str, d: Path) -> None:
        """先查 target 是否落于数据根内；越界拒绝清空并留痕（#140 复扫 A1）。"""
        if not inside.get(key, True):
            logger.warning(
                "拒绝清理 %s 目录（%s 落在数据根外或为外部符号链接，沙箱红线只清数据目录内）: %s",
                key, key, d,
            )
            result["kept_outside"].append(f"{key}（数据根外，拒绝清理: {d}）")
            return
        _empty(d, key)

    _empty_guarded("note_results", get_note_dir())
    _empty_guarded("static/screenshots", get_screenshots_dir())
    # local 封面两处目录（#125 B4）：每个 local 任务各产 1 个文件，此前永不清理
    _empty_guarded("static/cover", get_screenshots_dir().parent / "cover")
    _empty_guarded("covers", get_data_dir() / "covers")
    # logs/ 不清理（#121 C3）：MCP 进程持有 mcp_stderr.log 的打开 fd，unlink 后
    # 日志写入进入已删除的 inode——文件消失、磁盘不回收、无任何报错直到重启；
    # 且日志不属于任务产物。保留并记录到 kept。
    result["kept"].append(f"logs（{get_logs_dir()}）")
    _empty_guarded("note_cache", get_cache_dir())
    # 同步清空全局任务索引（尽力而为；#125 B12 单条 DELETE 替代 N+1 循环）。
    # 失败不能静默：目录已清但索引残留 → list_tasks 出现 note_dir 悬空的任务
    # 且零痕迹（DAO 契约是抛给调用方显式处理，#126 B7）
    try:
        from app.db.video_task_dao import delete_all_tasks

        delete_all_tasks()
    except Exception as exc:
        logger.warning(f"清空全局任务索引失败（目录已清理，索引可能残留）: {exc}")
        result["index_error"] = str(exc)

    # config/models：默认保留，include_* 时才清；越界同款拒绝（#140 A2）
    for label, d, flag in (
        ("config", get_config_dir(), include_config),
        ("models", get_models_dir(), include_models),
    ):
        if not flag:
            result["kept"].append(label)
            continue
        _empty_guarded(label, d)

    return result
