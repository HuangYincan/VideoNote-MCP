"""随包分发的短指引：不读凭证、不联网，不依赖客户端自动抓取 README。"""
import sys
from urllib.parse import quote

REPOSITORY_URL = "https://github.com/HuangYincan/VideoNote-MCP"
MANUAL_PATH = "docs/04-使用手册.md"

SERVER_INSTRUCTIONS = f"""VideoNote-MCP：视频转写与笔记，官方仓库 {REPOSITORY_URL}。无需安装 Skills。
首次使用或异常时先 health_check、get_config，按 checks[].next_steps 处理；默认当前 Agent 写笔记，
不因未配置 LLM key 阻塞：inspect_video → prepare_note_material → task 轮询 → 读取转写/帧写笔记。
初始化在用户终端运行同版本 videonote setup；登录用 videonote login <平台>。
CLI 与 MCP 须同版本、同 VIDEONOTE_DATA_DIR / VIDEONOTE_CONFIG_DIR。
API Key、Cookie、短信/刷脸验证只在用户终端或官方页面处理，不经 Agent 对话。
安装看 README.md，操作/排障看 {MANUAL_PATH}；先核对 health_check.server_version，
优先读对应发布版本的文档（有 tag 才按 tag 查），main/dev 可能包含尚未发布的修改，不盲目升级/重装/扫码。
MCP 未启动时无法自检，请从 README 和客户端启动日志排查；不能访问仓库时说明限制，不能假装已读。
多格式导出：SRT/VTT/JSON 用 process_media(action='export')；LaTeX/Typst 用
process_media(action='template') 列模板，template_id 选模板，template_file 读文本，out_dir 复制配套文件。
也可读 videonote://templates 和 videonote://help/export。基于已有底稿生成，不重复下载/转写。
模板随包提供，无需 Skills；PDF 需要本机编译器/字体/宏包，未编译成功只能交付源码，不能把样例 PDF 当用户产物。
"""


def project_info(version: str) -> dict:
    return {
        "repository": REPOSITORY_URL,
        "server_version": version,
        "documentation": {
            "install": f"{REPOSITORY_URL}/blob/main/README.md",
            "manual": f"{REPOSITORY_URL}/blob/main/{quote(MANUAL_PATH)}",
            "releases": f"{REPOSITORY_URL}/releases",
            "export_resource": "videonote://help/export",
        },
        "version_policy": "main/dev 文档可能领先已安装版本；先核对 server_version，有对应发布 tag 时优先读该版本文档。",
        "credential_policy": "凭证只在用户终端/官方页面输入，不发送给 Agent。",
    }


def add_next_steps(checks: list[dict]) -> None:
    """只丰富失败检查项，保留原有 ok/detail 判定；不执行安装、删除或配置修改。"""
    if sys.platform == "darwin":
        ffmpeg = "在用户终端安装 FFmpeg；已安装 Homebrew 时可运行 brew install ffmpeg。"
    elif sys.platform == "win32":
        ffmpeg = "在用户终端安装 FFmpeg 并加入 PATH；已启用 winget 时可运行 winget install --id Gyan.FFmpeg -e。"
    else:
        ffmpeg = "使用当前发行版的包管理器安装 FFmpeg；Debian/Ubuntu 示例：sudo apt install ffmpeg。"
    steps = {
        "ffmpeg": [ffmpeg, "重启 MCP 客户端以刷新 PATH，再调用 health_check。"],
        "db": ["检查 data_dir 的可写权限与数据库占用；保留数据库，不自动删除或重建。", "对照官方使用手册排查后重试 health_check。"],
        "encryption": ["检查配置目录可写性；不要删除已有加密密钥，也不要把密钥或配置原文发给 Agent。", "修复权限后重新运行同版本 videonote setup，再调用 health_check。"],
        "disk": ["确认 data_dir 可访问并检查可用空间。", "需要清理时先 cleanup(dry_run=True) 预览，得到用户确认后再清理，不删除配置/模型。", "重新调用 health_check。"],
        "transcriber": ["先调用 get_config 确认引擎与模型状态；正在下载时等待，不重复发起下载。", "在用户终端运行同版本 videonote setup，检查转写引擎、模型下载或云端配置；凭证不经对话。", "核对 CLI/MCP 的数据与配置目录一致，再调用 health_check。"],
        "provider": ["仅走 generate_note/batch_generate_notes 后备 LLM 才需要供应商配置。", "用户终端运行同版本 videonote setup；不要把 API Key 发给 Agent。", "重新调用 health_check(need_provider=True)。"],
        "queue": ["用 list_tasks / task 查看运行中的任务并等待完成；仅经用户确认后取消不再需要的任务。", "空出运行名额后重试，不重复提交同一视频。"],
    }
    for check in checks:
        if not check["ok"] and check["name"] in steps:
            check["code"] = f"{check['name'].upper()}_NOT_READY"
            check["next_steps"] = steps[check["name"]]
