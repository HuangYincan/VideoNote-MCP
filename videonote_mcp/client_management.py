"""Enable/disable user-level VideoNote registrations without losing settings."""
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from videonote_mcp.mcp_clients import (
    Client,
    ConfigError,
    backup,
    check_unchanged,
    clients,
    plugin_enabled,
    read_config,
    save_document,
)
from videonote_mcp.terminal_ui import checkbox, confirm, header, message, select
from videonote_mcp.terminal_ui import field as ui_field

LABELS = {"enabled": "注册已启用", "disabled": "注册已禁用，可恢复", "new": "无独立注册",
          "plugin": "插件管理，请在 Agent 中设置", "conflict": "注册名称冲突"}
STATUS_ROLES = {"enabled": "success", "disabled": "warning", "new": "muted",
                "plugin": "warning", "conflict": "error"}


@dataclass
class Registration:
    client: Client
    raw: bytes | None
    document: dict
    name: str
    status: str
    entry: dict | None = None
    archive_raw: bytes | None = None
    enablement_raw: bytes | None = None
    enablement: dict = field(default_factory=dict)


@dataclass
class UninstallPlan:
    registration: Registration
    archive_raw: bytes | None


def archive_client(client: Client, config: Path) -> Client:
    return Client("cursor", config / "client_registrations" / f"{client.key}.json")


def inspect_registration(client: Client, config: Path) -> Registration:
    raw, document = read_config(client)
    result = Registration(client, raw, document, "videonote", "new")
    result.enablement_raw, result.enablement = client.read_enablement()
    if plugin_enabled(client, document):
        result.status = "plugin"
        return result
    servers = client.servers(document)
    aliases = [name for name, entry in servers.items() if client.is_videonote(entry)]
    if len(aliases) > 1:
        raise ConfigError(f"{client.name} 已有多份 VideoNote 注册；请先手动核对")
    if "videonote" in servers and "videonote" not in aliases:
        result.status = "conflict"
        return result
    if aliases:
        result.name = aliases[0]
        result.entry = servers[result.name]
        result.status = "enabled" if client.is_enabled(result.entry, document, result.enablement) else "disabled"
        return result
    archive_raw, archive = read_config(archive_client(client, config))
    result.archive_raw = archive_raw
    if archive_raw is not None:
        if (archive.get("path") != str(client.path.absolute())
                or not isinstance(archive.get("name"), str) or not archive["name"]
                or not client.is_videonote(archive.get("entry"))):
            raise ConfigError(f"{client.name} 的禁用存档与当前配置不匹配；保留原文件")
        result.name, result.entry = archive["name"], archive["entry"]
        result.status = "conflict" if result.name in servers else "disabled"
    return result


def set_enabled(registration: Registration, enabled: bool, config: Path) -> Path | None:
    """Apply a reviewed snapshot. JSON targets archive before removing an entry."""
    client = registration.client
    check_unchanged(client.path, registration.raw)
    fresh = inspect_registration(client, config)
    if (fresh.status, fresh.name, fresh.entry, fresh.archive_raw, fresh.enablement_raw) != (
            registration.status, registration.name, registration.entry, registration.archive_raw, registration.enablement_raw):
        raise ConfigError("客户端注册状态已变化；请重新选择")
    if registration.status not in ("enabled", "disabled"):
        raise ConfigError("此注册不能自动管理；请先安装或在 Agent 中处理插件/冲突")
    if enabled == (registration.status == "enabled"):
        return None
    if enabled and client.policy_blocked(registration.name, registration.document):
        raise ConfigError("Gemini CLI 的 mcp.allowed / excluded 阻止此注册；请先在 Agent 中调整")
    if client.key == "gemini-cli" and registration.name in client.servers(registration.document):
        target = client.enablement_client()
        check_unchanged(target.path, registration.enablement_raw)
        document = dict(registration.enablement)
        name = registration.name.lower().strip()
        if enabled:
            document.pop(name, None)
        else:
            document[name] = {"enabled": False}
        saved = save_document(target, registration.enablement_raw, document)
        if inspect_registration(client, config).status != ("enabled" if enabled else "disabled"):
            raise ConfigError(f"{client.name} 启用状态回读未通过；备份：{saved}")
        return saved
    if client.spec.enabled_key and registration.name in client.servers(registration.document):
        document = client.editable_document(registration.raw, registration.document)
        client.toggle_native(document, registration.name, enabled)
    else:
        # Read a fresh document so changing it does not mutate the UI snapshot.
        _, document = read_config(client)
        servers = client.servers(document, create=True)
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


