"""Native client schemas, detection and preserving JSONC configuration edits."""
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from videonote_mcp import json_config, mcp_clients
from videonote_mcp.mcp_clients import CLIENT_NAMES, Client, ConfigError


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))


@pytest.fixture
def desired():
    return {"type": "stdio", "command": "/runner with spaces/uvx",
            "args": ["videonote==0.2.4"], "env": {"DATA": "shared", "DEVICE": "cpu"}}


@pytest.mark.parametrize("key", tuple(CLIENT_NAMES))
def test_native_registration_merge_and_readback(tmp_path, desired, key):
    client = Client(key, tmp_path / ("config.toml" if mcp_clients.CLIENT_SPECS[key].toml else "config.json"))
    state = mcp_clients.inspect(client, desired)
    mcp_clients.write_config(client, state, desired)
    assert mcp_clients.inspect(client, desired).status == "same"
    raw, document = mcp_clients.read_config(client)
    servers = client.servers(document)
    assert mcp_clients.matches(client.normalized_entry(servers["videonote"]), desired)
    if key == "opencode":
        assert servers["videonote"]["command"] == [desired["command"], *desired["args"]]
        assert "args" not in servers["videonote"] and "environment" in servers["videonote"]
    if key == "copilot-cli":
        assert servers["videonote"]["tools"] == ["*"]
    updated = client.editable_document(raw, document)
    native = client.servers(updated)["videonote"]
    native[client.spec.env_key]["TOKEN"] = "private"
    native["timeout"] = 90
    native["enabled"] = False
    if client.spec.local_type:
        native["type"] = client.spec.local_type
    client.servers(updated)["unrelated"] = {"command": "other"}
    updated["theme"] = "keep"
    mcp_clients.save_document(client, raw, updated)
    new = {**desired, "args": ["videonote==0.2.5"], "env": {"DEVICE": "cuda"}}
    state = mcp_clients.inspect(client, new)
    assert state.status == "update"
    saved = mcp_clients.write_config(client, state, new)
    assert saved.is_file()
    _, final = mcp_clients.read_config(client)
    native = client.servers(final)["videonote"]
    assert final["theme"] == "keep" and client.servers(final)["unrelated"]["command"] == "other"
    assert native["timeout"] == 90 and native["enabled"] is False
    assert native[client.spec.env_key] == {"DATA": "shared", "DEVICE": "cuda", "TOKEN": "private"}


@pytest.mark.parametrize("key", ["opencode", "vscode"])
def test_jsonc_merge_preserves_other_server_comments_and_settings(tmp_path, desired, key):
    client = Client(key, tmp_path / "settings.jsonc")
    server_key = client.servers_key
    original = ('// user header\n{\n  "theme": "https://example.test/a//b", // user theme\n'
                f'  "{server_key}": {{\n'
                '    // do not remove this other server comment\n'
                '    "other": {"command": "other",},\n  },\n}\n')
    client.path.write_text(original)
    saved = mcp_clients.write_config(client, mcp_clients.inspect(client, desired), desired)
    assert saved.read_text() == original
    text = client.path.read_text()
    assert "// user header" in text and "// user theme" in text and "other server comment" in text
    _, document = mcp_clients.read_config(client)
    assert document["theme"] == "https://example.test/a//b"
    assert client.servers(document)["other"] == {"command": "other"}
    assert mcp_clients.inspect(client, desired).status == "same"


@pytest.mark.parametrize("text", ['{"x": 1 2}', '{"x": true false}', '{"x":1,"x":2}',
                                  '{"x":1,,}', '{"x":"unterminated}', '{/* unterminated}'])
def test_jsonc_rejects_invalid_or_ambiguous_configuration(text):
    with pytest.raises(ValueError):
        json_config.loads(text)


@pytest.mark.parametrize("text,new", [
    ('{"a":1,"b":2,"c":3}', {"b": 2}),
    ('{"a":1,"b":2,}', {}),
    ('{\n // keep b\n "b": [1,2,],\n "c": 3,\n}', {"b": [1, 2], "d": {"nested": True}}),
    ('{}', {"x": {"nested": [1, 2]}}),
    ('{ /* keep */ "a": {"x": 1, // keep nested\n "y": 2}, "b": true}', {"a": {"y": 2}, "b": True}),
    ('{"a":1 /* tail comment */, "b":2}', {"a": 3, "b": 2}),
])
def test_jsonc_edits_nested_fields_additions_and_deletions(text, new):
    result = json_config.dumps_preserving(text, new)
    assert json_config.loads(result) == new
    if "tail comment" in text:
        assert "tail comment" in result


