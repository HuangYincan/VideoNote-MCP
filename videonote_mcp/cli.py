"""videonote 命令行入口（console script 指向本模块的 main）。

- `videonote providers ...` → 轻量 CLI：只导入 provider 相关模块
  （不加载下载器/转写器，启动快、无 import 噪音），在终端直接管理 LLM 供应商。
- 其余参数（含无参数，MCP stdio 模式）→ 懒加载并启动完整 MCP server。

API key 的设计原则：key 由用户在独立终端写入（不经过 agent 对话，
避免泄露给 agent 的 LLM 上游），见 README「安全说明」。
"""
import argparse
import builtins
import json
import os
import re
import sys
from pathlib import Path
from typing import List

from videonote_mcp.config import (
    get_app_config,
    remove_app_config,
    resolve_default_export_formats,
    resolve_int_config,
    set_app_config,
    setup_environment,
)

# 提前初始化运行时环境（数据目录、DB、输出目录）——必须在 import provider_probe / app.*
# 之前执行。provider_probe 会连累 app.utils.logger（其日志目录依赖 VIDEONOTE_DATA_DIR），
# 若 setup 在其后，LOG_DIR 会落到 CWD/logs。
DATA_DIR = setup_environment()

from videonote_mcp.provider_probe import probe_chat, probe_models

# 轻量 CLI 不该被 import 时的裸 print 污染 stdout，先进程级重定向到 stderr
_orig_print = builtins.print


def _print_to_stderr(*args, **kwargs):
    kwargs.setdefault("file", sys.stderr)
    _orig_print(*args, **kwargs)


builtins.print = _print_to_stderr

# 只导入 provider 相关（不触发 app.downloaders / app.transcriber 的 import 噪音）
from app.db.init_db import init_db
from app.db.model_dao import get_model_by_provider_and_name, insert_model
from app.db.provider_dao import seed_default_providers
from app.services.provider import ProviderService
from app.services.transcriber_config_manager import TranscriberConfigManager

init_db()
seed_default_providers()