def prepare_uninstall(registration: Registration, config: Path) -> UninstallPlan:
    """Snapshot both the live config and any recovery archive before consent."""
    client = registration.client
    check_unchanged(client.path, registration.raw)
    fresh = inspect_registration(client, config)
    if (fresh.status, fresh.name, fresh.entry, fresh.archive_raw, fresh.enablement_raw) != (
            registration.status, registration.name, registration.entry, registration.archive_raw, registration.enablement_raw):
        raise ConfigError("客户端注册状态已变化；请重新选择")
    if fresh.status not in ("enabled", "disabled", "new"):
        raise ConfigError("仅卸载独立用户级注册；插件或名称冲突请在 Agent 中处理")
    raw, archive = read_config(archive_client(client, config))
    if raw is not None and (archive.get("path") != str(client.path.absolute())
                            or not isinstance(archive.get("name"), str) or not archive["name"]
                            or not client.is_videonote(archive.get("entry"))):
        raise ConfigError(f"{client.name} 的禁用存档与当前配置不匹配；保留原文件")
    return UninstallPlan(registration, raw)


def uninstall(plan: UninstallPlan, config: Path) -> tuple[Path | None, Path | None]:
    """Remove only the reviewed registration and recovery archive, with backups."""
    registration, client = plan.registration, plan.registration.client
    fresh = prepare_uninstall(registration, config)
    if fresh.archive_raw != plan.archive_raw:
        raise ConfigError("禁用存档在选择后发生变化；请重新选择")
    archive = archive_client(client, config)
    check_unchanged(archive.path, plan.archive_raw)
    archive_backup = backup(archive.path, plan.archive_raw)
    saved = None
    if registration.name in client.servers(registration.document):
        document = client.editable_document(registration.raw, registration.document)
        if client.key == "grok-build":
            client.toggle_native(document, registration.name, True)
        del client.servers(document)[registration.name]
        saved = save_document(client, registration.raw, document)
    raw, document = read_config(client)
    if any(client.is_videonote(entry) for entry in client.servers(document).values()):
        raise ConfigError(f"{client.name} 注册移除后回读未通过；备份：{saved}")
    check_unchanged(client.path, raw)
    check_unchanged(archive.path, plan.archive_raw)
    enablement_target = client.enablement_client()
    if enablement_target:
        check_unchanged(enablement_target.path, registration.enablement_raw)
        name = registration.name.lower().strip()
        if name in registration.enablement:
            enablement = dict(registration.enablement)
            del enablement[name]
            try:
                save_document(enablement_target, registration.enablement_raw, enablement)
            except (ConfigError, OSError) as exc:
                raise ConfigError(f"{client.name} 用户级注册已移除，但启用状态清理失败；"
                                  f"请核对 {enablement_target.path}。原注册备份：{saved}") from exc
    if plan.archive_raw is not None:
        try:
            archive.path.unlink()
        except OSError as exc:
            raise ConfigError(f"{client.name} 用户级注册已移除，但禁用存档删除失败；"
                              f"请重试卸载。存档备份：{archive_backup}") from exc
    if inspect_registration(client, config).status != "new":
        raise ConfigError(f"{client.name} 卸载后状态核对未通过；请重新检查")
    return saved, archive_backup


def manage_uninstall(registrations: list[Registration], config: Path, inq=None) -> None:
    plans = []
    for item in registrations:
        if item.status not in ("enabled", "disabled"):
            continue
        try:
            plans.append(prepare_uninstall(item, config))
        except (ConfigError, OSError) as exc:
            message(f"{item.client.name}：{exc}", "error")
    if not plans:
        message("没有可卸载的独立用户级注册。", "muted")
        return
    selected = checkbox("选择要卸载 VideoNote 注册的平台", [
        {"name": f"{plan.registration.client.name} — {LABELS[plan.registration.status]}",
         "value": plan.registration.client.key, "enabled": False} for plan in plans], inq=inq)
    chosen = [plan for plan in plans if plan.registration.client.key in selected]
    if not chosen:
        message("未选择卸载平台。", "muted")
        return
    for plan in chosen:
        ui_field(plan.registration.client.name, "删除用户级注册及禁用恢复存档", role="warning")
    message("保留 VideoNote 程序、模型和笔记。", "muted")
    if not confirm("卸载以上平台的 VideoNote 注册？", inq=inq):
        return
    for plan in chosen:
        fresh = prepare_uninstall(plan.registration, config)
        if fresh.archive_raw != plan.archive_raw:
            raise ConfigError("禁用存档在选择后发生变化；请重新选择")
    for plan in chosen:
        try:
            saved, archive_saved = uninstall(plan, config)
            message(f"✓ {plan.registration.client.name} 用户级注册已卸载", "success")
            for path in (saved, archive_saved):
                if path:
                    ui_field("原配置备份", path, role="muted")
        except (ConfigError, OSError) as exc:
            message(f"{plan.registration.client.name}：{exc}", "error")
    message("请重启对应 Agent 或重新连接 MCP；插件、项目配置及兼容导入需在 Agent 中核对。")