@pytest.mark.parametrize("platform,code_path", [
    ("darwin", "Library/Application Support/Code/User/mcp.json"),
    ("linux", ".config/Code/User/mcp.json"),
    ("win32", "AppData/Roaming/Code/User/mcp.json"),
])
def test_additional_client_paths_and_cli_detection(tmp_path, platform, code_path):
    binaries = {"opencode", "pi", "grok", "gemini", "kiro-cli", "code", "copilot"}
    found = {client.key: client for client in mcp_clients.clients(
        platform, tmp_path, {}, which=lambda name: "/bin/" + name if name in binaries else None)}
    assert set(found) == set(CLIENT_NAMES) and len(found) == 11
    assert found["opencode"].path == tmp_path / ".config/opencode/opencode.json"
    assert found["pi"].path == tmp_path / ".pi/agent/mcp.json"
    assert found["grok-build"].path == tmp_path / ".grok/config.toml"
    assert found["gemini-cli"].path == tmp_path / ".gemini/settings.json"
    assert found["vscode"].path == tmp_path / code_path
    assert found["copilot-cli"].path == tmp_path / ".copilot/mcp-config.json"
    assert all(found[key].detected for key in ("opencode", "pi", "grok-build", "gemini-cli", "vscode", "copilot-cli", "kiro"))


def test_custom_config_roots_and_jsonc_detection(tmp_path):
    root = tmp_path / "xdg/opencode"
    root.mkdir(parents=True)
    (root / "opencode.jsonc").write_text('{"mcp":{}}')
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "PI_CODING_AGENT_DIR": str(tmp_path / "custom-pi"),
           "GROK_HOME": str(tmp_path / "custom-grok"), "GEMINI_CLI_HOME": str(tmp_path / "custom-gemini"),
           "COPILOT_HOME": str(tmp_path / "custom-copilot")}
    found = {client.key: client for client in mcp_clients.clients("linux", tmp_path, env, which=lambda _: None)}
    assert found["opencode"].path == root / "opencode.jsonc" and found["opencode"].detected
    assert found["pi"].path == tmp_path / "custom-pi/mcp.json"
    assert found["grok-build"].path == tmp_path / "custom-grok/config.toml"
    assert found["gemini-cli"].path == tmp_path / "custom-gemini/.gemini/settings.json"
    assert found["copilot-cli"].path == tmp_path / "custom-copilot/mcp-config.json"
    assert found["vscode"].path == tmp_path / "xdg/Code/User/mcp.json"


def test_ambiguous_opencode_files_require_explicit_config(tmp_path):
    root = tmp_path / ".config/opencode"
    root.mkdir(parents=True)
    for name in ("opencode.json", "opencode.jsonc"):
        (root / name).write_text("{}")
    client = next(c for c in mcp_clients.clients("linux", tmp_path, {}, which=lambda _: None) if c.key == "opencode")
    with pytest.raises(ConfigError, match="OPENCODE_CONFIG"):
        mcp_clients.read_config(client)
    chosen = root / "opencode.jsonc"
    client = next(c for c in mcp_clients.clients("linux", tmp_path, {"OPENCODE_CONFIG": str(chosen)},
                                               which=lambda _: None) if c.key == "opencode")
    assert client.path == chosen and mcp_clients.read_config(client)[1] == {}


def test_shared_gemini_folder_does_not_misidentify_gemini_cli(tmp_path):
    (tmp_path / ".gemini/antigravity").mkdir(parents=True)
    client = next(c for c in mcp_clients.clients("linux", tmp_path, {}, which=lambda _: None) if c.key == "gemini-cli")
    assert not client.detected


@pytest.mark.parametrize("stdout,expected", [("Usage: pi\n  pi mcp add name\n", True), ("Usage: pi [options]\n", False)])
def test_pi_registration_requires_native_mcp_support(tmp_path, desired, monkeypatch, stdout, expected):
    client = Client("pi", tmp_path / "mcp.json", cli="/bin/pi")
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=stdout))
    monkeypatch.setattr(mcp_clients.subprocess, "run", run)
    if expected:
        assert mcp_clients.inspect(client, desired).status == "new"
    else:
        with pytest.raises(ConfigError, match="原生 MCP"):
            mcp_clients.inspect(client, desired)
    assert run.call_args.args[0] == ["/bin/pi", "--help"]
    assert not client.path.exists()


def test_pi_capability_probe_is_bounded_and_fails_closed(tmp_path, desired, monkeypatch):
    monkeypatch.setattr(mcp_clients.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("pi", 5)))
    with pytest.raises(ConfigError, match="Pi"):
        mcp_clients.inspect(Client("pi", tmp_path / "mcp.json", cli="/bin/pi"), desired)


@pytest.mark.parametrize("policy", [{"allowed": ["other"]}, {"excluded": ["VideoNote"]}])
def test_gemini_policy_is_preserved_and_blocks_install(tmp_path, desired, policy):
    client = Client("gemini-cli", tmp_path / "settings.json")
    text = json.dumps({"mcp": policy, "theme": "keep"})
    client.path.write_text(text)
    with pytest.raises(ConfigError, match="allowed / excluded"):
        mcp_clients.inspect(client, desired)
    assert client.path.read_text() == text


def test_gemini_malformed_enablement_blocks_install_before_writing(tmp_path, desired):
    client = Client("gemini-cli", tmp_path / "settings.json")
    target = client.enablement_client()
    target.path.write_text('{"videonote":{"enabled":"false"}}')
    with pytest.raises(ConfigError, match="启用状态无效"):
        mcp_clients.inspect(client, desired)
    assert not client.path.exists()
