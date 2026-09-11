"""隔离 stdio 冒烟：源码/wheel，验证 10 工具、预检、转写导出与模板；不下载视频/模型。"""
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


async def smoke_artifacts(session: ClientSession, data: Path):
    """Exercise shared modules through the actual protocol, including packaged installs."""
    source = data / "本地 视频.mp4"
    source.write_bytes(b"metadata-only existence check")
    inspected = payload(await session.call_tool("inspect_video", {"url": source.as_uri()}))
    assert inspected["ok"] and inspected["entries"][0]["url"] == str(source)
    outside = data.parent / "outside.mp4"
    outside.write_bytes(b"must not expose")
    denied = payload(await session.call_tool("inspect_video", {"url": outside.as_uri()}))
    assert not denied["ok"] and "VIDEONOTE_ALLOW_EXTERNAL_PATHS" in denied["error"]

    task_id = "smoke-transcript"
    task_dir = data / "note_results" / task_id
    (task_dir / "gen").mkdir(parents=True)
    status_file = task_dir / "status.json"
    status_file.write_text('{"status":"SUCCESS"}', encoding="utf-8")
    cache = task_dir / "gen" / "transcript.json"
    cache.write_text("[]", encoding="utf-8")  # unusable cache must fall back everywhere
    transcript = {"language": "zh", "full_text": "离线共享转写",
                  "segments": [{"start": 0, "end": 1, "text": "离线共享转写"}]}
    (task_dir / "result.json").write_text(json.dumps({"transcript": transcript}), encoding="utf-8")
    read = payload(await session.call_tool("task", {"task_id": task_id, "action": "transcript"}))
    assert read["ok"] and read["full_text"] == transcript["full_text"]
    resource = await session.read_resource(f"videonote://task/{task_id}/transcript")
    assert transcript["full_text"] in resource.contents[0].text
    exported = payload(await session.call_tool("process_media", {
        "task_id": task_id, "formats": ["srt", "vtt", "json"],
    }))
    assert exported["ok"] and set(exported["formats"]) == {"srt", "vtt", "json"}
    content = json.loads((task_dir / "gen" / "transcript.export.json").read_text(encoding="utf-8"))
    assert content["full_text"] == transcript["full_text"]
    assert cache.read_text(encoding="utf-8") == "[]"
    status_file.write_text('{"status":"FAILED"}', encoding="utf-8")
    failed = payload(await session.call_tool("process_media", {"task_id": task_id}))
    assert not failed["ok"] and "FAILED" in failed["error"]


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
            await smoke_artifacts(session, data)
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
    print("stdio OK: metadata, 10 tools, local inspection, transcript fallback/export, resources, 3 offline templates (licenses verified), path rejection")


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
