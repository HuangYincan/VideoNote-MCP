"""模板作为包资源恢复；经实际工具面读取/复制，无 Skills、无编译副作用。"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from videonote_mcp import server
from videonote_mcp.export import FORMATS
from videonote_mcp.export import templates as templates_module

REPO = Path(__file__).resolve().parents[1]
IDS = ["latex-math-note", "latex-english-article", "typst-zju-lab"]


def invoke(**kwargs):
    return json.loads(server.process_media(action="template", **kwargs))


def test_original_assets_match_recovery_provenance():
    root = REPO / "videonote_mcp/templates"
    provenance = json.loads((root / "provenance.json").read_text())
    assert len(provenance["files"]) == 26
    for name, sha256 in provenance["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == sha256
    assert not (REPO / "skills").exists()
    assert not (REPO / "commands").exists()


def test_catalog_and_offline_guide_are_available_without_task():
    result = invoke()
    assert [t["id"] for t in result["templates"]] == IDS
    assert result["guide_resource"] == "videonote://help/export"
    guide = invoke(template_file="GUIDE.md")["content"]
    assert "xelatex -no-shell-escape" in guide
    assert "typst compile" in guide
    assert "样例 PDF" in guide
    assert "KaiTi" in guide
    assert "fontset=fandol" in guide
    assert "GUIDE.md" in guide
    assert FORMATS == ("srt", "vtt", "json")


@pytest.mark.parametrize("tid", IDS)
def test_selected_template_reads_source_and_copies_assets_and_license(tmp_path, monkeypatch, tid):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.delenv("VIDEONOTE_ALLOW_EXTERNAL_PATHS", raising=False)
    manifest = invoke(template_id=tid)
    assert not manifest["copied"]
    name = manifest["entrypoint"]
    text = invoke(template_id=tid, template_file=name)
    assert text["content"]
    assert all(item["uri"].startswith("file://") for item in text["files"])
    out = tmp_path / "export with spaces" / tid
    result = invoke(template_id=tid, out_dir=out.as_uri())
    assert result["copied"] and result["output_dir"] == str(out)
    assert (out / name).read_text() == text["content"]
    license_name = "LICENSE" if tid.startswith("typst") else "LICENSE-LPPL-1.3c.txt"
    assert (out / license_name).read_text()
    if tid.startswith("latex"):
        assert "Gua927" in (out / "NOTICE.md").read_text()
    else:
        assert (out / "img/ZJU-logo.png").is_file()
        assert (out / "imports.typ").is_file()


@pytest.mark.parametrize("kwargs", [
    {"template_id": "../../"},
    {"template_id": "unknown"},
    {"template_file": "main.tex"},
    {"template_id": "latex-math-note", "template_file": "../NOTICE.md"},
    {"template_id": "latex-math-note", "template_file": "/etc/passwd"},
    {"template_id": "latex-math-note", "template_file": "..\\main.tex"},
    {"template_id": "latex-math-note", "template_file": "image.png"},
    {"template_id": "latex-math-note", "template_file": "main.pdf"},
    {"hf_token": "fake-sensitive-input"},
])
def test_unknown_paths_binary_as_text_and_credentials_rejected(kwargs):
    with pytest.raises(ValueError):
        invoke(**kwargs)


def test_copy_requires_id_and_cannot_mix_read_and_copy(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    out = tmp_path / "out"
    for args in ({}, {"template_id": IDS[0], "template_file": "main.tex"}):
        with pytest.raises(ValueError):
            invoke(out_dir=str(out), **args)
        assert not out.exists()


@pytest.mark.parametrize("has_file", [False, True])
def test_copy_never_overwrites_even_empty_directory(tmp_path, monkeypatch, has_file):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    out = tmp_path / "existing"
    out.mkdir()
    if has_file:
        (out / "main.tex").write_text("user work")
    with pytest.raises(FileExistsError):
        invoke(template_id=IDS[0], out_dir=str(out))
    assert sorted(p.name for p in out.iterdir()) == (["main.tex"] if has_file else [])
    if has_file:
        assert (out / "main.tex").read_text() == "user work"


def test_copy_rejects_external_paths_and_symlink_escape(tmp_path, monkeypatch):
    data = tmp_path / "data"
    data.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(server, "DATA_DIR", data)
    monkeypatch.delenv("VIDEONOTE_ALLOW_EXTERNAL_PATHS", raising=False)
    (data / "link").symlink_to(outside, target_is_directory=True)
    for out in (outside / "new", data / "link/new"):
        with pytest.raises(ValueError, match="数据目录"):
            invoke(template_id=IDS[0], out_dir=str(out))
    assert not (outside / "new").exists()
    monkeypatch.setenv("VIDEONOTE_ALLOW_EXTERNAL_PATHS", "1")
    assert invoke(template_id=IDS[0], out_dir=str(outside / "allowed"))["copied"]


def test_even_opt_in_cannot_write_into_packaged_templates(monkeypatch):
    monkeypatch.setenv("VIDEONOTE_ALLOW_EXTERNAL_PATHS", "1")
    with pytest.raises(ValueError, match="安装包"):
        invoke(template_id=IDS[0], out_dir=str(templates_module._ROOT / "do-not-create"))
    assert not (templates_module._ROOT / "do-not-create").exists()


def test_symlink_in_template_cannot_expose_host_files(tmp_path, monkeypatch):
    root = tmp_path / "templates"
    template = root / "typst/zju-lab"
    template.mkdir(parents=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("fake secret")
    (template / "outside.txt").symlink_to(secret)
    monkeypatch.setattr(templates_module, "_ROOT", root)
    with pytest.raises(ValueError, match="符号链接"):
        invoke(template_id="typst-zju-lab")


def test_real_stdio_discovery_and_template_workflow():
    result = subprocess.run([sys.executable, str(REPO / "tests/mcp_stdio_smoke.py")],
                            cwd=REPO, capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "3 offline templates" in result.stdout
