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
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from videonote_mcp import json_config


@dataclass(frozen=True)
class ClientSpec:
    name: str
    servers_key: str = "mcpServers"
    toml: bool = False
    jsonc: bool = False
    command_array: bool = False
    env_key: str = "env"
    local_type: str | None = "stdio"
    enabled_key: str | None = None


CLIENT_SPECS = {
    "claude-code": ClientSpec("Claude Code"),
    "codex": ClientSpec("Codex", "mcp_servers", toml=True, local_type=None, enabled_key="enabled"),
    "claude-desktop": ClientSpec("Claude Desktop"),
    "cursor": ClientSpec("Cursor"),
    "opencode": ClientSpec("OpenCode", "mcp", jsonc=True, command_array=True,
                           env_key="environment", local_type="local", enabled_key="enabled"),
    "pi": ClientSpec("Pi", enabled_key="enabled"),
    "grok-build": ClientSpec("Grok Build", "mcp_servers", toml=True, local_type=None, enabled_key="enabled"),
    "gemini-cli": ClientSpec("Gemini CLI", local_type=None),
    "kiro": ClientSpec("Kiro", enabled_key="disabled"),
    "vscode": ClientSpec("VS Code", "servers", jsonc=True),
    "copilot-cli": ClientSpec("GitHub Copilot CLI", local_type="local"),
}
CLIENT_NAMES = {key: spec.name for key, spec in CLIENT_SPECS.items()}


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Client:
    key: str
    path: Path
    cli: str | None = None
    detected: bool = False
    issue: str | None = None

    @property
    def name(self) -> str:
        return CLIENT_NAMES[self.key]

    @property
    def servers_key(self) -> str:
        return self.spec.servers_key

    @property
    def spec(self) -> ClientSpec:
        return CLIENT_SPECS[self.key]

    def servers(self, document, *, create=False):
        parent = document
        for key in self.servers_key.split("."):
            if key not in parent:
                if not create:
                    return {}
                parent[key] = {}
            parent = parent[key]
            if not isinstance(parent, Mapping):
                raise ConfigError(f"MCP 配置必须为对象，保留原文件：{self.path}")
        return parent

    def editable_document(self, raw, document):
        if self.spec.toml:
            import tomlkit

            return tomlkit.parse((raw or b"").decode("utf-8-sig"))
        return deepcopy(document)

    def native_entry(self, desired):
        command = [desired["command"], *desired["args"]] if self.spec.command_array else desired["command"]
        entry = {"command": command, self.spec.env_key: desired["env"]}
        if not self.spec.command_array:
            entry["args"] = desired["args"]
        if self.spec.local_type:
            entry["type"] = self.spec.local_type
        return entry

    def normalized_entry(self, entry):
        if not isinstance(entry, Mapping):
            return entry
        command = entry.get("command", "")
        args = entry.get("args", [])
        if self.spec.command_array:
            if not isinstance(command, list) or not command:
                return {}
            command, args = command[0], command[1:]
        return {"command": command, "args": args, "env": entry.get(self.spec.env_key, {}),
                "type": "stdio" if entry.get("type", self.spec.local_type) == self.spec.local_type else entry.get("type")}

    def is_videonote(self, entry):
        return is_videonote(self.normalized_entry(entry))

    def enablement_client(self):
        return Client("cursor", self.path.parent / "mcp-server-enablement.json") if self.key == "gemini-cli" else None

    def read_enablement(self):
        target = self.enablement_client()
        return read_config(target) if target else (None, {})

    def policy_blocked(self, name, document):
        if self.key != "gemini-cli":
            return False
        policy = document.get("mcp", {})
        if not isinstance(policy, Mapping):
            raise ConfigError("Gemini CLI 的 mcp 设置必须为对象")
        allowed, excluded = policy.get("allowed"), policy.get("excluded", [])
        for values in (allowed, excluded):
            if values is not None and (not isinstance(values, list) or not all(isinstance(v, str) for v in values)):
                raise ConfigError("Gemini CLI 的 mcp.allowed / excluded 必须为字符串列表")
        name = name.lower().strip()
        return ((allowed is not None and name not in [v.lower().strip() for v in allowed])
                or name in [v.lower().strip() for v in (excluded or [])])

    def is_enabled(self, entry, document, enablement=None):
        if self.spec.enabled_key and not isinstance(entry.get(self.spec.enabled_key, True), bool):
            raise ConfigError(f"{self.name} 的 MCP 启用标记必须为布尔值；保留原配置")
        if self.key == "gemini-cli":
            name = next((name for name, value in self.servers(document).items() if value == entry), "videonote")
            if self.policy_blocked(name, document):
                return False
            if enablement is None:
                _, enablement = self.read_enablement()
            state = enablement.get(name.lower().strip(), {})
            if not isinstance(state, dict) or not isinstance(state.get("enabled", True), bool):
                raise ConfigError("Gemini CLI 的 MCP 启用状态无效；保留原文件")
            return state.get("enabled", True)
        if self.key == "grok-build":
            return entry.get("enabled", True) is not False and not any(name in document.get("disabled_mcp_servers", [])
                                               for name, value in self.servers(document).items() if value == entry)
        if self.spec.enabled_key:
            default = False if self.spec.enabled_key == "disabled" else True
            value = entry.get(self.spec.enabled_key, default)
            return not value if self.spec.enabled_key == "disabled" else value is not False
        return True

    def toggle_native(self, document, name, enabled):
        if self.key == "grok-build":
            disabled = document.get("disabled_mcp_servers", [])
            if not isinstance(disabled, list):
                raise ConfigError("disabled_mcp_servers 必须为列表；保留原配置")
            disabled = [value for value in disabled if value != name]
            if not enabled:
                disabled.append(name)
            document["disabled_mcp_servers"] = disabled
            self.servers(document)[name]["enabled"] = enabled
        else:
            value = not enabled if self.spec.enabled_key == "disabled" else enabled
            self.servers(document)[name][self.spec.enabled_key] = value


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
    config_root = (home / "Library" / "Application Support" if platform == "darwin" else
                   Path(env.get("APPDATA") or home / "AppData" / "Roaming") if platform == "win32" else
                   Path(env.get("XDG_CONFIG_HOME") or home / ".config"))
    opencode_root = Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"
    opencode_path = Path(env.get("OPENCODE_CONFIG") or opencode_root / "opencode.json")
    if not env.get("OPENCODE_CONFIG") and (opencode_root / "opencode.jsonc").is_file():
        opencode_path = opencode_root / "opencode.jsonc"
    pi_root = Path(env.get("PI_CODING_AGENT_DIR") or home / ".pi" / "agent")
    additional = [
        ("opencode", opencode_path, "opencode"),
        ("pi", pi_root / "mcp.json", "pi"),
        ("grok-build", Path(env.get("GROK_HOME") or home / ".grok") / "config.toml", "grok"),
        ("gemini-cli", Path(env.get("GEMINI_CLI_HOME") or home) / ".gemini" / "settings.json", "gemini"),
        ("kiro", home / ".kiro" / "settings" / "mcp.json", "kiro-cli"),
        ("vscode", config_root / "Code" / "User" / "mcp.json", "code"),
        ("copilot-cli", Path(env.get("COPILOT_HOME") or home / ".copilot") / "mcp-config.json", "copilot"),
    ]
    candidates = [
        Client("claude-code", claude_config, claude_cli, bool(claude_cli) or claude_config.is_file()),
        Client("codex", codex_home / "config.toml", codex_cli, bool(codex_cli) or codex_home.is_dir()),
        Client("claude-desktop", desktop / "claude_desktop_config.json", detected=desktop.is_dir()),
        Client("cursor", home / ".cursor" / "mcp.json", detected=(home / ".cursor").is_dir()),
    ]
    for key, path, command in additional:
        executable = which(command)
        if key == "kiro" and not executable:
            executable = which("kiro")
        detected = bool(executable) or path.is_file() or (
            key not in ("gemini-cli", "grok-build", "opencode") and path.parent.is_dir())
        issue = None
        if (key == "opencode" and not env.get("OPENCODE_CONFIG")
                and (opencode_root / "opencode.json").is_file() and (opencode_root / "opencode.jsonc").is_file()):
            issue = "OpenCode 同时存在 opencode.json 和 opencode.jsonc；请用 OPENCODE_CONFIG 明确目标配置"
        candidates.append(Client(key, path, executable, detected, issue))
    return candidates