def manage_enabled(registrations: list[Registration], config: Path, inq=None) -> None:
    managed = [item for item in registrations if item.status in ("enabled", "disabled")]
    if not managed:
        message("没有可管理的独立注册，请先选择安装平台。", "muted")
        return
    choices = [{"name": f"{item.client.name} — {LABELS[item.status]}", "value": item.client.key,
                "enabled": item.status == "enabled"} for item in managed]
    selected = checkbox("勾选要启用的平台；取消勾选将禁用 VideoNote", choices, inq=inq)
    changes = [(item, item.client.key in selected) for item in managed
               if (item.client.key in selected) != (item.status == "enabled")]
    if not changes:
        message("启用状态未变化。", "muted")
        return
    for item, enabled in changes:
        ui_field(item.client.name, f"{'启用' if enabled else '禁用'} VideoNote", role="success" if enabled else "warning")
    if not confirm("应用以上变更？", inq=inq):
        return
    # Validate every selected snapshot before beginning any writes.
    for item, _ in changes:
        check_unchanged(item.client.path, item.raw)
    for item, enabled in changes:
        try:
            saved = set_enabled(item, enabled, config)
            message(f"✓ {item.client.name} 注册已{'启用' if enabled else '禁用'}", "success" if enabled else "warning")
            if saved:
                ui_field("原配置备份", saved, role="muted")
        except (ConfigError, OSError) as exc:
            message(f"{item.client.name}：{exc}", "error")
    message("请重启对应 Agent 或重新连接 MCP。")


def wizard(config: Path, inq=None) -> None:
    while True:
        header("配置向导", "① Agent 客户端")
        available = clients(sys.platform, Path.home(), dict(os.environ))
        registrations = []
        for client in available:
            try:
                item = inspect_registration(client, config)
                registrations.append(item)
                if client.detected or item.status != "new":
                    ui_field(client.name, LABELS[item.status], role=STATUS_ROLES[item.status])
            except (ConfigError, OSError) as exc:
                message(f"  {client.name}：{exc}", "error")
        message("\n管理当前用户的 VideoNote MCP；项目注册和插件请在 Agent 中管理。", "muted")
        try:
            action = select("Agent 客户端设置", [
                {"name": "启用的平台（多选）", "value": "manage"},
                {"name": "安装 / 更新平台（多选）", "value": "install"},
                {"name": "卸载 VideoNote 注册（多选）", "value": "uninstall"},
                {"name": "返回", "value": "back"},
            ], "manage", inq=inq)
            if action == "back":
                return
            if action == "manage":
                manage_enabled(registrations, config, inq)
            elif action == "uninstall":
                manage_uninstall(registrations, config, inq)
            else:
                candidates = [item for item in registrations if item.status in ("new", "enabled")]
                if not candidates:
                    message("没有可安装的平台；已禁用的平台可从启用菜单恢复。", "muted")
                    continue
                from videonote_mcp.installer import choose_clients, main

                selected = choose_clients([item.client for item in candidates], inq=inq, title="配置向导")
                if selected and confirm("准备运行环境并安装 / 更新所选平台？", inq=inq):

                    arguments = ["--yes", "--replace", "--asr", "keep", "--no-download"]
                    for client in selected:
                        arguments.extend(["--client", client.key])
                    if main(arguments):
                        message("部分平台安装失败；请查看上方诊断。", "warning")
        except (EOFError, KeyboardInterrupt):
            return
        except (ConfigError, OSError) as exc:
            message(str(exc), "error")
        try:
            select("继续", [{"name": "返回 Agent 客户端设置", "value": "back"}], "back", inq=inq)
        except (EOFError, KeyboardInterrupt):
            return
