"""Cross-platform installer with explicit targets and a read-only preview."""
import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from videonote_mcp import __version__
from videonote_mcp.hardware import inspect_hardware
from videonote_mcp.installer_profiles import (
    ASR_FIELDS,
    asr_values,
    device_label,
    pick_profile,
    profile_digest,
    safe_env,
)
from videonote_mcp.mcp_clients import (
    CLIENT_NAMES,
    ConfigError,
    clients,
    inspect,
    register,
)
from videonote_mcp.terminal_ui import checkbox, field, header, message, select

SIZES = ("tiny", "base", "small", "medium", "large-v3", "large-v3-turbo")
ENGINES = ("fast-whisper", "mlx-whisper", "funasr", "groq", "bcut", "kuaishou")
RESULT_PREFIX = "VIDEONOTE_INSTALL_RESULT="


@dataclass(frozen=True)
class Runtime:
    command: str
    args: list[str]
    env: dict[str, str]

    @property
    def entry(self) -> dict:
        return {"type": "stdio", "command": self.command, "args": self.args, "env": self.env}


def find_runner(name: str) -> str:
    found = shutil.which(name)
    if found:
        return os.path.abspath(found)  # Keep stable Homebrew symlinks.
    executable = name + ".exe" if sys.platform == "win32" else name
    for directory in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin",
                      Path("/opt/homebrew/bin"), Path("/usr/local/bin")):
        candidate = directory / executable
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise ConfigError(f"未找到 {name}；请先安装 uv 并重新打开终端")


def source_root() -> Path | None:
    root = Path(__file__).resolve().parent.parent
    return root if (root / "pyproject.toml").is_file() else None


def directories(root: Path | None) -> tuple[Path, Path]:
    if os.environ.get("VIDEONOTE_DATA_DIR"):
        data = Path(os.environ["VIDEONOTE_DATA_DIR"])
    elif root:
        data = root / "data"
    elif sys.platform == "win32":
        data = Path(os.environ.get("APPDATA") or Path.home()) / "videonote-mcp"
    else:
        data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "videonote-mcp"
    data = data.expanduser().absolute()
    config = Path(os.environ.get("VIDEONOTE_CONFIG_DIR") or data / "config").expanduser().absolute()
    return data, config


