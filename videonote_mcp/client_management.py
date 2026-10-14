"""Enable/disable user-level VideoNote registrations without losing settings."""
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from videonote_mcp.mcp_clients import (
    Client,
    ConfigError,
    check_unchanged,
    clients,
    is_videonote,
    plugin_enabled,
    read_config,
    save_document,
)
from videonote_mcp.terminal_ui import checkbox, confirm, header, select

LABELS = {"enabled": "已启用", "disabled": "已禁用，可恢复", "new": "未安装",
          "plugin": "插件管理，请在 Agent 中设置", "conflict": "注册名称冲突"}


@dataclass
class Registration:
    client: Client
    raw: bytes | None
    document: dict
    name: str
    status: str
    entry: dict | None = None
    archive_raw: bytes | None = None


def archive_client(client: Client, config: Path) -> Client:
    return Client("cursor", config / "client_registrations" / f"{client.key}.json")


def inspect_registration(client: Client, config: Path) -> Registration:
    raw, document = read_config(client)
    result = Registration(client, raw, document, "videonote", "new")
    if plugin_enabled(client, document):
        result.status = "plugin"
        return result
    servers = document.get(client.servers_key, {})
    aliases = [name for name, entry in servers.items() if is_videonote(entry)]
    if len(aliases) > 1:
        raise ConfigError(f"{client.name} 已有多份 VideoNote 注册；请先手动核对")
    if "videonote" in servers and "videonote" not in aliases:
        result.status = "conflict"
        return result
    if aliases:
        result.name = aliases[0]
        result.entry = servers[result.name]
        result.status = "disabled" if client.key == "codex" and result.entry.get("enabled") is False else "enabled"
        return result
    archive_raw, archive = read_config(archive_client(client, config))
    result.archive_raw = archive_raw
    if archive_raw is not None:
        if (archive.get("path") != str(client.path.absolute())
                or not isinstance(archive.get("name"), str) or not archive["name"]
                or not is_videonote(archive.get("entry"))):
            raise ConfigError(f"{client.name} 的禁用存档与当前配置不匹配；保留原文件")
        result.name, result.entry = archive["name"], archive["entry"]
        result.status = "conflict" if result.name in servers else "disabled"
    return result


def set_enabled(registration: Registration, enabled: bool, config: Path) -> Path | None:
    """Apply a reviewed snapshot. JSON targets archive before removing an entry."""
    client = registration.client
    check_unchanged(client.path, registration.raw)
    fresh = inspect_registration(client, config)
    if (fresh.status, fresh.name, fresh.entry, fresh.archive_raw) != (
            registration.status, registration.name, registration.entry, registration.archive_raw):
        raise ConfigError("客户端注册状态已变化；请重新选择")
    if registration.status not in ("enabled", "disabled"):
        raise ConfigError("此注册不能自动管理；请先安装或在 Agent 中处理插件/冲突")
    if enabled == (registration.status == "enabled"):
        return None
    if client.key == "codex":
        import tomlkit

        document = tomlkit.parse((registration.raw or b"").decode("utf-8-sig"))
        document[client.servers_key][registration.name]["enabled"] = enabled
    else:
        # Read a fresh document so changing it does not mutate the UI snapshot.
        _, document = read_config(client)
        servers = document.setdefault(client.servers_key, {})
        archive_target = archive_client(client, config)
        if enabled:
            check_unchanged(archive_target.path, registration.archive_raw)
            if registration.name in servers:
                raise ConfigError("原注册名称已被占用；保留当前配置")
            servers[registration.name] = registration.entry
        else:
            archive_raw, _ = read_config(archive_target)
            archive = {"path": str(client.path.absolute()), "name": registration.name,
                       "entry": registration.entry}
            # Secure atomic storage completes before touching the active config.
            save_document(archive_target, archive_raw, archive)
            del servers[registration.name]
    saved = save_document(client, registration.raw, document)
    if inspect_registration(client, config).status != ("enabled" if enabled else "disabled"):
        raise ConfigError(f"{client.name} 设置回读未通过；备份：{saved}")
    return saved


