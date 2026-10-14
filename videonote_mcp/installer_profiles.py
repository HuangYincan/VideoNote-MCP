"""Read-only discovery of current and legacy transcription profiles."""
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from videonote_mcp.json_config import strict_loads
from videonote_mcp.mcp_clients import ConfigError, read_config
from videonote_mcp.terminal_ui import select

ASR_ENV = ("TRANSCRIBER_TYPE", "WHISPER_MODEL_SIZE", "VIDEONOTE_TRANSCRIBER_DEVICE",
           "VIDEONOTE_ENABLE_PREPROCESS", "VIDEONOTE_DIARIZATION", "GROQ_TRANSCRIBER_MODEL",
           "VIDEONOTE_MODEL_DIR")
ASR_FIELDS = ("transcriber_type", "whisper_model_size", "enable_preprocess", "diarization", "diarization_speakers")


def profile_digest(settings: dict) -> str:
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def asr_values(existing, installation, env):
    def value(key, variable, default, record=None):
        stored = existing.get(key)
        if stored is not None and (key not in ("transcriber_type", "whisper_model_size") or stored):
            return stored, "配置文件"
        if env.get(variable):
            return env[variable], "已有启动环境"
        if record and installation.get(record):
            return installation[record], "上次安装记录"
        return default, "程序默认"

    result = {
        "engine": value("transcriber_type", "TRANSCRIBER_TYPE", "fast-whisper", "engine"),
        "size": value("whisper_model_size", "WHISPER_MODEL_SIZE", "small", "size"),
        "device": env.get("VIDEONOTE_TRANSCRIBER_DEVICE") or installation.get("env", {}).get("VIDEONOTE_TRANSCRIBER_DEVICE"),
    }
    for key, variable in (("enable_preprocess", "VIDEONOTE_ENABLE_PREPROCESS"), ("diarization", "VIDEONOTE_DIARIZATION")):
        raw, source = value(key, variable, False)
        enabled = raw.strip().lower() in ("1", "true", "yes", "on") if isinstance(raw, str) else bool(raw)
        result[key] = enabled, source
    return result


def safe_env(env: dict) -> dict:
    return {key: env[key] for key in ASR_ENV if isinstance(env.get(key), str)
            and env[key].strip() and not env[key].startswith("${")}


def _read_object(path: Path, encoding="utf-8") -> dict:
    try:
        profile = strict_loads(path.read_text(encoding=encoding))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigError(f"无法读取现有设置，保留原文件：{path}") from exc
    if not isinstance(profile, dict):
        raise ConfigError(f"现有设置必须为对象：{path}")
    return profile


def read_settings(config: Path) -> dict:
    # Match the runtime's UTF-8 policy. A BOM must fail here without allowing
    # its fallback reader to rename the original file to *.corrupt.
    settings = _read_object(config / "transcriber.json")
    for name in ("transcriber_type", "whisper_model_size"):
        value = settings.get(name)
        if value is not None and not isinstance(value, str):
            raise ConfigError(f"现有 {name} 必须为字符串；保留原文件：{config / 'transcriber.json'}")
    return settings


def read_profile(config: Path) -> tuple[dict, dict]:
    settings = read_settings(config)
    installation = _read_object(config / "installation.json", encoding="utf-8-sig")
    if not isinstance(installation.get("env", {}), dict):
        raise ConfigError("现有 installation.json 的 env 必须为对象")
    for name in ("engine", "size"):
        if installation.get(name) is not None and not isinstance(installation[name], str):
            raise ConfigError(f"旧安装记录的 {name} 必须为字符串；保留 installation.json")
    return settings, installation


@dataclass
class Profile:
    data: Path
    config: Path
    settings: dict
    installation: dict
    environment: dict
    source: str

    @property
    def detected(self):
        return (any(key in self.settings for key in ASR_FIELDS)
                or any(key in self.environment for key in ASR_ENV if key != "VIDEONOTE_MODEL_DIR")
                or bool(self.installation.get("engine")))