def runtime_for(root: Path | None, engine: str, device: str | None, python: str,
                *, data_config=None, settings_env=None) -> Runtime:
    data, config = data_config or directories(root)
    extra = {"mlx-whisper": "mlx", "funasr": "funasr"}.get(engine)
    if root:
        command = find_runner("uv")
        args = ["--directory", str(root), "run", "--frozen", "--python", python]
        if extra:
            args += ["--extra", extra]
        args += ["videonote"]
    else:
        command = find_runner("uvx")
        package = f"videonote[{extra}]" if extra else "videonote"
        args = ["--python", python, f"{package}=={__version__}"]
    env = {"VIDEONOTE_DATA_DIR": str(data), "VIDEONOTE_CONFIG_DIR": str(config), "PYTHONUTF8": "1"}
    env.update(safe_env(settings_env or {}))
    # GUI clients do not necessarily inherit terminal paths. Carry the paths
    # used for FFmpeg and CUDA probes into their eventual MCP subprocess.
    for key in ("PATH", "CUDA_PATH", "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    if device:
        env["VIDEONOTE_TRANSCRIBER_DEVICE"] = device
    if os.environ.get("VIDEONOTE_MODEL_DIR"):
        env["VIDEONOTE_MODEL_DIR"] = str(Path(os.environ["VIDEONOTE_MODEL_DIR"]).expanduser().absolute())
    return Runtime(command, args, env)


def runtime_action(runtime: Runtime, action: str, extra: list[str] | None = None) -> dict:
    command = [runtime.command, *runtime.args, "_install-runtime", action, *(extra or [])]
    try:
        result = subprocess.run(command, stdout=subprocess.PIPE, text=True, encoding="utf-8",
                                errors="replace", env={**os.environ, **runtime.env},
                                timeout=90 if action == "probe" else 3600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(f"运行环境准备失败（{type(exc).__name__}）") from exc
    if result.returncode:
        raise ConfigError(f"运行环境 {action} 失败（退出码 {result.returncode}）；请查看上方诊断")
    for line in reversed(result.stdout.splitlines()):
        if line.startswith(RESULT_PREFIX):
            try:
                data = json.loads(line[len(RESULT_PREFIX):])
            except ValueError as exc:
                raise ConfigError("运行环境返回了无效结果") from exc
            if isinstance(data, dict):
                return data
    raise ConfigError("运行环境未返回检查结果")


async def verify_mcp(runtime: Runtime) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(command=runtime.command, args=runtime.args, env=runtime.env)
    async with asyncio.timeout(90):
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                result = await session.initialize()
                tools = await session.list_tools()
                if result.serverInfo.version != __version__ or "health_check" not in {t.name for t in tools.tools}:
                    raise ConfigError("MCP 握手返回了不匹配的服务版本或工具列表")


def confirm(message: str, default: bool = False) -> bool:
    from videonote_mcp.terminal_ui import confirm as prompt_confirm

    try:
        return prompt_confirm(message, default)
    except EOFError:
        return False


def choose_clients(available: list, *, inq=None, title="安装向导") -> list:
    header(title, "① Agent 客户端")
    message("检测结果仅供参考；请勾选要安装的平台。", "muted")
    choices = [{"name": f"{client.name} — {'已检测到' if client.detected else '未检测到'}",
                "value": client.key, "enabled": False} for client in available if client.detected]
    undetected = [client for client in available if not client.detected]
    if undetected:
        choices.append({"name": "手动选择其他平台…", "value": "__manual__", "enabled": False})
    if not choices:
        return []
    keys = checkbox("安装到哪些 Agent 平台？", choices, inq=inq)
    if "__manual__" in keys:
        keys += checkbox("手动选择平台（未检测到应用，请确认已安装）", [
            {"name": client.name, "value": client.key, "enabled": False} for client in undetected], inq=inq)
    return [client for client in available if client.key in keys]


def _show_asr_profile(existing, installation, env, hardware, config_path=None):
    values = asr_values(existing, installation, env)
    engine, engine_source = values["engine"]
    size, size_source = values["size"]
    message("当前转写配置：", "section")
    field("引擎", engine, f"（{engine_source}）")
    if engine == "groq":
        field("当前模型", env.get('GROQ_TRANSCRIBER_MODEL') or 'whisper-large-v3', "（Groq 云端）")
    elif engine == "funasr":
        field("当前模型", "paraformer-zh / fsmn-vad / ct-punc", "（FunASR）")
    if engine not in ("fast-whisper", "mlx-whisper"):
        field("Whisper 备用尺寸", size, f"（{size_source}；当前引擎不使用）")
    else:
        field("模型尺寸", size, f"（{size_source}）")
    if engine == "mlx-whisper":
        device_text = device_label(engine, values["device"])
    elif engine in ("groq", "bcut", "kuaishou"):
        device_text = device_label(engine, values["device"])
    elif values["device"]:
        device_text = device_label(engine, values["device"]) + "（已有设置，实际可用性后续检查）"
    else:
        device_text = "未明确设置；本次安装按检测使用 " + ("CUDA" if hardware.cuda_available else "CPU")
    field("设备设置", device_text)
    for key, label in (("enable_preprocess", "音频预处理"), ("diarization", "说话人分离")):
        enabled, source = values[key]
        field(label, '开' if enabled else '关', f"（{source}）", role="success" if enabled else "muted")
    speakers = existing.get("diarization_speakers")
    try:
        count = int(speakers) if speakers else None
        speaker_label = str(count) if count and count > 0 else "自动检测"
    except (TypeError, ValueError):
        speaker_label = "无效（请在 setup 中核对）"
    field("说话人数", speaker_label, "（分离关闭时不使用）" if not values["diarization"][0] else "")
    if config_path:
        path = config_path / "transcriber.json"
        field("配置文件", path, "" if path.is_file() else "（尚未保存，使用上方其它来源）")
    if env.get("VIDEONOTE_MODEL_DIR"):
        field("模型目录", env['VIDEONOTE_MODEL_DIR'])
    print("", file=sys.stdout)


def choose_asr(options, hardware, existing: dict, installation: dict, interactive: bool,
               *, profile=None) -> tuple[str, str, str | None, bool]:
    env = profile.environment if profile else safe_env(dict(os.environ))
    values = asr_values(existing, installation, env)
    current = existing.get("transcriber_type") or env.get("TRANSCRIBER_TYPE") or installation.get("engine")
    has_existing = bool(current or any(key in existing for key in ASR_FIELDS)
                        or any(key in env for key in ("WHISPER_MODEL_SIZE", "VIDEONOTE_TRANSCRIBER_DEVICE",
                                                      "VIDEONOTE_ENABLE_PREPROCESS", "VIDEONOTE_DIARIZATION")))
    choice = options.asr
    if choice is None:
        choice = "keep" if has_existing else "auto"
        if interactive:
            header("安装向导", "② 语音转写引擎")
            _show_asr_profile(existing, installation, env, hardware, profile.config if profile else None)
            if options.model:
                message(f"本次 --model 指定 {options.model}，将覆盖上方的当前模型尺寸。", "warning")
            field("硬件检测", hardware.detail)
            labels = {"cpu": "CPU / fast-whisper", "cuda": "NVIDIA CUDA / fast-whisper",
                      "mlx": "Apple GPU / mlx-whisper"}
            field("推荐方案", labels[hardware.recommended])
            choices = [{"name": f"自动推荐：{labels[hardware.recommended]}", "value": "auto"},
                       {"name": labels["cpu"], "value": "cpu"}]
            if hardware.nvidia_present:
                choices.append({"name": labels["cuda"], "value": "cuda"})
            if hardware.mlx_supported:
                choices.append({"name": labels["mlx"], "value": "mlx"})
            keep_name = "保留现有转写配置" if has_existing else "使用默认转写配置（尚无保存的设置）"
            choices.extend([{"name": f"{keep_name} — {values['engine'][0]} / {values['size'][0]}", "value": "keep"},
                            {"name": "稍后设置转写", "value": "skip"}])
            choice = select("选择转写方案", choices, choice)
    elif choice == "keep":
        _show_asr_profile(existing, installation, env, hardware, profile.config if profile else None)
    if choice not in ("auto", "cpu", "cuda", "mlx", "keep", "skip"):
        raise ConfigError("转写方案必须是 auto / cpu / cuda / mlx / keep / skip")
    if choice == "auto":
        choice = hardware.recommended
    old_device = installation.get("env", {}).get("VIDEONOTE_TRANSCRIBER_DEVICE")
    if choice in ("keep", "skip"):
        engine = current or "fast-whisper"
        device = (env.get("VIDEONOTE_TRANSCRIBER_DEVICE") or old_device
                  or ("cuda" if hardware.cuda_available else "cpu"))
    else:
        engine = "mlx-whisper" if choice == "mlx" else "fast-whisper"
        device = "cpu" if choice == "mlx" else choice
    if engine not in ENGINES:
        raise ConfigError("现有转写引擎无法识别；请先在 setup 中核对设置")
    if engine == "mlx-whisper" and not hardware.mlx_supported:
        raise ConfigError("mlx-whisper 需要 macOS 14+、Apple Silicon 和原生 ARM Python")
    size = options.model or values["size"][0]
    if not isinstance(size, str) or not size:
        raise ConfigError("现有模型尺寸无效；请用 --model 指定")
    if interactive and engine in ("fast-whisper", "mlx-whisper") and not options.model and choice not in ("keep", "skip"):
        choices = [{"name": value + ("（默认）" if value == "small" else ""), "value": value} for value in SIZES]
        if size not in SIZES:
            choices.insert(0, {"name": f"保留自定义模型：{size}", "value": size})
        size = select("模型尺寸（越大通常越耗内存和下载空间）", choices, size)
    if device not in (None, "cpu", "cuda"):
        raise ConfigError("现有计算设备无效；请通过 --asr 重新选择")
    return engine, size, device, choice == "skip"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="videonote install", description="检测 MCP 客户端并推荐本地转写方案")
    targets = result.add_mutually_exclusive_group()
    targets.add_argument("--client", choices=tuple(CLIENT_NAMES), action="append", help="指定客户端，可重复")
    targets.add_argument("--all", action="store_true", help="安装到所有检测到的候选客户端")
    result.add_argument("--yes", action="store_true", help="非交互安装；必须显式指定 --client 或 --all")
    result.add_argument("--dry-run", action="store_true", help="只预览，不写配置、不下载依赖或模型")
    result.add_argument("--replace", action="store_true", help="更新已识别的 VideoNote 注册；保留其他设置")
    result.add_argument("--asr", choices=("auto", "cpu", "cuda", "mlx", "keep", "skip"), help="硬件推荐或手动选择，已有配置默认保留")
    result.add_argument("--model", choices=SIZES, help="模型尺寸，默认保留现有值或 small")
    downloads = result.add_mutually_exclusive_group()
    downloads.add_argument("--download", dest="download", action="store_true", help="下载选中的本地模型")
    downloads.add_argument("--no-download", dest="download", action="store_false", help="稍后下载模型")
    result.set_defaults(download=None)
    result.add_argument("--no-verify", action="store_true", help="跳过 ASR 推理与 MCP 握手验证")
    result.add_argument("--python", choices=("3.11", "3.12", "3.13"), default="3.12", help="运行环境 Python 版本，默认 3.12")
    return result


def run(options) -> int:
    interactive = sys.stdin.isatty() and not options.yes and not options.dry_run
    if not interactive and not options.dry_run and not options.yes:
        raise ConfigError("非交互安装需要 --yes，并显式指定 --client 或 --all")
    if options.yes and not options.client and not options.all:
        raise ConfigError("--yes 必须配合 --client 或 --all；不会默认修改全部客户端")
    available = clients(sys.platform, Path.home(), dict(os.environ))
    if options.client:
        selected = [client for client in available if client.key in options.client]
    elif options.all or options.dry_run:
        selected = [client for client in available if client.detected]
    else:
        selected = choose_clients(available)
    if not selected:
        print("没有选中客户端。支持：" + ", ".join(CLIENT_NAMES))
        return 0 if options.dry_run or interactive else 1
    root = source_root()
    data_dir, config_dir = directories(root)
    profile = pick_profile(root, data_dir, config_dir, selected, dict(os.environ), Path.home(), interactive)
    data_dir, config_dir = profile.data, profile.config
    existing, installation = profile.settings, profile.installation
    hardware = inspect_hardware()
    engine, size, device, skip_asr = choose_asr(options, hardware, existing, installation, interactive, profile=profile)
    runtime = runtime_for(root, engine, device, options.python,
                          data_config=(data_dir, config_dir), settings_env=profile.environment)
    field("硬件", hardware.detail, indent="")
    field("转写", f"{engine} / {size} / {device_label(engine, device)}", indent="")
    field("MCP 启动命令", json.dumps([runtime.command, *runtime.args], ensure_ascii=False), indent="")
    field("数据目录", data_dir, indent="")
    pending, failed = [], False
    for client in selected:
        try:
            state = inspect(client, runtime.entry)
            status = {"new": "新增注册", "same": "配置相同，跳过", "update": "已有 VideoNote，需更新启动环境",
                      "conflict": "同名其他服务，跳过", "plugin": "已有 VideoNote 插件，跳过"}[state.status]
            field(client.name, status, f" — {client.path}", indent="",
                  role="error" if state.status in ("plugin", "conflict") else "info")
            if state.status in ("plugin", "conflict"):
                failed = True
                continue
            if client.key == "claude-code" and not client.cli:
                raise ConfigError("未找到 claude CLI，请先安装 Claude Code")
            if state.status == "update" and not options.replace:
                if options.dry_run:
                    message("  更新需要 --replace，或在交互安装中选择更新。", "warning")
                elif not interactive or not confirm(f"更新 {client.name} 的 VideoNote 启动环境？"):
                    failed = True
                    continue
            pending.append((client, state))
        except ConfigError as exc:
            message(str(exc), "error", file=sys.stderr)
            failed = True
    if options.dry_run:
        message("预览完成：未写配置、未下载依赖或模型。", "success")
        return 0
    if not pending:
        return 1 if failed else 0
    message("准备同一套运行环境（首次下载依赖可能需要几分钟）…", flush=True)
    environment = runtime_action(runtime, "probe")
    if environment.get("version") != __version__:
        raise ConfigError("运行环境版本与安装器不一致；请重新安装同一版本")
    if engine == "fast-whisper" and device == "cuda" and not environment.get("cuda"):
        raise ConfigError("最终环境无法使用 CUDA；请补齐匹配的运行库，或重新运行 --asr cpu")
    if engine == "mlx-whisper" and not environment.get("mlx"):
        raise ConfigError("最终环境不满足 MLX 要求；请重新运行 --asr cpu")
    model_state = {"model_ready": None, "asr_verified": False}
    if not skip_asr:
        download = options.download
        if download is None:
            download = interactive and engine in ("fast-whisper", "mlx-whisper") and confirm(
                f"准备 {engine} / {size} 的模型（未缓存时将从 Hugging Face 下载）？", True)
        arguments = ["--engine", engine, "--size", size, "--device", device or "cpu",
                     "--expected-profile-hash", profile_digest(existing)]
        if download:
            arguments += ["--download"]
        if options.no_verify:
            arguments += ["--no-verify"]
        message("准备转写配置和模型…", flush=True)
        model_state = runtime_action(runtime, "prepare", arguments)
    if not options.no_verify:
        message("验证 MCP 启动与握手…", flush=True)
        try:
            asyncio.run(verify_mcp(runtime))
        except Exception as exc:
            raise ConfigError(f"MCP 启动验证失败（{type(exc).__name__}）；尚未注册客户端。日志：{data_dir / 'logs'}") from exc
    for client, state in pending:
        try:
            saved = register(client, state, runtime.entry)
            message(f"✓ {client.name}：" + ("已核对现有注册" if state.status == "same" else "已注册并回读核对"), "success")
            if saved:
                field("原配置备份", saved, role="muted")
            from videonote_mcp.mcp_clients import read_config

            _, final_document = read_config(client)
            final_entry = client.servers(final_document)[state.name]
            if not client.is_enabled(final_entry, final_document):
                message("  保留了禁用状态；请在客户端启用此服务后使用。", "warning")
        except (ConfigError, OSError) as exc:
            message(f"{client.name}：{exc}", "error", file=sys.stderr)
            failed = True
    message("请重启客户端或开启新会话，再调用 health_check。")
    if model_state["model_ready"] is False:
        message("本地模型尚未下载；重新运行 install --download，或使用同一启动命令运行 transcriber download。", "warning")
    elif skip_asr:
        message("本次跳过了转写配置；可稍后进入 setup。", "muted")
    elif model_state["asr_verified"]:
        message("所选设备的 ASR 短音频推理已通过。", "success")
    if not shutil.which("ffmpeg"):
        message("未找到 FFmpeg：视频下载/处理仍需安装它。", "warning")
    if options.no_verify:
        message("本次跳过了 ASR 与 MCP 运行验证。", "muted")
    if not failed:
        from app.utils.json_store import write_json_atomic

        profile = {**installation, "version": __version__, "command": runtime.command, "args": runtime.args,
                   "env": runtime.env, "engine": engine, "size": size}
        if installation != profile:
            write_json_atomic(config_dir / "installation.json", profile)
        if interactive and confirm("打开设置向导，配置平台登录或其它选项？"):
            subprocess.run([runtime.command, *runtime.args, "setup"], env={**os.environ, **runtime.env}, check=False)
    return 1 if failed else 0


def main(argv=None) -> int:
    options = parser().parse_args(argv)
    try:
        return run(options)
    except (ConfigError, OSError) as exc:
        message(str(exc), "error", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        message("安装已取消。", "muted", file=sys.stderr)
        return 130