def read_config(client: Client) -> tuple[bytes | None, dict]:
    if client.issue:
        raise ConfigError(client.issue)
    try:
        raw = client.path.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError as exc:
        raise ConfigError(f"无法读取 {client.path}（{type(exc).__name__}）") from exc
    try:
        text = raw.decode("utf-8-sig")
        reader = tomllib.loads if client.spec.toml else json_config.loads if client.spec.jsonc else json_config.strict_loads
        document = reader(text) if text.strip() else {}
    except (ValueError, UnicodeError) as exc:
        raise ConfigError(f"配置无法解析，保留原文件：{client.path}") from exc
    if not isinstance(document, dict):
        raise ConfigError(f"MCP 配置必须为对象，保留原文件：{client.path}")
    client.servers(document)
    if client.key == "grok-build":
        disabled = document.get("disabled_mcp_servers", [])
        if not isinstance(disabled, list) or not all(isinstance(name, str) for name in disabled):
            raise ConfigError("disabled_mcp_servers 必须为字符串列表；保留原配置")
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
    servers = client.servers(document)
    aliases = [name for name, entry in servers.items() if client.is_videonote(entry)]
    if len(aliases) > 1:
        raise ConfigError(f"{client.name} 已有多份 VideoNote 注册；请先手动核对")
    name = aliases[0] if aliases else "videonote"
    if "videonote" in servers and "videonote" not in aliases:
        return State(raw, document, "videonote", "conflict")
    entry = servers.get(name)
    if entry is None:
        status = "new"
    elif not client.is_videonote(entry):
        status = "conflict"
    else:
        status = "same" if matches(client.normalized_entry(entry), desired) else "update"
    if status in ("new", "same", "update") and client.key == "pi" and client.cli:
        try:
            result = subprocess.run([client.cli, "--help"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ConfigError("无法核对 Pi 的原生 MCP 支持；请升级 Pi 后重试") from exc
        if result.returncode or not re.search(r"(?mi)^\s*(?:pi\s+)?mcp(?:\s|$)", result.stdout):
            raise ConfigError("此 Pi 版本未确认支持原生 MCP；请升级后重试，不自动安装第三方扩展")
    if client.policy_blocked(name, document):
        raise ConfigError("Gemini CLI 的 mcp.allowed / excluded 阻止此注册；请先在 Agent 中调整")
    if client.key == "gemini-cli":
        client.is_enabled(entry or client.native_entry(desired), document)
    elif entry is not None:
        client.is_enabled(entry, document)
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
    document = client.editable_document(state.raw, state.document)
    servers = client.servers(document, create=True)
    if state.name not in servers:
        servers[state.name] = {}
        if client.key == "copilot-cli":
            servers[state.name]["tools"] = ["*"]
    entry = servers[state.name]
    native = client.native_entry(desired)
    for key, value in native.items():
        if key != client.spec.env_key:
            entry[key] = value
    if not isinstance(entry.get(client.spec.env_key, {}), Mapping):
        raise ConfigError("已有 env 不是对象；保留原配置")
    if client.spec.env_key not in entry:
        entry[client.spec.env_key] = {}
    entry[client.spec.env_key].update(desired["env"])
    return save_document(client, state.raw, document)


def save_document(client: Client, raw: bytes | None, document) -> Path | None:
    """Validate, back up and atomically save a preserving client edit."""
    path = client.path.resolve()  # Respect users' symlinked dotfiles.
    check_unchanged(path, raw)
    if client.spec.toml:
        import tomlkit

    try:
        content = (tomlkit.dumps(document) if client.spec.toml else
                   json_config.dumps_preserving((raw or b"").decode("utf-8-sig"), document))
        # Parse the final text before touching the original file.
        (tomllib.loads if client.spec.toml else json_config.loads if client.spec.jsonc else json_config.strict_loads)(content)
    except (ValueError, UnicodeError) as exc:
        raise ConfigError(f"配置编辑无法通过核对，保留原文件：{client.path}") from exc
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
    if client.cli and client.key in ("claude-code", "codex") and state.status == "new":
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
    entry = client.servers(document).get(state.name, {})
    if not isinstance(entry, dict) or not matches(client.normalized_entry(entry), desired):
        raise ConfigError(f"{client.name} 注册后的配置未通过核对；备份：{saved}")
    return saved
