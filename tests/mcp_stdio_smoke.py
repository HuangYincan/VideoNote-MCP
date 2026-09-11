"""隔离 stdio 冒烟：源码或 wheel，验证初始化/10 工具/离线模板/复制；不下载视频或模型。"""
import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED = sorted([
    "batch_generate_notes", "cleanup", "generate_note", "get_config", "health_check",
    "inspect_video", "list_tasks", "prepare_note_material", "process_media", "task",
])
REPOSITORY = "https://github.com/HuangYincan/VideoNote-MCP"


def payload(result):
    assert not result.isError, result
    return json.loads(result.content[0].text)


async def smoke(package_root: Path, data: Path):
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "videonote_mcp.cli"], cwd=str(data.parent),
        env={"PYTHONPATH": str(package_root), "PATH": os.environ.get("PATH", ""),
             "VIDEONOTE_DATA_DIR": str(data), "VIDEONOTE_CONFIG_DIR": str(data / "config"),
             "NOTE_OUTPUT_DIR": str(data / "note_results"),
             "DATABASE_URL": f"sqlite:///{data / 'video_note.db'}"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.serverInfo.websiteUrl == REPOSITORY
            assert "health_check" in init.instructions and "process_media" in init.instructions
            tools = await session.list_tools()
            assert sorted(t.name for t in tools.tools) == EXPECTED
            process = next(t for t in tools.tools if t.name == "process_media")
            assert "template" in process.inputSchema["properties"]["action"]["enum"]
            health = payload(await session.call_tool("health_check"))
            config = payload(await session.call_tool("get_config"))
            assert health["project"]["repository"] == config["project"]["repository"] == REPOSITORY
            assert init.serverInfo.version == health["server_version"] == config["project"]["server_version"]
            assert health["data_dir"] == str(data)
            assert health["skill_refresh"] == ""
            for check in health["checks"]:
                if not check["ok"]:
                    assert check["next_steps"]
            resources = await session.list_resources()
            assert {"videonote://templates", "videonote://help/export"} <= {str(r.uri) for r in resources.resources}
            guide = await session.read_resource("videonote://help/export")
            assert "GUIDE.md" in guide.contents[0].text
            catalog = payload(await session.call_tool("process_media", {"action": "template"}))
            assert len(catalog["templates"]) == 3
            for template in catalog["templates"]:
                tid = template["id"]
                src = payload(await session.call_tool("process_media", {
                    "action": "template", "template_id": tid, "template_file": template["entrypoint"],
                }))
                assert src["content"]
                # 文件 URI 必须来自选定源码 / 解包后的 wheel，不能误用开发仓库的资源。
                assert all(f["uri"].startswith(package_root.as_uri() + "/") for f in src["files"])
                resource = await session.read_resource(f"videonote://templates/{tid}")
                assert json.loads(resource.contents[0].text)["template_id"] == tid
                out_dir = data / "exports" / tid
                copied = payload(await session.call_tool("process_media", {
                    "action": "template", "template_id": tid, "out_dir": str(out_dir),
                }))
                assert copied["copied"] and (out_dir / template["entrypoint"]).is_file()
                assert (out_dir / ("LICENSE" if template["format"] == "typst" else "LICENSE-LPPL-1.3c.txt")).is_file()
            traversal = await session.call_tool("process_media", {
                "action": "template", "template_id": "latex-math-note", "template_file": "../../config.json",
            })
            assert traversal.isError
    print("stdio OK: source metadata, 10 tools, resources, 3 offline templates, licensed copies, path rejection")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="videonote-smoke-") as temporary:
        root = Path(temporary).resolve()
        package_root = Path(__file__).resolve().parents[1]
        if args.wheel:
            package_root = root / "wheel"
            with zipfile.ZipFile(args.wheel) as wheel:
                names = wheel.namelist()
                assert not any(n.startswith(("skills/", "commands/")) or n.endswith("/SKILL.md") for n in names)
                for name in names:
                    assert not Path(name).is_absolute() and ".." not in Path(name).parts
                wheel.extractall(package_root)
            templates = package_root / "videonote_mcp/templates"
            assert (templates / "README.md").is_file()
            provenance = json.loads((templates / "provenance.json").read_text())
            assert len(provenance["files"]) == 26
            for name, sha256 in provenance["files"].items():
                assert hashlib.sha256((templates / name).read_bytes()).hexdigest() == sha256
        asyncio.run(smoke(package_root, root / "data"))


if __name__ == "__main__":
    main()