def manage_enabled(registrations: list[Registration], config: Path, inq=None) -> None:
    managed = [item for item in registrations if item.status in ("enabled", "disabled")]
    if not managed:
        print("没有可管理的独立注册，请先选择安装平台。", file=sys.stdout)
        return
    choices = [{"name": f"{item.client.name} — {LABELS[item.status]}", "value": item.client.key,
                "enabled": item.status == "enabled"} for item in managed]
    selected = checkbox("勾选要启用的平台；取消勾选将禁用 VideoNote", choices, inq=inq)
    changes = [(item, item.client.key in selected) for item in managed
               if (item.client.key in selected) != (item.status == "enabled")]
    if not changes:
        print("启用状态未变化。", file=sys.stdout)
        return
    for item, enabled in changes:
        print(f"  {item.client.name}：{'启用' if enabled else '禁用'} VideoNote", file=sys.stdout)
    if not confirm("应用以上变更？", inq=inq):
        return
    # Validate every selected snapshot before beginning any writes.
    for item, _ in changes:
        check_unchanged(item.client.path, item.raw)
    for item, enabled in changes:
        try:
            saved = set_enabled(item, enabled, config)
            print(f"✓ {item.client.name} 已{'启用' if enabled else '禁用'}", file=sys.stdout)
            if saved:
                print(f"  原配置备份：{saved}", file=sys.stdout)
        except (ConfigError, OSError) as exc:
            print(f"{item.client.name}：{exc}", file=sys.stdout)
    print("请重启对应 Agent 或重新连接 MCP。", file=sys.stdout)


def wizard(config: Path, inq=None) -> None:
    while True:
        header("配置向导", "① Agent 客户端")
        available = clients(sys.platform, Path.home(), dict(os.environ))
        registrations = []
        for client in available:
            try:
                item = inspect_registration(client, config)
                registrations.append(item)
                print(f"  {client.name}：{LABELS[item.status]}", file=sys.stdout)
            except (ConfigError, OSError) as exc:
                print(f"  {client.name}：{exc}", file=sys.stdout)
        print("\n管理当前用户的 VideoNote MCP；项目注册和插件请在 Agent 中管理。", file=sys.stdout)
        try:
            action = select("Agent 客户端设置", [
                {"name": "启用的平台（多选）", "value": "manage"},
                {"name": "安装 / 更新平台（多选）", "value": "install"},
                {"name": "返回", "value": "back"},
            ], "manage", inq=inq)
            if action == "back":
                return
            if action == "manage":
                manage_enabled(registrations, config, inq)
            else:
                candidates = [item for item in registrations if item.status in ("new", "enabled")]
                if not candidates:
                    print("没有可安装的平台；已禁用的平台可从启用菜单恢复。", file=sys.stdout)
                    continue
                keys = checkbox("安装 / 更新到哪些平台？", [
                    {"name": f"{item.client.name} — {LABELS[item.status]}"
                             + ("（未检测到应用）" if not item.client.detected else ""),
                     "value": item.client.key, "enabled": False} for item in candidates], inq=inq)
                if keys and confirm("准备运行环境并安装 / 更新所选平台？", inq=inq):
                    from videonote_mcp.installer import main

                    arguments = ["--yes", "--replace", "--asr", "keep", "--no-download"]
                    for key in keys:
                        arguments.extend(["--client", key])
                    if main(arguments):
                        print("部分平台安装失败；请查看上方诊断。", file=sys.stdout)
        except (EOFError, KeyboardInterrupt):
            return
        except (ConfigError, OSError) as exc:
            print(str(exc), file=sys.stdout)
        try:
            select("继续", [{"name": "返回 Agent 客户端设置", "value": "back"}], "back", inq=inq)
        except (EOFError, KeyboardInterrupt):
            return
