"""MCP client adapters: detect candidates, inspect, and register one server.

Only installer-owned command/env fields are changed. Existing server options,
unrelated servers, and the rest of each client's configuration are preserved.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CLIENT_NAMES = {"claude-code": "Claude Code", "codex": "Codex",
                "claude-desktop": "Claude Desktop", "cursor": "Cursor"}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Client:
    key: str
    path: Path
    cli: str | None = None
    detected: bool = False

    @property
    def name(self) -> str:
        return CLIENT_NAMES[self.key]

    @property
    def servers_key(self) -> str:
        return "mcp_servers" if self.key == "codex" else "mcpServers"


@dataclass
class State:
    raw: bytes | None
    document: dict
    name: str
    status: str  # new, same, update, conflict, plugin


def clients(platform: str, home: Path, env: dict, which=shutil.which) -> list[Client]:
    claude_home = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    # Claude's user MCP map is in .claude.json, adjacent to the default .claude
    # directory, or inside an explicit CLAUDE_CONFIG_DIR.
    claude_config = claude_home / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    codex_home = Path(env.get("CODEX_HOME") or home / ".codex")
    if platform == "darwin":
        desktop = home / "Library" / "Application Support" / "Claude"
    elif platform == "win32":
        desktop = Path(env.get("APPDATA") or home / "AppData" / "Roaming") / "Claude"
        local = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local")
        msix = sorted((local / "Packages").glob("Claude_*/LocalCache/Roaming/Claude"))
        if msix:
            desktop = msix[0]
    else:
        desktop = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "Claude"
    claude_cli, codex_cli = which("claude"), which("codex")
    return [
        Client("claude-code", claude_config, claude_cli, bool(claude_cli)),
        Client("codex", codex_home / "config.toml", codex_cli, bool(codex_cli) or codex_home.is_dir()),
        Client("claude-desktop", desktop / "claude_desktop_config.json", detected=desktop.is_dir()),
        Client("cursor", home / ".cursor" / "mcp.json", detected=(home / ".cursor").is_dir()),
    ]


def read_config(client: Client) -> tuple[bytes | None, dict]:
    try:
        raw = client.path.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError as exc:
        raise ConfigError(f"无法读取 {client.path}（{type(exc).__name__}）") from exc
    try:
        text = raw.decode("utf-8-sig")
        document = (tomllib.loads(text) if client.key == "codex" else json.loads(text)) if text.strip() else {}
    except (ValueError, UnicodeError) as exc:
        raise ConfigError(f"配置无法解析，保留原文件：{client.path}") from exc
    if not isinstance(document, dict) or not isinstance(document.get(client.servers_key, {}), dict):
        raise ConfigError(f"MCP 配置必须为对象，保留原文件：{client.path}")
    return raw, document


def is_videonote(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    parts = [entry.get("command", ""), *entry.get("args", [])] if isinstance(entry.get("args", []), list) else []
    return any(re.fullmatch(r"videonote(?:\.exe|(?:\[[^]]+\])?(?:==|@).+)?", str(p).replace("\\", "/").split("/")[-1])
               or p in ("videonote_mcp", "videonote_mcp.cli", "videonote_mcp.server") for p in parts)


def matches(entry: dict, desired: dict) -> bool:
    return (entry.get("command") == desired["command"] and entry.get("args", []) == desired["args"]
            and isinstance(entry.get("env", {}), dict)
            and all(entry.get("env", {}).get(k) == v for k, v in desired["env"].items())
            and entry.get("type", "stdio") == "stdio")


def plugin_enabled(client: Client, document: dict) -> bool:
    if client.key == "codex":
        plugins = document.get("plugins", {})
        return isinstance(plugins, dict) and any(
            str(key).split("@")[0] == "videonote" and isinstance(value, dict) and value.get("enabled", True)
            for key, value in plugins.items())
    if client.key == "claude-code":
        home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
        try:
            settings = json.loads((home / "settings.json").read_text(encoding="utf-8"))
            plugins = settings.get("enabledPlugins", {})
            return isinstance(plugins, dict) and any(
                str(key).split("@")[0] == "videonote" and value is True for key, value in plugins.items())
        except FileNotFoundError:
            return False
        except (OSError, ValueError, AttributeError) as exc:
            raise ConfigError("无法检查 Claude 插件设置；请先核对 settings.json") from exc
    return False


def inspect(client: Client, desired: dict) -> State:
    raw, document = read_config(client)
    if plugin_enabled(client, document):
        return State(raw, document, "videonote", "plugin")
    servers = document.get(client.servers_key, {})
    aliases = [name for name, entry in servers.items() if is_videonote(entry)]
    if len(aliases) > 1:
        raise ConfigError(f"{client.name} 已有多份 VideoNote 注册；请先手动核对")
    name = aliases[0] if aliases else "videonote"
    if "videonote" in servers and "videonote" not in aliases:
        return State(raw, document, "videonote", "conflict")
    entry = servers.get(name)
    if entry is None:
        status = "new"
    elif not is_videonote(entry):
        status = "conflict"
    else:
        status = "same" if matches(entry, desired) else "update"
    return State(raw, document, name, status)


def backup(path: Path, raw: bytes | None) -> Path | None:
    if raw is None:
        return None
    target = path.with_name(path.name + "." + uuid.uuid4().hex[:12] + ".bak")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
    return target


def check_unchanged(path: Path, raw: bytes | None) -> None:
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current != raw:
        raise ConfigError("客户端配置在安装期间发生变化；请重新预览后安装")


def write_config(client: Client, state: State, desired: dict) -> Path | None:
    check_unchanged(client.path, state.raw)
    if client.key == "codex":
        import tomlkit

        document = tomlkit.parse((state.raw or b"").decode("utf-8-sig"))
        if client.servers_key not in document:
            document[client.servers_key] = tomlkit.table()
        servers = document[client.servers_key]
        if state.name not in servers:
            servers[state.name] = tomlkit.table()
    else:
        document = state.document
        servers = document.setdefault(client.servers_key, {})
        if state.name not in servers or not is_videonote(servers[state.name]):
            servers[state.name] = {}
    entry = servers[state.name]
    entry["command"], entry["args"] = desired["command"], desired["args"]
    if client.key != "codex":
        entry["type"] = "stdio"
    if not isinstance(entry.get("env", {}), Mapping):
        raise ConfigError("已有 env 不是对象；保留原配置")
    if "env" not in entry:
        entry["env"] = {}
    entry["env"].update(desired["env"])
    return save_document(client, state.raw, document)


def save_document(client: Client, raw: bytes | None, document) -> Path | None:
    """Validate, back up and atomically save a preserving client edit."""
    path = client.path.resolve()  # Respect users' symlinked dotfiles.
    check_unchanged(path, raw)
    if client.key == "codex":
        import tomlkit

    content = (tomlkit.dumps(document) if client.key == "codex" else json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    # Parse the final text before touching the original file.
    (tomllib.loads if client.key == "codex" else json.loads)(content)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    saved = backup(path, raw)
    fd, staged_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        check_unchanged(path, raw)
        os.replace(staged, path)
    finally:
        staged.unlink(missing_ok=True)
    return saved


def register(client: Client, state: State, desired: dict) -> Path | None:
    if state.status in ("plugin", "conflict"):
        raise ConfigError("已有插件或同名其它服务；请手动核对，不自动覆盖")
    if state.status == "same":
        check_unchanged(client.path, state.raw)
        return None
    # Let official CLIs own new registrations. Updates use a preserving merge,
    # retaining user timeouts, disabled flags, and custom environment variables.
    if client.cli and state.status == "new":
        check_unchanged(client.path, state.raw)
        saved = backup(client.path, state.raw)
        command = [client.cli, "mcp", "add"]
        if client.key == "claude-code":
            command += ["--scope", "user"]
        command.append(state.name)
        for key, value in desired["env"].items():
            command += ["--env", f"{key}={value}"]
        command += ["--", desired["command"], *desired["args"]]
        try:
            result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigError(f"{client.name} 注册命令失败（{type(exc).__name__}）；备份：{saved}") from exc
        if result.returncode != 0:
            # A client's errors can include its existing secrets. Do not echo.
            raise ConfigError(f"{client.name} 注册失败（退出码 {result.returncode}）；备份：{saved}")
    else:
        if client.key == "claude-code" and not client.cli:
            raise ConfigError("未找到 claude CLI；请先安装 Claude Code 或选择其他客户端")
        saved = write_config(client, state, desired)
    _, document = read_config(client)
    entry = document.get(client.servers_key, {}).get(state.name, {})
    if not isinstance(entry, dict) or not matches(entry, desired):
        raise ConfigError(f"{client.name} 注册后的配置未通过核对；备份：{saved}")
    return saved