def _path(value, home, relative_to=None):
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("旧注册中的目录必须为非空字符串；请先核对原配置")
    if value.startswith("${"):
        raise ConfigError("旧注册中的目录仍为占位符；请显式设置对应的 VIDEONOTE_*_DIR 后重试")
    path = Path(value)
    if value == "~" or value.startswith("~/") or value.startswith("~\\"):
        path = home / value[2:] if value != "~" else home
    if not path.is_absolute():
        if relative_to is None:
            raise ConfigError("旧注册中的数据 / 配置 / 模型目录为相对路径，无法确认原工作目录；"
                              "请显式设置对应的 VIDEONOTE_*_DIR 后重试")
        path = relative_to / path
    return path.absolute()


def _registration_root(entry, home):
    args = entry.get("args", [])
    if isinstance(args, list) and "--directory" in args:
        index = args.index("--directory")
        if index + 1 < len(args) and isinstance(args[index + 1], str):
            return _path(args[index + 1], home)
    command = entry.get("command")
    if isinstance(command, str):
        path = Path(command)
        if path.is_absolute() and len(path.parents) >= 3 and path.parent.parent.name == ".venv":
            return path.parents[2]
    return None


def _source_root(root):
    if root and ((root / "videonote_mcp").is_dir()
                 or (root / "data/config/transcriber.json").is_file()
                 or (root / "config/transcriber.json").is_file()):
        return root
    return None


def discover_profiles(root, data, config, selected, env, home) -> list[Profile]:
    settings, installation = read_profile(config)
    remembered_data = installation.get("env", {}).get("VIDEONOTE_DATA_DIR")
    if remembered_data and not env.get("VIDEONOTE_DATA_DIR"):
        data = _path(remembered_data, home)
    terminal = safe_env(env)
    if terminal.get("VIDEONOTE_MODEL_DIR"):
        terminal["VIDEONOTE_MODEL_DIR"] = str(_path(terminal["VIDEONOTE_MODEL_DIR"], home, Path.cwd()))
    current = Profile(data, config, settings, installation,
                      {**safe_env(installation.get("env", {})), **terminal}, "当前目录 / 环境变量")
    remembered_root = _source_root(_registration_root(installation, home)) if installation.get("command") else root
    cache = remembered_root / "models" if remembered_root else data / "models"
    if "VIDEONOTE_MODEL_DIR" in current.environment:
        current.environment["VIDEONOTE_MODEL_DIR"] = str(_path(current.environment["VIDEONOTE_MODEL_DIR"], home))
    else:
        current.environment["VIDEONOTE_MODEL_DIR"] = str(cache.absolute())
    candidates = [current] if current.detected else []
    explicit_paths = bool(env.get("VIDEONOTE_DATA_DIR") or env.get("VIDEONOTE_CONFIG_DIR"))
    packaged_data = (Path(env.get("APPDATA") or home) / "videonote-mcp" if os.name == "nt" else
                     Path(env.get("XDG_DATA_HOME") or home / ".local/share") / "videonote-mcp")
    for client in selected:
        try:
            _, document = read_config(client)
        except ConfigError:
            # The installation's per-client inspection reports this error.
            continue
        entries = [entry for entry in client.servers(document).values() if client.is_videonote(entry)]
        if len(entries) > 1:
            raise ConfigError(f"{client.name} 已有多份 VideoNote 注册；请先手动核对")
        for native in entries:
            entry = client.normalized_entry(native)
            old_env = entry.get("env", {})
            if not isinstance(old_env, dict):
                raise ConfigError(f"{client.name} 的旧 VideoNote env 必须为对象")
            old_root = _registration_root(entry, home)
            source_root = _source_root(old_root)
            relative_root = old_root if "--directory" in entry.get("args", []) else None
            if explicit_paths:
                old_data, old_config = data, config
            else:
                old_data = (_path(old_env["VIDEONOTE_DATA_DIR"], home, relative_root)
                            if old_env.get("VIDEONOTE_DATA_DIR") else source_root / "data" if source_root else packaged_data)
                old_config = (_path(old_env["VIDEONOTE_CONFIG_DIR"], home, relative_root)
                              if old_env.get("VIDEONOTE_CONFIG_DIR") else old_data / "config")
                if (not old_env.get("VIDEONOTE_CONFIG_DIR") and source_root
                        and not (old_config / "transcriber.json").exists()
                        and (source_root / "config/transcriber.json").is_file()):
                    old_config = source_root / "config"
            stored, prior = read_profile(old_config)
            remembered_data = prior.get("env", {}).get("VIDEONOTE_DATA_DIR")
            if remembered_data and not (env.get("VIDEONOTE_DATA_DIR") or old_env.get("VIDEONOTE_DATA_DIR")):
                old_data = _path(remembered_data, home, relative_root)
            merged = {**safe_env(prior.get("env", {})), **safe_env(old_env), **terminal}
            if merged.get("VIDEONOTE_MODEL_DIR"):
                merged["VIDEONOTE_MODEL_DIR"] = str(_path(merged["VIDEONOTE_MODEL_DIR"], home, relative_root))
            else:
                cache = source_root / "models" if source_root else old_data / "models"
                merged["VIDEONOTE_MODEL_DIR"] = str(cache.absolute())
            candidates.append(Profile(old_data, old_config, stored, prior, merged, f"{client.name} 的现有 VideoNote 注册"))
    if not explicit_paths:
        legacy = [(packaged_data, packaged_data / "config", "已安装包的用户配置")]
        if root:
            legacy.append((data, root / "config", "旧源码 config 目录"))
        for old_data, old_config, label in legacy:
            if (old_config / "transcriber.json").is_file():
                stored, prior = read_profile(old_config)
                remembered_data = prior.get("env", {}).get("VIDEONOTE_DATA_DIR")
                if remembered_data:
                    old_data = _path(remembered_data, home)
                inherited = {**safe_env(prior.get("env", {})), **terminal}
                cache = root / "models" if root and old_config == root / "config" else old_data / "models"
                if "VIDEONOTE_MODEL_DIR" in inherited:
                    inherited["VIDEONOTE_MODEL_DIR"] = str(_path(inherited["VIDEONOTE_MODEL_DIR"], home))
                else:
                    inherited["VIDEONOTE_MODEL_DIR"] = str(cache.absolute())
                candidates.append(Profile(old_data, old_config, stored, prior, inherited, label))
    unique = {}
    for item in candidates:
        # File-backed values shadow env; irrelevant env defaults must not create
        # false conflicts when clients point to the same actual profile.
        effective = dict(item.environment)
        for field, variable in (("transcriber_type", "TRANSCRIBER_TYPE"), ("whisper_model_size", "WHISPER_MODEL_SIZE"),
                                ("enable_preprocess", "VIDEONOTE_ENABLE_PREPROCESS"), ("diarization", "VIDEONOTE_DIARIZATION")):
            if item.settings.get(field) is not None:
                effective.pop(variable, None)
        fingerprint = (item.data.absolute(), item.config.absolute(), tuple(sorted(effective.items())))
        if fingerprint not in unique:
            unique[fingerprint] = item
    return list(unique.values()) or [current]