_BUILTIN_PROVIDERS = {
    "1": ("deepseek", "DeepSeek", "https://api.deepseek.com"),
    "2": ("openai", "OpenAI", "https://api.openai.com/v1"),
    "3": ("qwen", "Qwen", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "4": ("groq", "Groq", "https://api.groq.com/openai/v1"),
    "5": ("ollama", "Ollama（本地免费，无需 key）", "http://127.0.0.1:11434/v1"),
}
_WHISPER_SIZES = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")


def _ask(prompt: str, default: str = "") -> str:
    """交互式提问；非交互环境（管道）下返回默认值。"""
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ")
    except EOFError:
        return default
    val = val.strip()
    return val or default


def _ask_secret(prompt: str) -> str:
    """隐藏输入的 API key（不经任何对话/日志）。"""
    import getpass

    try:
        return getpass.getpass(f"{prompt}: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _tqdm_bar():
    """构造带统一格式的 tqdm 类（snapshot_download 的 tqdm_class 用）。"""
    from tqdm import tqdm

    class _Bar(tqdm):
        def __init__(self, *a, **k):
            k.setdefault("bar_format", "{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]")
            super().__init__(*a, **k)

    return _Bar


def _download_whisper(size: str) -> None:
    """在终端下载 fast-whisper 模型（阻塞，带进度条）。"""
    from app.transcriber.whisper_models import is_local_target, resolve_whisper_model
    from app.utils.path_helper import get_model_dir
    from huggingface_hub import snapshot_download

    target = resolve_whisper_model(size)
    model_dir = get_model_dir("whisper")
    if is_local_target(target):
        print(f"（{target} 为本地路径，无需下载）", file=sys.stdout)
        return
    print(f"正在下载 whisper-{size}（{target}）…", file=sys.stdout)
    snapshot_download(repo_id=target, cache_dir=model_dir, tqdm_class=_tqdm_bar())
    # 让 faster-whisper 真正加载（确认模型可用）
    from faster_whisper import WhisperModel

    WhisperModel(model_size_or_path=target, device="cpu", compute_type="int8", download_root=model_dir)
    print(f"✓ whisper-{size} 下载完成", file=sys.stdout)


def _download_mlx_model(size: str) -> None:
    """在终端下载 mlx-whisper 模型（仅 macOS，阻塞，带进度条）。

    下载只需 huggingface_hub，**不 import mlx_whisper** —— 其依赖链（numba 等）
    有循环导入风险，且下载模型根本不需要 mlx 运行时。
    """
    from app.utils.model_status import MLX_REPO_MAP
    from app.utils.path_helper import get_model_dir
    from huggingface_hub import snapshot_download

    repo_id = MLX_REPO_MAP.get(size)
    if not repo_id:
        raise ValueError(f"未找到 mlx 模型映射: {size}（可选: {', '.join(MLX_REPO_MAP.keys())}）")
    print(f"正在下载 mlx-whisper-{size}（{repo_id}）…", file=sys.stdout)
    snapshot_download(
        repo_id=repo_id,
        local_dir=os.path.join(get_model_dir("mlx-whisper"), repo_id),
        tqdm_class=_tqdm_bar(),
    )
    print(f"✓ mlx-whisper-{size} 下载完成", file=sys.stdout)


def _model_dir(engine: str, size: str) -> "str | None":
    """返回 fast-whisper / mlx 模型的实际目录（用于显示位置 / 卸载）。"""
    from app.utils.path_helper import get_model_dir

    if engine == "mlx-whisper":
        from app.utils.model_status import MLX_REPO_MAP

        repo = MLX_REPO_MAP.get(size)
        return os.path.join(get_model_dir("mlx-whisper"), repo) if repo else None
    from app.transcriber.whisper_models import hf_cache_dirname, is_local_target, resolve_whisper_model

    try:
        target = resolve_whisper_model(size)
    except Exception:
        return None
    if is_local_target(target):
        return target
    return os.path.join(get_model_dir("whisper"), hf_cache_dirname(target))


def _show_uninstall_option(inq, pick: str, size: str, label: str) -> None:
    """显示模型位置 + 询问是否卸载（已下载 / 下载完成后用）。"""
    model_path = _model_dir(pick, size)
    if model_path and os.path.exists(model_path):
        print(f"  位置：{model_path}", file=sys.stdout)
        if inq.confirm(message="卸载该模型？", default=False, keybindings=_KB).execute():
            import shutil
            from app.utils.path_helper import get_model_dir

            shutil.rmtree(model_path, ignore_errors=True)
            if pick == "fast-whisper":
                # 老布局 whisper-{size} 一并清
                shutil.rmtree(os.path.join(get_model_dir("whisper"), f"whisper-{size}"), ignore_errors=True)
            print(f"{_YELLOW}已卸载 {label}{_RESET}", file=sys.stdout)
        else:
            print(f"{_DIM}（保留）{_RESET}", file=sys.stdout)
    else:
        print(f"{_DIM}（未找到模型目录）{_RESET}", file=sys.stdout)


# 交互配色（ANSI）
_CYAN = "\033[1;36m"
_YELLOW = "\033[1;33m"
_GREEN = "\033[1;32m"
_DIM = "\033[2m"
_RESET = "\033[0m"
# 让「← 左键」= interrupt（返回上一层，与 Ctrl-C 同）；InquirerPy 绑定需带 key 字段、
# action 必须是已注册的（interrupt / answer / skip 等）
_KB = {"interrupt": [{"key": "left"}]}


def _show_header(section: str = "") -> None:
    """清屏并重绘标题，避免历史信息堆积。"""
    print("\033[2J\033[H", end="", file=sys.stdout)
    print(f"{_CYAN}⚙  VideoNote 配置向导{_RESET}  {_DIM}↑↓ 选择 · 回车确认 · ← 返回 · Ctrl-C 退出{_RESET}", file=sys.stdout)
    if section:
        print(f"{_YELLOW}▶ {section}{_RESET}", file=sys.stdout)
    print("", file=sys.stdout)


def _setup_cli() -> None:
    """交互式配置向导：主菜单 + 各配置区，方向键选择、左键返回、随时可反复进入修改。"""
    try:
        from InquirerPy import inquirer
    except ImportError:
        print("（未安装 InquirerPy，使用纯文本提示；`uv sync` 后可启用方向键/高亮选择）", file=sys.stderr)
        _setup_cli_fallback()
        return
    _show_header()
    print("    API key 为隐藏输入，不经过 agent 对话。", file=sys.stdout)
    try:
        _wizard(inquirer)
    except (EOFError, KeyboardInterrupt):
        print(f"{_GREEN}✔ 已退出{_RESET}", file=sys.stdout)


def _wizard(inq) -> None:
    while True:
        _show_header()
        choice = inq.select(
            message="选择要配置的项目",
            choices=[
                {"name": "① LLM 供应商（填 key / 检测连接 / 默认模型）", "value": "llm"},
                {"name": "② 语音转写引擎（选引擎 / 模型尺寸 / 下载）", "value": "transcriber"},
                {"name": "③ 其他（平台 Cookie / 默认笔记位置 / 视频理解默认 / 评论·弹幕整合默认 / 笔记默认）", "value": "other"},
                {"name": "④ 数据管理（查看 / 清理任务产物）", "value": "data"},
                {"name": "✔ 完成 / 退出", "value": "exit"},
            ],
            default="llm",
            keybindings=_KB,
        ).execute()
        if choice == "llm":
            _wizard_llm(inq)
        elif choice == "transcriber":
            _wizard_transcriber(inq)
        elif choice == "other":
            _wizard_other(inq)
        elif choice == "data":
            _wizard_data(inq)
        else:
            print(f"{_GREEN}✔ 配置完成。验证：`videonote providers list`、`videonote transcriber list`{_RESET}", file=sys.stdout)
            return


def _wizard_llm(inq) -> None:
    try:
        while True:
            _show_header("① LLM 供应商")
            provs = ProviderService.get_all_providers_safe()
            choices = []
            for p in provs:
                default_model = get_app_config().get(f"default_model:{p['id']}")
                suffix = f"  ⤷默认={default_model}" if default_model else ""
                choices.append(
                    {
                        "name": f"{p['id']:<10} {p['name']:<12} key={'✓已填' if p['api_key'] else '空'}  {p['base_url']}{suffix}",
                        "value": ("open", p["id"]),
                    }
                )
            choices += [
                {"name": "＋ 新增供应商（中转站/自建）", "value": ("add", None)},
                {"name": "← 返回主菜单", "value": ("back", None)},
            ]
            pick = inq.select(message="选择要管理的供应商（← 返回）", choices=choices, keybindings=_KB).execute()
            if pick[0] == "back":
                return
            if pick[0] == "add":
                _show_header("新增供应商")
                name = inq.text(message="供应商名称", keybindings=_KB).execute()
                base_url = inq.text(message="base_url（如 https://relay.example.com/v1）", keybindings=_KB).execute()
                key = inq.secret(message="API key（隐藏输入）", keybindings=_KB).execute()
                if name and base_url and key:
                    try:
                        new_id = ProviderService.add_provider(name=name, api_key=key, base_url=base_url, logo="custom", type_="custom")
                        print(f"{_GREEN}✓ 已新增 {name} → id={new_id}{_RESET}", file=sys.stdout)
                    except ValueError as exc:
                        # 重名等校验错误就地消化，向导不崩（已填的 key/base_url 不浪费，#120）
                        print(f"{_YELLOW}⚠ {exc}（未新增）{_RESET}", file=sys.stdout)
                else:
                    print(f"{_YELLOW}⚠ 信息不完整，未新增{_RESET}", file=sys.stdout)
                continue
            _wizard_llm_provider(inq, pick[1])
    except KeyboardInterrupt:
        return  # 左键/Ctrl-C → 返回主菜单


def _wizard_llm_provider(inq, pid) -> None:
    """单个供应商的子菜单：编辑 key/base_url / 检测连接→列模型→设默认 / 返回。"""
    try:
        while True:
            _show_header(f"管理供应商 {pid}")
            cur = get_app_config().get(f"default_model:{pid}")
            pick = inq.select(
                message=f"管理 {pid}（默认模型：{cur or '未设置'}）",
                choices=[
                    {"name": "✏ 编辑 API key / base_url", "value": "edit"},
                    {"name": "🔌 检测连接 → 列出模型 → 设默认", "value": "test"},
                    {"name": "← 返回供应商列表", "value": "back"},
                ],
                keybindings=_KB,
            ).execute()
            if pick == "back":
                return
            if pick == "edit":
                _edit_provider(inq, pid)
            else:
                _test_and_set_default(inq, pid)
    except KeyboardInterrupt:
        return  # 左键/Ctrl-C → 返回供应商列表（只退一级）


def _edit_provider(inq, pid) -> None:
    """编辑供应商的 API key / base_url（key 为空=保持不变）。"""
    _show_header(f"编辑供应商 {pid}")
    key = inq.secret(message="新的 API key（直接回车保持不变）", keybindings=_KB).execute()
    if key:
        # #123 B8：update_provider 对不存在返回 None、对真实异常（DB 锁等）抛
        # ValueError——后者必须如实报出原因，不能伪装成「不存在」。
        try:
            updated = ProviderService.update_provider(pid, {"api_key": key})
        except ValueError as e:
            print(f"{_YELLOW}⚠ {e}{_RESET}", file=sys.stdout)
            updated = None
        if updated:
            print(f"{_GREEN}✓ 已更新 {pid} 的 key{_RESET}", file=sys.stdout)
        elif updated is None:
            print(f"{_YELLOW}⚠ 更新 {pid} 的 key 失败（供应商不存在？）{_RESET}", file=sys.stdout)
    base_url = inq.text(message="base_url（直接回车保持不变）", keybindings=_KB).execute()
    if base_url:
        try:
            updated = ProviderService.update_provider(pid, {"base_url": base_url})
        except ValueError as e:
            print(f"{_YELLOW}⚠ {e}{_RESET}", file=sys.stdout)
            updated = None
        if updated:
            print(f"{_GREEN}✓ 已更新 {pid} 的 base_url{_RESET}", file=sys.stdout)
        elif updated is None:
            print(f"{_YELLOW}⚠ 更新 {pid} 的 base_url 失败（供应商不存在？）{_RESET}", file=sys.stdout)


def _test_and_set_default(inq, pid) -> None:
    """检测连接 → 列出可用模型 → 选择默认模型。"""
    _show_header(f"检测连接 {pid}")
    provider = ProviderService.get_provider_by_id(pid)
    if not provider:
        print(f"{_YELLOW}⚠ 供应商 {pid} 不存在{_RESET}", file=sys.stdout)
        return
    if not provider.get("api_key"):
        print(f"{_DIM}（该供应商未填 key；Ollama 可无 key 直测）{_RESET}", file=sys.stdout)
    r = probe_models(provider.get("api_key"), provider.get("base_url"), name=provider.get("name", ""))
    if r["ok"]:
        print(f"{_GREEN}✓ 连接成功：{len(r['models'])} 个模型{_RESET}", file=sys.stdout)
        for m in sorted(set(r["models"]))[:30]:
            print(f"  {_DIM}{m}{_RESET}", file=sys.stdout)
        if len(r["models"]) > 30:
            print(f"{_DIM}… 共 {len(r['models'])} 个，仅显示前 30{_RESET}", file=sys.stdout)
        _pick_default_model(inq, pid, r["models"])
        return
    # /v1/models 失败 → 提示 + 可选降级 chat 探测
    print(f"{_YELLOW}✗ 无法从 /v1/models 获取模型列表：{r['error']}{_RESET}", file=sys.stdout)
    print(f"{_DIM}部分中转站 / 自建网关不实现 /v1/models，可改用「最小对话请求」检测。{_RESET}", file=sys.stdout)
    try:
        if inq.confirm(message="改用「最小对话请求」检测？需要输入一个模型名", default=True, keybindings=_KB).execute():
            model = inq.text(message="模型名（如 deepseek-chat / llama3 / qwen-plus）", keybindings=_KB).execute()
            if model:
                c = probe_chat(provider.get("api_key"), provider.get("base_url"), model)
                if c["ok"]:
                    print(f"{_GREEN}✓ 连接成功（model={model}）{_RESET}", file=sys.stdout)
                    _set_default_model(pid, model)
                else:
                    print(f"{_YELLOW}✗ chat 检测失败：{c['error']}{_RESET}", file=sys.stdout)
    except KeyboardInterrupt:
        return  # 左键/Ctrl-C → 回子菜单


def _pick_default_model(inq, pid, models) -> None:
    """让用户从探测到的模型里选一个默认（或手动输入 / 清除 / 取消）。"""
    cur = get_app_config().get(f"default_model:{pid}")
    unique = sorted(set(models))[:30]
    choices = []
    if cur and cur not in unique:
        choices.append({"name": f"● {cur}（当前默认）", "value": cur})
    choices += [{"name": f"● {m}", "value": m} for m in unique]
    choices += [
        {"name": "⌨ 手动输入模型名", "value": "__manual__"},
        {"name": "不设置（清除默认）", "value": "__clear__"},
        {"name": "← 取消", "value": "__cancel__"},
    ]
    pick = inq.select(message=f"选择 {pid} 的默认模型（← 取消）", choices=choices, keybindings=_KB).execute()
    if pick == "__cancel__":
        return
    if pick == "__clear__":
        _set_default_model(pid, None)
        return
    if pick == "__manual__":
        model = inq.text(message="模型名", keybindings=_KB).execute()
        if model:
            _set_default_model(pid, model)
        return
    _set_default_model(pid, pick)


def _set_default_model(pid: str, model_name: "str | None") -> None:
    """设置 / 清除某供应商的默认模型。

    持久化到 app_config.json（default_model:{pid}），同时 dedup 写回 models 表
    （供 list_models 本地回退用）；清除时只删配置、不删 DB 行。
    """
    if model_name:
        set_app_config(f"default_model:{pid}", model_name)
        if not get_model_by_provider_and_name(pid, model_name):
            insert_model(provider_id=pid, model_name=model_name)
        print(f"{_GREEN}✓ 已设置 {pid} 的默认模型：{model_name}{_RESET}", file=sys.stdout)
    else:
        remove_app_config(f"default_model:{pid}")
        print(f"{_YELLOW}✓ 已清除 {pid} 的默认模型{_RESET}", file=sys.stdout)


def _wizard_transcriber(inq) -> None:
    try:
        while True:
            cfg = TranscriberConfigManager().get_config()
            cur = f"{cfg['transcriber_type']} / {cfg['whisper_model_size']}"
            _show_header("② 语音转写引擎")
            cur_engine = cfg["transcriber_type"]
            choices = []
            for val, base in (
                ("fast-whisper", "fast-whisper（本地）"),
                ("groq", "groq（云端，需 key）"),
                ("bcut", "bcut（云端）"),
                ("kuaishou", "kuaishou（云端）"),
                ("mlx-whisper", "mlx-whisper（仅 macOS，GPU）"),
                ("funasr", "funasr（中文最优，VAD+标点）"),
            ):
                # 注意：InquirerPy 选择项 name 里不能嵌 ANSI 转义码（会原样显示），用纯文本
                if val == cur_engine:
                    mark = "  ✓ 当前"
                    if val in ("fast-whisper", "mlx-whisper"):
                        mark += f"  尺寸 {cfg['whisper_model_size']}"
                else:
                    mark = ""
                choices.append({"name": base + mark, "value": val})
            choices.append(
                {"name": f"音频预处理（16kHz 归一 + 超长分块）：{'开' if cfg.get('enable_preprocess') else '关'}", "value": "preprocess"}
            )
            choices.append(
                {"name": f"说话人分离（pyannote，可选）：{'开' if cfg.get('diarization') else '关'}", "value": "diarization"}
            )
            choices.append({"name": "← 返回主菜单", "value": "back"})
            pick = inq.select(
                message=f"当前引擎：{cur}",
                choices=choices,
                # 与 _TRANSCRIBER_ENGINES 同源：漏 funasr 会让当前引擎为 funasr 的用户
                # 回车确认时被光标默认位切回 fast-whisper（#124 A1）
                default=cur_engine if cur_engine in _TRANSCRIBER_ENGINES else "fast-whisper",
                keybindings=_KB,
            ).execute()
            if pick == "back":
                return
            if pick == "preprocess":
                _show_header("音频预处理")
                print(
                    f"{_DIM}转写前先把音频归一化为 16kHz mono wav；超长音频自动分块（云端引擎受益；"
                    f"faster-whisper 自带 VAD 也有帮助）。零额外依赖。{_RESET}",
                    file=sys.stdout,
                )
                cur_on = bool(cfg.get("enable_preprocess", False))
                on = inq.confirm(message="启用音频预处理？", default=cur_on, keybindings=_KB).execute()
                TranscriberConfigManager().update_config(cfg["transcriber_type"], enable_preprocess=bool(on))
                print(f"{_GREEN}✓ 音频预处理：{'开' if on else '关'}{_RESET}", file=sys.stdout)
                continue
            if pick == "diarization":
                _show_header("说话人分离")
                print(
                    f"{_DIM}用 pyannote 给转写标说话人（会议纪要/多人口播）。需要 torch + HF_TOKEN + 在"
                    f"huggingface.co 同意模型授权（重依赖，可选安装）。{_RESET}",
                    file=sys.stdout,
                )
                cur_on = bool(cfg.get("diarization", False))
                on = inq.confirm(message="启用说话人分离？", default=cur_on, keybindings=_KB).execute()
                if on:
                    import importlib.util

                    if importlib.util.find_spec("pyannote") is None:
                        print(
                            f"{_YELLOW}⚠ 当前环境未装 pyannote（可选依赖）。{_RESET}",
                            file=sys.stdout,
                        )
                        print(
                            f"{_DIM}`uv tool install --from git+https://github.com/HuangYincan/VideoNote-MCP videonote "
                            f"--with pyannote.audio --with torch`，或用 `uvx --from ... --with pyannote.audio --with torch` 运行。{_RESET}",
                            file=sys.stdout,
                        )
                    hf = inq.secret(message="HuggingFace token（HF_TOKEN，留空跳过）", keybindings=_KB).execute()
                    if hf:
                        set_app_config("hf_token", hf)
                TranscriberConfigManager().update_config(cfg["transcriber_type"], diarization=bool(on))
                print(f"{_GREEN}✓ 说话人分离：{'开' if on else '关'}{_RESET}", file=sys.stdout)
                continue
            if pick in ("fast-whisper", "mlx-whisper"):
                _show_header(f"选择 {pick} 模型尺寸")
                sizes = [{"name": s, "value": s} for s in _WHISPER_SIZES]
                sizes.append({"name": "← 取消", "value": "back"})
                size = inq.select(message="模型尺寸", choices=sizes, default=cfg["whisper_model_size"], keybindings=_KB).execute()
                if size == "back":
                    continue
                TranscriberConfigManager().update_config(pick, size)
                print(f"{_GREEN}✓ 已切换 {pick} / {size}{_RESET}", file=sys.stdout)
                print(f"{_DIM}（检查模型是否已下载…）{_RESET}", file=sys.stdout)
                # 本地引擎：检查模型是否已下载，未下载则询问是否现在下载
                from app.utils.model_status import check_mlx_whisper_model_exists, check_whisper_model_exists

                if pick == "fast-whisper":
                    downloaded = check_whisper_model_exists(size, "whisper")
                    dl_fn = lambda: _download_whisper(size)
                    label = f"whisper-{size}"
                    mlx_missing = False
                else:  # mlx-whisper
                    # mlx-whisper 是可选依赖；用 find_spec 轻量判断（避免 import mlx_whisper 卡顿）
                    import importlib.util

                    mlx_missing = importlib.util.find_spec("mlx_whisper") is None
                    if mlx_missing:
                        print(
                            f"{_YELLOW}⚠ 当前环境未装 mlx-whisper（可选依赖）。{_RESET}"
                            f"{_DIM}想用 mlx：`uv tool install --from git+https://github.com/HuangYincan/VideoNote-MCP videonote --with mlx-whisper`，"
                            f"或用 `uvx --from ... --with mlx-whisper` 运行。{_RESET}",
                            file=sys.stdout,
                        )
                        if inq.confirm(message="改用 fast-whisper（当前环境可用）？", default=True, keybindings=_KB).execute():
                            TranscriberConfigManager().update_config("fast-whisper", size)
                            pick, mlx_missing = "fast-whisper", False
                            downloaded = check_whisper_model_exists(size, "whisper")
                            dl_fn = lambda: _download_whisper(size)
                            label = f"whisper-{size}"
                        else:
                            continue  # 回引擎选择
                    else:
                        downloaded = check_mlx_whisper_model_exists(size)
                        dl_fn = lambda: _download_mlx_model(size)
                        label = f"mlx-whisper-{size}"
                if downloaded:
                    # 已下载：显示位置 + 可卸载 + 暂留
                    _show_header(f"{label} 已下载")
                    print(f"{_GREEN}✓ {label} 已下载{_RESET}", file=sys.stdout)
                    _show_uninstall_option(inq, pick, size, label)
                    try:
                        input("（按回车返回）", )
                    except (EOFError, KeyboardInterrupt):
                        pass
                    continue
                elif inq.confirm(message=f"本地模型 {label} 尚未下载，现在下载？（约几十MB~数GB）", default=False, keybindings=_KB).execute():
                    # 专门的下载界面：进度条 + 完成后停留 + 位置/卸载，避免立刻跳回
                    _show_header(f"下载 {label}")
                    print("", file=sys.stdout)
                    try:
                        dl_fn()
                        print(f"{_GREEN}✓ {label} 下载完成{_RESET}", file=sys.stdout)
                        _show_uninstall_option(inq, pick, size, label)
                    except Exception as e:
                        print(f"{_YELLOW}⚠ 下载失败：{e}（可稍后 `videonote transcriber download {size}` 重试）{_RESET}", file=sys.stdout)
                    try:
                        input("（按回车返回）", )
                    except (EOFError, KeyboardInterrupt):
                        pass
            else:
                TranscriberConfigManager().update_config(pick)
                print(f"{_GREEN}✓ 已切换 {pick}{_RESET}", file=sys.stdout)
                if pick == "funasr":
                    import importlib.util

                    if importlib.util.find_spec("funasr") is None:
                        print(
                            f"{_YELLOW}⚠ 当前环境未装 funasr（可选重依赖）。{_RESET}",
                            file=sys.stdout,
                        )
                        print(
                            f"{_DIM}`uv tool install --from git+https://github.com/HuangYincan/VideoNote-MCP videonote "
                            f"--with funasr --with torch`，或用 `uvx --from ... --with funasr --with torch` 运行。{_RESET}",
                            file=sys.stdout,
                        )
    except KeyboardInterrupt:
        return  # 左键/Ctrl-C → 返回主菜单


def _wizard_other(inq) -> None:
    try:
        while True:
            from app.services.cookie_manager import CookieConfigManager

            notes_dir = get_app_config().get("notes_dir") or os.environ.get("VIDEONOTE_NOTES_DIR") or "（默认 note_results/{task_id}/）"
            vu_on = bool(get_app_config().get("video_understanding", False))
            vu_int = resolve_int_config("video_interval", "VIDEONOTE_VIDEO_INTERVAL", 6)
            cm_on = bool(get_app_config().get("include_comments", False))
            cm_lim = resolve_int_config("comments_limit", "VIDEONOTE_COMMENTS_LIMIT", 20)
            st_style = get_app_config().get("default_style") or "detailed"
            ss_on = bool(get_app_config().get("default_screenshot", False))
            ad_on = bool(get_app_config().get("agent_direct", False))
            _show_header("③ 其他设置")
            pick = inq.select(
                message="选择要配置的项（← 返回）",
                choices=[
                    {"name": "B 站扫码登录（自动获取 SESSDATA，AI 字幕用）", "value": "bili-login"},
                    {"name": "平台 Cookie（手动填，B 站等需登录内容）", "value": "cookie"},
                    {"name": f"默认笔记位置（图片模式）：{notes_dir}", "value": "notes"},
                    {"name": f"视频理解默认（{'开' if vu_on else '关'} / {vu_int}s，需多模态模型）", "value": "video"},
                    {"name": f"评论/弹幕整合默认（{'开' if cm_on else '关'} / {cm_lim}条，需 SESSDATA）", "value": "comments"},
                    {"name": f"笔记默认（风格 {st_style} / 截图 {'开' if ss_on else '关'} / AGENT直接写 {'开' if ad_on else '关'}）", "value": "note-default"},
                    {"name": f"导出格式默认（生成后自动导出：{_format_list_display(get_app_config().get('default_export_formats'))}）", "value": "export-default"},
                    {"name": "← 返回主菜单", "value": "back"},
                ],
                keybindings=_KB,
            ).execute()
            if pick == "back":
                return
            if pick == "bili-login":
                _login_cli([], exit_on_fail=False)
            elif pick == "cookie":
                _show_header("平台 Cookie")
                platform = inq.select(
                    message="平台",
                    choices=[
                        {"name": "bilibili", "value": "bilibili"},
                        {"name": "youtube", "value": "youtube"},
                        {"name": "douyin", "value": "douyin"},
                        {"name": "kuaishou", "value": "kuaishou"},
                        {"name": "其他（手动输入）", "value": "other"},
                        {"name": "← 返回", "value": "back"},
                    ],
                    keybindings=_KB,
                ).execute()
                if platform == "back":
                    continue
                if platform == "other":
                    platform = inq.text(message="平台名", keybindings=_KB).execute()
                cookie = inq.secret(message=f"{platform} 的 Cookie 值（留空取消）", keybindings=_KB).execute()
                if platform and cookie:
                    CookieConfigManager().set(platform, cookie)
                    print(f"{_GREEN}✓ 已保存 {platform} 的 Cookie{_RESET}", file=sys.stdout)
                else:
                    print(f"{_YELLOW}⚠ 未保存（平台或 Cookie 为空）{_RESET}", file=sys.stdout)
            elif pick == "notes":
                cur = get_app_config().get("notes_dir") or "（默认）"
                _show_header("默认笔记位置")
                new_dir = inq.text(message=f"当前：{cur}。输入新目录（留空=保持默认）", keybindings=_KB).execute()
                if new_dir:
                    set_app_config("notes_dir", new_dir)
                    print(f"{_GREEN}✓ 已保存默认笔记位置：{new_dir}{_RESET}", file=sys.stdout)
            elif pick == "video":
                _show_header("视频理解默认")
                print(f"{_DIM}视频理解把画面按间隔抽帧发给多模态 LLM（需 qwen-vl / gpt-4o 等；会下载整个视频、比纯转写慢）。{_RESET}", file=sys.stdout)
                cur_on = bool(get_app_config().get("video_understanding", False))
                cur_int = resolve_int_config("video_interval", "VIDEONOTE_VIDEO_INTERVAL", 6)
                on = inq.confirm(message="默认开启视频理解？", default=cur_on, keybindings=_KB).execute()
                set_app_config("video_understanding", bool(on))
                iv = inq.text(message=f"帧间隔秒数（当前 {cur_int}，默认 6）", keybindings=_KB).execute()
                if iv:
                    try:
                        iv = max(1, int(iv))
                    except ValueError:
                        iv = 6
                    set_app_config("video_interval", iv)
                else:
                    iv = cur_int
                print(f"{_GREEN}✓ 已保存视频理解默认：{'开' if on else '关'} / {iv}s{_RESET}", file=sys.stdout)
            elif pick == "comments":
                _show_header("评论/弹幕整合默认")
                print(f"{_DIM}把弹幕+评论区观点整合进笔记（需 B 站 SESSDATA；没配则评论拿不到，任务不阻断）。{_RESET}", file=sys.stdout)
                cur_on = bool(get_app_config().get("include_comments", False))
                cur_lim = resolve_int_config("comments_limit", "VIDEONOTE_COMMENTS_LIMIT", 20)
                on = inq.confirm(message="默认整合弹幕+评论区观点？", default=cur_on, keybindings=_KB).execute()
                set_app_config("include_comments", bool(on))
                lim = inq.text(message=f"评论条数（当前 {cur_lim}，默认 20）", keybindings=_KB).execute()
                if lim:
                    try:
                        lim = max(1, int(lim))
                    except ValueError:
                        lim = 20
                    set_app_config("comments_limit", lim)
                else:
                    lim = cur_lim
                print(f"{_GREEN}✓ 已保存评论/弹幕整合默认：{'开' if on else '关'} / {lim}条{_RESET}", file=sys.stdout)
                print(f"{_DIM}需 SESSDATA（`videonote login bilibili`），没配则评论拿不到。{_RESET}", file=sys.stdout)
            elif pick == "note-default":
                _show_header("笔记默认")
                print(f"{_DIM}不传 style / screenshot 参数时用这里的默认值；AGENT 直接写笔记绕过配置的 LLM。{_RESET}", file=sys.stdout)
                style_map = [
                    ("minimal", "minimal 精简"),
                    ("detailed", "detailed 详细"),
                    ("academic", "academic 学术"),
                    ("tutorial", "tutorial 教程"),
                    ("xiaohongshu", "xiaohongshu 小红书"),
                    ("life_journal", "life_journal 生活向"),
                    ("task_oriented", "task_oriented 任务导向"),
                    ("business", "business 商业风格"),
                    ("meeting_minutes", "meeting_minutes 会议纪要"),
                ]
                cur_style = get_app_config().get("default_style") or "detailed"
                style = inq.select(
                    message=f"默认笔记风格（当前 {cur_style}）",
                    choices=[{"name": n, "value": v} for v, n in style_map],
                    default=cur_style,
                    keybindings=_KB,
                ).execute()
                set_app_config("default_style", style)
                ss = inq.confirm(
                    message="默认开启截图？",
                    default=bool(get_app_config().get("default_screenshot", False)),
                    keybindings=_KB,
                ).execute()
                set_app_config("default_screenshot", bool(ss))
                ad = inq.confirm(
                    message="默认用 AGENT 直接写笔记（不走配置 LLM）？",
                    default=bool(get_app_config().get("agent_direct", False)),
                    keybindings=_KB,
                ).execute()
                set_app_config("agent_direct", bool(ad))
                print(f"{_GREEN}✓ 已保存笔记默认：风格 {style} / 截图 {'开' if ss else '关'} / AGENT直接写 {'开' if ad else '关'}{_RESET}", file=sys.stdout)
            elif pick == "export-default":
                _show_header("导出格式默认")
                print(f"{_DIM}笔记/素材任务成功后，自动把转写导出为这些格式（确定性渲染，不耗 LLM）。srt/vtt 是字幕文件，json 是结构化转写。{_RESET}", file=sys.stdout)
                cur = get_app_config().get("default_export_formats") or []
                choices = [
                    {"name": "srt（字幕，标准 SubRip）", "value": "srt"},
                    {"name": "vtt（字幕，WebVTT）", "value": "vtt"},
                    {"name": "json（结构化转写）", "value": "json"},
                ]
                picked = inq.checkbox(
                    message="选择导出格式（空格勾选，留空 = 不自动导出）",
                    choices=[{"name": c["name"], "value": c["value"], "checked": c["value"] in cur} for c in choices],
                    keybindings=_KB,
                ).execute()
                if picked:
                    set_app_config("default_export_formats", list(picked))
                    print(f"{_GREEN}✓ 已保存导出格式默认：{','.join(picked)}{_RESET}", file=sys.stdout)
                else:
                    remove_app_config("default_export_formats")
                    print(f"{_YELLOW}⚠ 已清除导出格式默认（任务成功不再自动导出）{_RESET}", file=sys.stdout)
    except KeyboardInterrupt:
        return  # 左键/Ctrl-C → 返回主菜单


def _wizard_data(inq) -> None:
    """setup ④ 数据管理：查看任务列表 / 清理单任务 / 全局清理。"""
    try:
        while True:
            _show_header("④ 数据管理")
            pick = inq.select(
                message="选择操作（← 返回）",
                choices=[
                    {"name": "查看任务列表（task_id | 标题 | 状态）", "value": "list"},
                    {"name": "清理单个任务", "value": "cleanup-one"},
                    {"name": "全局清理（清空所有任务产物）", "value": "cleanup-all"},
                    {"name": "← 返回主菜单", "value": "back"},
                ],
                keybindings=_KB,
            ).execute()
            if pick == "back":
                return
            if pick == "list":
                _wizard_data_list(inq)
            elif pick == "cleanup-one":
                _wizard_data_cleanup_one(inq)
            else:
                _wizard_data_cleanup_all(inq)
    except KeyboardInterrupt:
        return


def _wizard_data_list(inq) -> None:
    """列出全局索引里的任务（带语义标题/状态）。"""
    from app.db.video_task_dao import list_tasks as _list

    tasks = _list()
    if not tasks:
        print(f"{_DIM}暂无任务记录（尚未生成过笔记/素材）{_RESET}", file=sys.stdout)
        _press_any_key()
        return
    _show_header("任务列表")
    for t in tasks:
        created = (t.get("created_at") or "")[:16]
        title = (t.get("title") or "（无标题）")[:50]
        print(f"  {t['task_id'][:8]}  {t.get('status','')[:9]:9}  {created}  {title}", file=sys.stdout)
    print(f"{_DIM}共 {len(tasks)} 个任务（task_id 显示前 8 位）{_RESET}", file=sys.stdout)
    _press_any_key()


_TERMINAL_STATUSES = ("SUCCESS", "FAILED", "CANCELLED")


def _list_running_tasks() -> List[str]:
    """返回状态仍为「进行中」的任务（读 note_results/{task_id}/status.json）。

    MCP 侧 cleanup_note/cleanup_all 用进程内 _task_futures 守卫；CLI 是独立进程
    拿不到，改以磁盘 status.json 为准（独立步骤任务也写它，比 video_tasks 表全）。
    进程被杀会残留中间状态——调用方须二次确认后才能清理（交互式 CLI 允许用户
    判断后强清，与 MCP 的硬拒绝区别于此）。
    """
    import glob

    from app.utils.task_manifest import get_note_dir

    running = []
    for sp in glob.glob(str(get_note_dir() / "*" / "status.json")):
        try:
            data = json.loads(Path(sp).read_text(encoding="utf-8"))
        except Exception:
            continue
        st = str(data.get("status") or "").upper()
        if st not in _TERMINAL_STATUSES:
            running.append(Path(sp).parent.name)
    return sorted(running)


def _wizard_data_cleanup_one(inq) -> None:
    """选一个任务 → 确认清理（默认保留最终笔记）。"""
    from app.db.video_task_dao import list_tasks as _list
    from app.utils.task_manifest import cleanup_task_files, list_task_files

    tasks = _list()
    if not tasks:
        print(f"{_DIM}暂无任务记录{_RESET}", file=sys.stdout)
        _press_any_key()
        return
    choices = []
    for t in tasks:
        title = (t.get("title") or "（无标题）")[:40]
        status = t.get("status") or ""
        choices.append(
            {"name": f"{t['task_id'][:8]}  [{status[:8]}]  {title}", "value": t["task_id"]}
        )
    choices.append({"name": "← 取消", "value": "back"})
    tid = inq.select(message="选择要清理的任务（← 取消）", choices=choices, keybindings=_KB).execute()
    if tid == "back":
        return

    files = list_task_files(tid)
    print(f"{_DIM}该任务占用：{len(files.get('existing', []))} 个文件/目录{_RESET}", file=sys.stdout)
    # 运行中任务守卫（#122 A6，与 MCP cleanup_note 的拒绝行为对齐）：
    # 直接清理会删掉下载器/转写器正在写的目录。CLI 看不到 MCP 内存状态，
    # 以磁盘 status.json 为准；非终态要求用户显式确认（可能被 MCP 进程占用）。
    _task_status = ""
    try:
        from app.utils.task_manifest import get_note_dir

        _task_status = str(
            json.loads((get_note_dir() / str(tid) / "status.json").read_text(encoding="utf-8")).get("status", "")
        ).upper()
    except Exception:
        pass
    if _task_status and _task_status not in _TERMINAL_STATUSES:
        if not inq.confirm(
            message=f"任务 {tid[:8]} 状态为 {_task_status}（可能仍在运行）——确认清理？",
            default=False,
            keybindings=_KB,
        ).execute():
            print(f"{_YELLOW}已取消清理（任务仍在运行）{_RESET}", file=sys.stdout)
            _press_any_key()
            return
    include_note = inq.confirm(
        message="连最终笔记一起删？[n=保留笔记]",
        default=False,
        keybindings=_KB,
    ).execute()
    if not inq.confirm(
        message=f"确认清理任务 {tid[:8]}？{'（连笔记）' if include_note else '（保留笔记）'}",
        default=False,
        keybindings=_KB,
    ).execute():
        print(f"{_YELLOW}已取消清理{_RESET}", file=sys.stdout)
        return
    r = cleanup_task_files(tid, include_note=include_note)
    print(
        f"{_GREEN}✓ 已清理：删除 {len(r.get('deleted', []))} 项，"
        f"笔记{'保留' if r.get('note_kept') else '已删'}{_RESET}",
        file=sys.stdout,
    )
    for p in r.get("notes_kept_outside", []):
        print(
            f"{_YELLOW}⚠ 数据目录外的便携笔记未删除（沙箱红线）：{p}{_RESET}",
            file=sys.stdout,
        )
    _press_any_key()


def _wizard_data_cleanup_all(inq) -> None:
    """全局清理（恢复出厂）。"""
    from app.utils.task_manifest import cleanup_all_files

    _show_header("全局清理")
    print(
        f"{_YELLOW}⚠ 将清空 note_results/（所有任务产物）、static/screenshots/、note_cache/。"
        f"logs/ 保留（运行日志不属任务产物）。{_RESET}",
        file=sys.stdout,
    )
    # 运行中任务守卫（#122 A6，与 MCP cleanup_all 的拒绝行为对齐）：全局清空会把
    # 运行中任务的目录一并删掉。CLI 看不到 MCP 内存状态，以磁盘 status.json 为准。
    running = _list_running_tasks()
    if running:
        shown = ", ".join(t[:8] for t in running[:8])
        if len(running) > 8:
            shown += f" 等 {len(running)} 个"
        print(
            f"{_YELLOW}⚠ {len(running)} 个任务状态未终态（可能仍在运行）：{shown}{_RESET}",
            file=sys.stdout,
        )
        if not inq.confirm(
            message="有任务可能仍在运行——仍要全局清理？",
            default=False,
            keybindings=_KB,
        ).execute():
            print(f"{_YELLOW}已取消全局清理（任务仍在运行）{_RESET}", file=sys.stdout)
            _press_any_key()
            return
    include_config = inq.confirm(message="连 config/（LLM key / cookie）一起清？", default=False, keybindings=_KB).execute()
    include_models = inq.confirm(message="连 models/（已下载模型）一起清？", default=False, keybindings=_KB).execute()
    # 模型下载中守卫（#123 A1）：huggingface 下载中的文件带 .incomplete 后缀（cache_dir
    # 的 blobs/ 与 local_dir 两种布局都会被覆盖）。CLI 独立进程看不到 MCP 端的内存下载态，
    # 但磁盘残留能兜底「另一个进程正在下载模型」的竞态——删 models/ 会打断下载。
    incomplete = []
    if include_models:
        try:
            from app.utils.path_helper import get_model_dir

            models_root = os.path.dirname(get_model_dir("whisper"))
            if os.path.isdir(models_root):
                incomplete = [str(p) for p in Path(models_root).rglob("*.incomplete")]
        except Exception:
            incomplete = []
    if incomplete:
        print(
            f"{_YELLOW}⚠ 检测到 {len(incomplete)} 个未完成的模型下载文件（.incomplete）——"
            "可能有模型正在下载中，删除 models/ 会打断下载。{_RESET}",
            file=sys.stdout,
        )
        if not inq.confirm(
            message="仍有模型下载未完成——仍要连 models/ 一起清？",
            default=False,
            keybindings=_KB,
        ).execute():
            print(f"{_YELLOW}已取消全局清理（模型下载未完成）{_RESET}", file=sys.stdout)
            _press_any_key()
            return
    if not inq.confirm(message="确认全局清理？此操作不可撤销", default=False, keybindings=_KB).execute():
        print(f"{_YELLOW}已取消全局清理{_RESET}", file=sys.stdout)
        return
    cleanup_all_files(include_config=include_config, include_models=include_models)
    print(f"{_GREEN}✓ 全局清理完成{_RESET}", file=sys.stdout)
    _press_any_key()


def _press_any_key() -> None:
    try:
        input("（按回车返回）", )
    except (EOFError, KeyboardInterrupt, OSError):
        pass


def _fallback_test_and_default(pid: str) -> None:
    """纯文本兜底：检测连接 + 选默认模型（镜像 _test_and_set_default）。"""
    provider = ProviderService.get_provider_by_id(pid)
    if not provider:
        print(f"   ⚠ 供应商 {pid} 不存在", file=sys.stdout)
        return
    print(f"   检测连接 {pid}…", file=sys.stdout)
    r = probe_models(provider.get("api_key"), provider.get("base_url"), name=provider.get("name", ""))
    if not r["ok"]:
        print(f"   ✗ 无法获取模型列表：{r['error']}", file=sys.stdout)
        m = _ask("   改用 chat 检测？输入模型名（空=跳过）")
        if m:
            c = probe_chat(provider.get("api_key"), provider.get("base_url"), m)
            if c["ok"]:
                print(f"   ✓ 连接成功（model={m}）", file=sys.stdout)
                _set_default_model(pid, m)
            else:
                print(f"   ✗ chat 检测失败：{c['error']}", file=sys.stdout)
        return
    print(f"   ✓ 连接成功：{len(r['models'])} 个模型", file=sys.stdout)
    models = sorted(set(r["models"]))[:10]
    for i, m in enumerate(models, 1):
        print(f"   {i}) {m}", file=sys.stdout)
    if len(r["models"]) > 10:
        print(f"   … 共 {len(r['models'])} 个，仅显示前 10", file=sys.stdout)
    # 回车=保持现状（此前 `if not sel: _set_default_model(pid, None)` 把「跳过」
    # 当「清除」——管道/EOF 场景跑一次 setup 就误删已设默认，#120）；清除要显式 clear
    sel = _ask("   选默认模型 [1-%d]，0=手动输入，clear=清除默认，回车=保持" % len(models), default="")
    if not sel:
        pass
    elif sel == "clear":
        _set_default_model(pid, None)
    elif sel == "0":
        m = _ask("   手动输入模型名")
        if m:
            _set_default_model(pid, m)
    elif sel.isdigit() and 1 <= int(sel) <= len(models):
        _set_default_model(pid, models[int(sel) - 1])
    else:
        print("   ⚠ 无效选择，未设置", file=sys.stdout)


def _setup_cli_fallback() -> None:
    """无 InquirerPy 时的纯文本兜底向导（同功能，输入编号选择）。"""
    print("=== VideoNote-MCP 配置（纯文本模式） ===", file=sys.stdout)

    provs = ProviderService.get_all_providers_safe()
    if provs:
        print("\n① LLM 供应商（当前）：", file=sys.stdout)
        for i, p in enumerate(provs, 1):
            dm = get_app_config().get(f"default_model:{p['id']}")
            suffix = f"  默认={dm}" if dm else ""
            print(f"   {i}) {p['id']}  key={'已填' if p['api_key'] else '空'}  {p['base_url']}{suffix}", file=sys.stdout)
        sel = _ask("   选择要管理的 [1-%d]，0 跳过" % len(provs), default="0")
        if sel.isdigit() and 1 <= int(sel) <= len(provs):
            pid = provs[int(sel) - 1]["id"]
            key = _ask_secret(f"   新的 API key（{pid}，留空不变）")
            if key:
                try:
                    ProviderService.update_provider(pid, {"api_key": key})
                    print(f"   ✓ 已更新 {pid} 的 key", file=sys.stdout)
                except ValueError as e:
                    print(f"   ✗ {e}", file=sys.stdout)
            base_url = _ask(f"   新的 base_url（{pid}，留空不变）")
            if base_url:
                try:
                    ProviderService.update_provider(pid, {"base_url": base_url})
                except ValueError as e:
                    print(f"   ✗ {e}", file=sys.stdout)
            _fallback_test_and_default(pid)
    if _ask("   新增中转站/自建供应商？[y/N]", default="N").lower() == "y":
        name = _ask("   供应商名称", default="我的中转站")
        base_url = _ask("   base_url")
        key = _ask_secret("   API key")
        if name and base_url and key:
            new_id = ProviderService.add_provider(name=name, api_key=key, base_url=base_url, logo="custom", type_="custom")
            print(f"   ✓ 已新增 → id={new_id}", file=sys.stdout)

    print("\n② 语音转写引擎：", file=sys.stdout)
    print("   1) fast-whisper  2) groq  3) bcut  4) kuaishou  5) mlx-whisper  6) funasr（中文最优，VAD+标点）", file=sys.stdout)
    t = _ask("   选择 [1-6]", default="1")
    engines = ("fast-whisper", "groq", "bcut", "kuaishou", "mlx-whisper", "funasr")
    eng = engines[int(t) - 1] if t.isdigit() and 1 <= int(t) <= 6 else "fast-whisper"
    size = None
    if eng in ("fast-whisper", "mlx-whisper"):
        size = _ask("   模型尺寸（tiny/base/small/medium/large-v3/large-v3-turbo）", default="small")
        if size not in _WHISPER_SIZES:
            size = "small"
    if eng == "funasr":
        import importlib.util

        if importlib.util.find_spec("funasr") is None:
            print("   ⚠ 未装 funasr：`uvx --from ... --with funasr --with torch` 安装", file=sys.stdout)
    TranscriberConfigManager().update_config(eng, size)
    print(f"   ✓ 已切换 {eng} / {size}", file=sys.stdout)
    if eng == "fast-whisper" and _ask(f"   下载 whisper-{size}？[y/N]", default="N").lower() == "y":
        try:
            _download_whisper(size)
        except Exception as e:
            print(f"   ⚠ 下载失败：{e}", file=sys.stdout)
    print("   音频预处理：转写前把音频归一化为 16kHz mono wav，超长自动分块（零额外依赖）", file=sys.stdout)
    cur_pre = bool(TranscriberConfigManager().get_config().get("enable_preprocess", False))
    pre = _ask(f"   启用音频预处理？[y/N]（当前 {'开' if cur_pre else '关'}）", default="Y" if cur_pre else "N").lower() == "y"
    TranscriberConfigManager().update_config(eng, enable_preprocess=bool(pre))
    print(f"   ✓ 音频预处理：{'开' if pre else '关'}", file=sys.stdout)
    print("   说话人分离：pyannote 给转写标说话人（重依赖：torch + HF_TOKEN + 模型授权）", file=sys.stdout)
    cur_dia = bool(TranscriberConfigManager().get_config().get("diarization", False))
    dia = _ask(f"   启用说话人分离？[y/N]（当前 {'开' if cur_dia else '关'}）", default="Y" if cur_dia else "N").lower() == "y"
    if dia:
        import importlib.util

        if importlib.util.find_spec("pyannote") is None:
            print("   ⚠ 未装 pyannote：`uvx --from ... --with pyannote.audio --with torch` 安装", file=sys.stdout)
    TranscriberConfigManager().update_config(eng, diarization=bool(dia))
    print(f"   ✓ 说话人分离：{'开' if dia else '关'}", file=sys.stdout)

    print("\n③ 其他（视频理解默认 / 评论·弹幕整合默认 / 笔记默认）：", file=sys.stdout)
    print("   视频理解把画面按间隔抽帧发给多模态 LLM（需 qwen-vl / gpt-4o 等；会下载整个视频、比纯转写慢）", file=sys.stdout)
    cur_on = bool(get_app_config().get("video_understanding", False))
    cur_int = resolve_int_config("video_interval", "VIDEONOTE_VIDEO_INTERVAL", 6)
    on = _ask(f"   默认开启视频理解？[y/N]（当前 {'开' if cur_on else '关'}）", default="N").lower() == "y"
    set_app_config("video_understanding", bool(on))
    iv = _ask(f"   帧间隔秒数（当前 {cur_int}，默认 6）", default=str(cur_int))
    try:
        iv = max(1, int(iv))
    except ValueError:
        iv = 6
    set_app_config("video_interval", iv)
    print(f"   ✓ 视频理解默认：{'开' if on else '关'} / {iv}s", file=sys.stdout)
    print("   评论/弹幕整合默认：把弹幕+评论区观点整合进笔记（需 B 站 SESSDATA；没配则评论拿不到，任务不阻断）", file=sys.stdout)
    cur_on = bool(get_app_config().get("include_comments", False))
    cur_lim = resolve_int_config("comments_limit", "VIDEONOTE_COMMENTS_LIMIT", 20)
    on = _ask(f"   默认整合弹幕+评论区观点？[y/N]（当前 {'开' if cur_on else '关'}）", default="N").lower() == "y"
    set_app_config("include_comments", bool(on))
    lim = _ask(f"   评论条数（当前 {cur_lim}，默认 20）", default=str(cur_lim))
    try:
        lim = max(1, int(lim))
    except ValueError:
        lim = 20
    set_app_config("comments_limit", lim)
    print(f"   ✓ 评论/弹幕整合默认：{'开' if on else '关'} / {lim}条", file=sys.stdout)
    print("   需 SESSDATA（`videonote login bilibili`），没配则评论拿不到", file=sys.stdout)
    print("   笔记默认：不传 style/screenshot 参数时用这里的默认值；AGENT 直接写笔记绕过配置的 LLM", file=sys.stdout)
    _NOTE_STYLES = [
        "minimal 精简",
        "detailed 详细",
        "academic 学术",
        "tutorial 教程",
        "xiaohongshu 小红书",
        "life_journal 生活向",
        "task_oriented 任务导向",
        "business 商业风格",
        "meeting_minutes 会议纪要",
    ]
    cur_style = get_app_config().get("default_style") or "detailed"
    for i, s in enumerate(_NOTE_STYLES, 1):
        print(f"     {i}) {s}", file=sys.stdout)
    cur_idx = next((i for i, s in enumerate(_NOTE_STYLES, 1) if s.startswith(cur_style)), 2)
    sel = _ask(f"   默认笔记风格（当前 {cur_style}）", default=str(cur_idx))
    try:
        i = int(sel)
        style = _NOTE_STYLES[i - 1].split()[0] if 1 <= i <= len(_NOTE_STYLES) else "detailed"
    except (ValueError, IndexError):
        style = "detailed"
    set_app_config("default_style", style)
    cur_ss = bool(get_app_config().get("default_screenshot", False))
    ss = _ask(f"   默认开启截图？[y/N]（当前 {'开' if cur_ss else '关'}）", default="Y" if cur_ss else "N").lower() == "y"
    set_app_config("default_screenshot", bool(ss))
    ad = _ask("   默认用 AGENT 直接写笔记（不走配置 LLM）？[y/N]", default="N").lower() == "y"
    set_app_config("agent_direct", bool(ad))
    print(f"   ✓ 笔记默认：风格 {style} / 截图 {'开' if ss else '关'} / AGENT直接写 {'开' if ad else '关'}", file=sys.stdout)
    print("   导出格式默认：任务成功后自动把转写导出为纯格式（确定性渲染，不耗 LLM）", file=sys.stdout)
    print("     1) srt（字幕，标准 SubRip）  2) vtt（字幕，WebVTT）  3) json（结构化转写）", file=sys.stdout)
    print("     输入逗号分隔编号，如 1,3；留空 = 不自动导出", file=sys.stdout)
    cur_ex = get_app_config().get("default_export_formats") or []
    _EXPORT_OPTS = ["srt", "vtt", "json"]
    ex_sel = _ask(f"   选择导出格式（当前 {','.join(cur_ex) if cur_ex else '无'}）", default="")
    if ex_sel.strip():
        picked = []
        for part in ex_sel.split(","):
            part = part.strip()
            if part.isdigit() and 1 <= int(part) <= 3:
                picked.append(_EXPORT_OPTS[int(part) - 1])
        if picked:
            set_app_config("default_export_formats", picked)
            print(f"   ✓ 已保存导出格式默认：{','.join(picked)}", file=sys.stdout)
    elif cur_ex:
        remove_app_config("default_export_formats")
        print("   ✓ 已清除导出格式默认（任务成功不再自动导出）", file=sys.stdout)

    print("\n④ 数据管理（查看 / 清理任务产物）：", file=sys.stdout)
    try:
        from app.db.video_task_dao import list_tasks as _list_tasks

        tasks = _list_tasks()
        if not tasks:
            print("   暂无任务记录（尚未生成过笔记/素材）", file=sys.stdout)
        else:
            print(f"   共 {len(tasks)} 个任务：", file=sys.stdout)
            for i, t in enumerate(tasks, 1):
                title = (t.get("title") or "（无标题）")[:40]
                status = (t.get("status") or "")[:9]
                print(f"     {i}) {t['task_id'][:8]}  [{status}]  {title}", file=sys.stdout)
            sel = _ask(f"   清理哪个任务？[1-{len(tasks)}，0=跳过]", default="0")
            if sel.isdigit() and 1 <= int(sel) <= len(tasks):
                tid = tasks[int(sel) - 1]["task_id"]
                include_note = _ask(f"   连最终笔记一起删？[y/N]（{tid[:8]}）", default="N").lower() == "y"
                if _ask(f"   确认清理 {tid[:8]}？[y/N]", default="N").lower() == "y":
                    from app.utils.task_manifest import cleanup_task_files

                    r = cleanup_task_files(tid, include_note=include_note)
                    print(f"   ✓ 已清理：删除 {len(r.get('deleted', []))} 项，笔记{'保留' if r.get('note_kept') else '已删'}", file=sys.stdout)
    except Exception as e:
        print(f"   ⚠ 读取任务列表失败：{e}", file=sys.stdout)
    if _ask("   全局清理（清空所有任务产物）？[y/N]", default="N").lower() == "y":
        from app.utils.task_manifest import cleanup_all_files

        cleanup_all_files()
        print("   ✓ 已全局清理", file=sys.stdout)

    print("\n=== 配置完成 ===", file=sys.stdout)


def _providers_cli(argv) -> None:
    parser = argparse.ArgumentParser(
        prog="videonote providers",
        description="在终端管理 LLM 供应商（key 不经过 agent 对话，避免泄露给 agent 的 LLM 上游）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出供应商（key 掩码）")
    p_set = sub.add_parser("set", help="给供应商填 key / base_url / name")
    p_set.add_argument("provider_id")
    p_set.add_argument("--api-key", help="API key（会出现在 shell history；建议用 add 的交互输入填 key）")
    p_set.add_argument("--base-url", help="base_url")
    p_set.add_argument("--name", help="显示名")
    p_add = sub.add_parser("add", help="新增供应商（如中转站）；key 缺省走 getpass 交互输入")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--api-key", help="API key（缺省交互输入，不落 shell history / 进程列表）")
    p_add.add_argument("--base-url", required=True)
    p_add.add_argument("--type", default="custom")
    p_test = sub.add_parser("test", help="检测连接并列出可用模型（--default 设为默认模型）")
    p_test.add_argument("provider_id")
    p_test.add_argument("--default", help="把某个模型设为该供应商的默认模型")

    opts = parser.parse_args(argv)
    if opts.cmd == "list":
        rows = ProviderService.get_all_providers_safe()
        if not rows:
            print("（暂无供应商，可先启动一次 MCP 自动预置内置供应商）", file=sys.stdout)
            return
        for p in rows:
            key = f"已填 {p['api_key']}" if p["api_key"] else "空"
            dm = get_app_config().get(f"default_model:{p['id']}")
            suffix = f"  默认={dm}" if dm else ""
            print(f"{p['id']:10} {p['name']:12} key={key}  base_url={p['base_url']}{suffix}", file=sys.stdout)
    elif opts.cmd == "test":
        provider = ProviderService.get_provider_by_id(opts.provider_id)
        if not provider:
            print(f"供应商不存在: {opts.provider_id}", file=sys.stderr)
            sys.exit(1)
        r = probe_models(provider.get("api_key"), provider.get("base_url"), name=provider.get("name", ""))
        if not r["ok"]:
            print(f"✗ 连接失败：{r['error']}", file=sys.stdout)
            sys.exit(1)
        print(f"✓ 连接成功：{len(r['models'])} 个模型", file=sys.stdout)
        for m in sorted(set(r["models"]))[:30]:
            print(f"  {m}", file=sys.stdout)
        if len(r["models"]) > 30:
            print(f"  … 共 {len(r['models'])} 个，仅显示前 30", file=sys.stdout)
        if opts.default:
            _set_default_model(opts.provider_id, opts.default)
        sys.exit(0)
    elif opts.cmd == "set":
        data = {}
        if opts.api_key is not None:
            data["api_key"] = opts.api_key
        if opts.base_url is not None:
            data["base_url"] = opts.base_url
        if opts.name is not None:
            data["name"] = opts.name
        if not data:
            parser.error("至少提供 --api-key / --base-url / --name 之一")
        updated = None
        try:
            updated = ProviderService.update_provider(opts.provider_id, data)
        except ValueError as e:
            print(f"✗ {e}", file=sys.stderr)
            sys.exit(1)
        if not updated:
            print(f"更新失败：供应商 {opts.provider_id} 不存在", file=sys.stderr)
            sys.exit(1)
        print(f"已更新 {opts.provider_id} (enabled={updated.get('enabled')})", file=sys.stdout)
    elif opts.cmd == "add":
        # key 缺省走 getpass 交互：不落 shell history / 进程列表（docs/05 #45）
        key = opts.api_key
        if not key:
            key = _ask_secret(f"{opts.name} 的 API key（输入不回显）")
        try:
            new_id = ProviderService.add_provider(
                name=opts.name, api_key=key, base_url=opts.base_url, logo="custom", type_=opts.type
            )
        except ValueError as e:
            # 重名等业务错误：打印原因退出，不裸 traceback（向导有捕获、CLI 漏网，#124 A2）
            print(f"✗ {e}", file=sys.stderr)
            sys.exit(1)
        print(f"已新增 {opts.name} → id={new_id}", file=sys.stdout)


_TRANSCRIBER_ENGINES = ("fast-whisper", "groq", "bcut", "kuaishou", "mlx-whisper", "funasr")


def _transcriber_cli(argv) -> None:
    """`videonote transcriber ...`：在终端管理语音转写引擎。"""
    parser = argparse.ArgumentParser(
        prog="videonote transcriber",
        description="在终端管理语音转写引擎（fast-whisper 本地 / groq / bcut / kuaishou / mlx-whisper）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="查看当前转写引擎与就绪状态")
    p_set = sub.add_parser("set", help="切换转写引擎")
    p_set.add_argument("engine", choices=_TRANSCRIBER_ENGINES)
    p_set.add_argument("--size", help="whisper 模型尺寸（tiny/base/small/medium/large-v3）")
    p_dl = sub.add_parser("download", help="下载本地 whisper 模型")
    p_dl.add_argument("size", choices=_WHISPER_SIZES)
    p_dl.add_argument("--engine", default="fast-whisper", choices=("fast-whisper", "mlx-whisper"), help="fast-whisper（默认）或 mlx-whisper（macOS）")
    p_pre = sub.add_parser("preprocess", help="音频预处理开关（16kHz 归一 + 超长分块）")
    p_pre.add_argument("state", choices=("on", "off"), help="on=启用 / off=关闭")
    p_dia = sub.add_parser("diarization", help="说话人分离开关（pyannote 可选）")
    p_dia.add_argument("state", choices=("on", "off"), help="on=启用 / off=关闭")

    opts = parser.parse_args(argv)
    mgr = TranscriberConfigManager()
    if opts.cmd == "list":
        cfg = mgr.get_config()
        ready = mgr.is_model_ready()
        print(f"当前引擎: {cfg['transcriber_type']} / {cfg['whisper_model_size']}", file=sys.stdout)
        print(f"就绪: {'✓ ready' if ready['ready'] else '✗ ' + ready['reason']}", file=sys.stdout)
        print(f"音频预处理: {'开' if cfg.get('enable_preprocess') else '关'}", file=sys.stdout)
        print(f"说话人分离: {'开' if cfg.get('diarization') else '关'}", file=sys.stdout)
        print(f"可选引擎: {', '.join(_TRANSCRIBER_ENGINES)}", file=sys.stdout)
        print(f"whisper 尺寸: {', '.join(_WHISPER_SIZES)}", file=sys.stdout)
    elif opts.cmd == "set":
        if opts.engine in ("fast-whisper", "mlx-whisper") and not opts.size:
            opts.size = "small"
        if opts.size:
            # 与 MCP set_transcriber 同口径（#108）：非法尺寸被持久化后任务跑到
            # TRANSCRIBING 才因模型加载失败；下载白名单有 choices，set 是自由串
            from app.transcriber.whisper_models import resolve_whisper_model

            try:
                resolve_whisper_model(opts.size)
            except ValueError:
                print(
                    f"✗ 未知 whisper 模型尺寸: {opts.size!r}（可选: {', '.join(_WHISPER_SIZES)}，"
                    "或自定义模型名 / HF repo_id / 本地目录）",
                    file=sys.stderr,
                )
                sys.exit(1)
        cfg = mgr.update_config(opts.engine, opts.size)
        print(f"已切换: {cfg['transcriber_type']} / {cfg['whisper_model_size']}", file=sys.stdout)
        if opts.engine == "fast-whisper":
            print(f"（本地模型还需下载：videonote transcriber download {cfg['whisper_model_size']}）", file=sys.stdout)
    elif opts.cmd == "download":
        try:
            if opts.engine == "mlx-whisper":
                _download_mlx_model(opts.size)
            else:
                _download_whisper(opts.size)
        except Exception as e:
            print(f"✗ 下载失败: {e}（可稍后重试或换小尺寸）", file=sys.stderr)
            sys.exit(1)
    elif opts.cmd == "preprocess":
        on = opts.state == "on"
        cfg = mgr.update_config(mgr.get_config()["transcriber_type"], enable_preprocess=on)
        print(f"音频预处理: {'开' if cfg.get('enable_preprocess') else '关'}", file=sys.stdout)
        if on:
            print("（转写前会先把音频归一化为 16kHz mono wav；超长自动分块）", file=sys.stdout)
    elif opts.cmd == "diarization":
        on = opts.state == "on"
        cfg = mgr.update_config(mgr.get_config()["transcriber_type"], diarization=on)
        print(f"说话人分离: {'开' if cfg.get('diarization') else '关'}", file=sys.stdout)
        if on:
            import importlib.util

            if importlib.util.find_spec("pyannote") is None:
                print(
                    "⚠ 当前环境未装 pyannote（可选依赖）。",
                    file=sys.stdout,
                )
                print(
                    "  `uv tool install --from git+https://github.com/HuangYincan/VideoNote-MCP videonote "
                    "--with pyannote.audio --with torch`，或用 `uvx --from ... --with pyannote.audio --with torch` 运行。",
                    file=sys.stdout,
                )
                print(
                    "  另需 HF_TOKEN 并在 huggingface.co 同意 pyannote 模型授权（diarize_media 时用）。",
                    file=sys.stdout,
                )


def _login_cli(argv, exit_on_fail: bool = True) -> None:
    """`videonote login [bilibili]`：扫码登录 B 站，自动获取并保存 SESSDATA（AI 字幕用）。

    exit_on_fail=False 时失败路径只返回不杀进程（setup 向导内调用，#120：
    登录失败不再让整个向导带 traceback/退出，已配的其它设置不丢失）。
    """
    if argv and argv[0] != "bilibili":
        print(f"未知平台: {argv[0]}（当前支持 bilibili）", file=sys.stderr)
        sys.exit(2)
    import time
    import urllib.parse
    import requests

    try:
        import qrcode
    except ImportError:
        print("需要 qrcode 库：`uv sync` 后重试", file=sys.stderr)
        if exit_on_fail:
            sys.exit(1)
        return

    _UA = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bilibili.com",
    }
    try:
        resp = requests.get(
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
            headers=_UA, timeout=10,
        ).json()
        if resp.get("code") != 0:
            print(f"生成二维码失败: {resp}", file=sys.stderr)
            if exit_on_fail:
                sys.exit(1)
            return
        qr_url = resp["data"]["url"]
        qrcode_key = resp["data"]["qrcode_key"]
    except Exception as e:
        print(f"生成二维码失败（网络？）: {e}", file=sys.stderr)
        if exit_on_fail:
            sys.exit(1)
        return

    _show_header("B 站扫码登录")
    print(f"{_YELLOW}请用 B 站 App「扫一扫」扫描下方二维码（约 1 分钟内有效）{_RESET}", file=sys.stdout)
    qr = qrcode.QRCode(border=1)
    qr.add_data(qr_url)
    qr.make(fit=True)
    try:
        qr.print_ascii(out=sys.stdout, invert=True)
    except TypeError:
        qr.print_ascii(invert=True)
    print("", file=sys.stdout)

    last_status = None
    try:
        while True:
            time.sleep(2)
            try:
                poll = requests.get(
                    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                    params={"qrcode_key": qrcode_key},
                    headers=_UA, timeout=10,
                ).json()
            except Exception as e:
                print(f"轮询失败（网络？）: {e}", file=sys.stderr)
                continue
            data = poll.get("data") or {}
            st = data.get("code", 0)
            if st == 0 and data.get("url"):
                # 登录成功。url 可能直接带 SESSDATA，也可能是 crossDomain ticket（需跟随拿 Set-Cookie）
                sess = ""
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(data["url"]).query)
                sess = qs.get("SESSDATA", [""])[0]
                if not sess:
                    try:
                        s = requests.Session()
                        s.headers.update(_UA)
                        s.get(data["url"], timeout=10)
                        # SESSDATA 可能有多条（不同 domain/path），requests 的 .get() 会抛
                        # CookieConflictError；手动遍历取第一条
                        sess = next((c.value for c in s.cookies if c.name == "SESSDATA"), "")
                    except Exception as e:
                        print(f"跟随登录 URL 拿 cookie 失败: {e}", file=sys.stderr)
                if not sess:
                    # 不打印完整 URL：crossDomain ticket 含敏感参数（#71）
                    from urllib.parse import urlparse

                    print(f"登录成功但未取到 SESSDATA（{urlparse(data['url']).netloc or '未知域名'}）", file=sys.stderr)
                    if exit_on_fail:
                        sys.exit(1)
                    return
                from app.services.cookie_manager import CookieConfigManager

                CookieConfigManager().set("bilibili", f"SESSDATA={sess}")
                print(f"{_GREEN}✓ 已保存 B 站 SESSDATA —— AI 字幕可直接用了{_RESET}", file=sys.stdout)
                print("（下次生成 B 站笔记会优先用 AI 字幕、跳过语音识别）", file=sys.stdout)
                try:
                    input("（按回车返回）", )
                except (EOFError, KeyboardInterrupt):
                    pass
                return
            # B 站状态码：86101=未扫码（安静等待）；86090=已扫码待确认；86038=过期
            if st == 86090 and last_status != 86090:
                print("已扫码，请在手机上确认登录…", file=sys.stdout)
                last_status = 86090
            elif st == 86038:
                print(f"{_YELLOW}二维码已过期，请重新运行 `videonote login bilibili`{_RESET}", file=sys.stdout)
                try:
                    input("（按回车返回）", )
                except (EOFError, KeyboardInterrupt):
                    pass
                return
    except KeyboardInterrupt:
        print("（已取消）", file=sys.stdout)


def _format_list_display(cfg) -> str:
    """向导菜单里的导出格式显示：非列表垃圾配置（手动编辑 JSON 常见）显示「无」
    而不是被 `','.join` 拆成字符（#124 A4）。"""
    if isinstance(cfg, list) and cfg:
        return ",".join(str(f) for f in cfg)
    return "无"


def _export_cli(argv) -> None:
    """`videonote export ...`：把已完成任务的转写导出为纯格式（srt/vtt/json）。"""
    parser = argparse.ArgumentParser(
        prog="videonote export",
        description="把已完成任务的转写导出为字幕/JSON（确定性渲染，不耗 LLM）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出支持的导出格式")
    p_run = sub.add_parser("export", help="导出指定任务（<task_id> 必填）")
    p_run.add_argument("task_id", help="已完成任务的 task_id（generate_note 返回）")
    p_run.add_argument("--format", default=None, help="逗号分隔的格式（srt,vtt,json），缺省取 setup 默认")
    p_run.add_argument("--out-dir", default=None, help="输出目录（缺省 note_results/{task_id}/）")

    opts = parser.parse_args(argv)
    if opts.cmd == "list":
        print("支持的导出格式（确定性渲染，不耗 LLM）：")
        for f, desc in (("srt", "字幕，标准 SubRip"), ("vtt", "字幕，WebVTT"), ("json", "结构化转写")):
            print(f"  - {f}: {desc}")
        return

    # 缺省格式：命令行 > setup 配置 > env > 默认 ["srt"]（与 MCP export_transcript 同源，
    # #123 A6 统一 ["srt"]；非列表垃圾配置由 resolve_default_export_formats 守卫回退，#124 A4）
    formats = None
    if opts.format:
        formats = [f.strip() for f in opts.format.split(",") if f.strip()]
    if formats is None:
        formats = resolve_default_export_formats() or ["srt"]

    from videonote_mcp.export import FORMATS, export_transcript

    # 未知格式直接报错（与 MCP export 工具同口径；此前 exporter 只 warning 丢弃后
    # 仍打印「✓ 已导出 N 个格式」——用户以为导出齐了实际缺文件，#120）
    unknown = sorted(set(formats) - set(FORMATS))
    if unknown:
        print(
            f"✗ 未知导出格式: {', '.join(unknown)}（支持: {', '.join(FORMATS)}）",
            file=sys.stderr,
        )
        sys.exit(1)

    note_output_dir = Path(os.environ.get("NOTE_OUTPUT_DIR", "note_results"))
    # task_id 进路径拼接前校验格式（与 MCP _validate_task_id 同源正则，防 ../ 逃逸）
    if not re.fullmatch(r"^[A-Za-z0-9_-]{1,64}$", opts.task_id):
        print(
            f"✗ 非法 task_id: {opts.task_id!r}（应为 1-64 位字母/数字/下划线/连字符）",
            file=sys.stderr,
        )
        sys.exit(1)
    task_dir = note_output_dir / opts.task_id
    import json as _json

    # 与 server 侧 _load_task_transcript 同源（docs/05 #16）：gen/transcript.json 是
    # 规范来源，缺失/损坏才退 result.json；损坏与「无转写」分开报（#120）
    transcript = None
    cache = task_dir / "gen" / "transcript.json"
    if cache.exists():
        try:
            transcript = _json.loads(cache.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠ 转写缓存损坏（{cache}）：{e}", file=sys.stderr)
    if not transcript:
        result_path = task_dir / "result.json"
        if not result_path.exists():
            print(f"✗ 找不到任务 {opts.task_id} 的结果文件（{result_path}），任务可能未成功", file=sys.stderr)
            sys.exit(1)
        try:
            transcript = _json.loads(result_path.read_text(encoding="utf-8")).get("transcript")
        except Exception as e:
            print(f"✗ 任务 {opts.task_id} 的结果文件损坏：{e}", file=sys.stderr)
            sys.exit(1)
    if not transcript:
        print(f"✗ 任务 {opts.task_id} 没有转写结果（可能未到转写阶段）", file=sys.stderr)
        sys.exit(1)

    out_dir = opts.out_dir or str(task_dir / "gen")
    written = export_transcript(transcript, formats=formats, out_dir=out_dir, task_id=opts.task_id)
    # 部分失败时 exporter 把 _errors 塞进 written（#125 C2）：先剥离再判空——
    # 否则全部失败时 {"_errors": ...} 仍 truthy，`not written` 报错分支变死代码，
    # 还打印「✓ 已导出 1 个格式: _errors」。与 server.export_transcript 同口径。
    errors = written.pop("_errors", {}) if isinstance(written, dict) else {}
    if not written:
        detail = f"；失败原因: {errors}" if errors else ""
        print(f"✗ 没有成功导出任何格式（请求: {formats}）{detail}", file=sys.stderr)
        sys.exit(1)
    print(f"✓ 已导出 {len(written)} 个格式（task_id={opts.task_id}）：")
    for fmt, uri in written.items():
        print(f"  - {fmt}: {uri}")
    if errors:
        print(f"⚠ 部分格式导出失败: {errors}", file=sys.stderr)


def main() -> None:
    """入口：providers / setup / transcriber / login / export 走轻量 CLI；**无参数**时才是 MCP server（stdio）。"""
    known = ("providers", "setup", "transcriber", "login", "export")
    if len(sys.argv) > 1 and sys.argv[1] in known:
        cmd = sys.argv[1]
        if cmd == "providers":
            _providers_cli(sys.argv[2:])
        elif cmd == "setup":
            _setup_cli()
        elif cmd == "login":
            _login_cli(sys.argv[2:])
        elif cmd == "export":
            _export_cli(sys.argv[2:])
        else:
            _transcriber_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1:
        # 未知参数（如 uvx 选项写错位置）→ 报错而不是静默启动 MCP server
        print(f"未知子命令: {sys.argv[1]}", file=sys.stderr)
        print(f"用法: videonote {' | '.join(known)} ...", file=sys.stderr)
        print("（MCP server 模式由客户端无参数启动，不要手动传参）", file=sys.stderr)
        sys.exit(2)
    # MCP 模式（无参数，stdio 客户端启动）：懒加载完整流水线（server.py）
    from videonote_mcp.server import main as _server_main

    _server_main()


if __name__ == "__main__":
    main()
