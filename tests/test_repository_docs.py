"""Current contributor/user docs must remain usable from a clean clone."""
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIVE_DOCS = (
    "README.md", "README_EN.md", "CONTRIBUTING.md", "VENDOR.md",
    "docs/00-新手上路.md", "docs/00-文档索引.md", "docs/01-目的与背景.md",
    "docs/02-架构设计.md", "docs/03-预期效果.md", "docs/04-使用手册.md",
)


@pytest.mark.parametrize("filename", LIVE_DOCS)
def test_local_documentation_links_exist(filename):
    document = ROOT / filename
    # Files only; anchor validation and external websites are deliberately out of scope.
    text = document.read_text(encoding="utf-8")
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = re.sub(r"`[^`]*`", "", text)  # literal examples are not navigable links
    for target in re.findall(r"\]\(([^\s)]+)\)", text):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc or not parsed.path:
            continue
        assert (document.parent / unquote(parsed.path)).exists(), (filename, target)


@pytest.mark.parametrize("filename", ["README.md", "README_EN.md", "docs/02-架构设计.md"])
def test_shared_modules_are_discoverable_in_live_docs(filename):
    text = (ROOT / filename).read_text(encoding="utf-8")
    for module in ("task_artifacts.py", "local_paths.py", "media_source.py"):
        assert module in text


def test_contributor_guidance_does_not_require_private_skills():
    for filename in ("docs/00-新手上路.md", "CONTRIBUTING.md"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        for private_dependency in ("videonote-dev", "videonote-audit", "videonote-mcp-gotchas", ".claude/skills/"):
            assert private_dependency not in text
    background = (ROOT / "docs/01-目的与背景.md").read_text(encoding="utf-8")
    for removed_tool in ("`add_provider`", "`list_providers`", "`set_transcriber`"):
        assert removed_tool not in background