def pick_profile(root, data, config, selected, env, home, interactive) -> Profile:
    candidates = discover_profiles(root, data, config, selected, env, home)
    if len(candidates) == 1:
        result = candidates[0]
        if result.detected:
            print(f"沿用配置：{result.source} — {result.config}", file=sys.stdout)
        return result
    print("检测到多套现有配置，请选择本次安装使用的配置目录 / 环境。", file=sys.stdout)
    choices = []
    for index, item in enumerate(candidates):
        values = asr_values(item.settings, item.installation, item.environment)
        summary = f"{values['engine'][0]} / {values['size'][0]} · 设备标记 {values['device'] or '未指定'}"
        summary += f" · 预处理 {'开' if values['enable_preprocess'][0] else '关'} · 分离 {'开' if values['diarization'][0] else '关'}"
        label = f"{item.source} — {item.config} — {summary}"
        if item.environment.get("VIDEONOTE_MODEL_DIR"):
            label += " · 模型目录 " + item.environment["VIDEONOTE_MODEL_DIR"]
        choices.append({"name": label, "value": index})
        print(f"  {label}", file=sys.stdout)
    if not interactive:
        raise ConfigError("多套历史转写设置不一致；请先用 VIDEONOTE_CONFIG_DIR / VIDEONOTE_DATA_DIR 指定目录，"
                          "并用 TRANSCRIBER_TYPE / WHISPER_MODEL_SIZE 指定冲突的环境默认值")
    index = select("沿用哪套现有配置？", choices, 0)
    return candidates[index]
