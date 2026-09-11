"""独立导出模板：目录/单文件读取与显式复制；不下载、不编译、不加载 app.*。"""
import mimetypes
import shutil
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1] / "templates"
_TEMPLATES = {
    "latex-math-note": {
        "name": "Math Note", "directory": "latex/Math Note", "format": "latex",
        "entrypoint": "main.tex", "compiler": "xelatex", "license": "LPPL-1.3c",
        "description": "数学/理工科笔记，中英文 MathNote / MathNoteCN 文档类。",
    },
    "latex-english-article": {
        "name": "English Article", "directory": "latex/English Article", "format": "latex",
        "entrypoint": "main.tex", "compiler": "xelatex", "license": "LPPL-1.3c",
        "description": "英文文稿/演讲大纲，article 文档类。",
    },
    "typst-zju-lab": {
        "name": "zju-lab", "directory": "typst/zju-lab", "format": "typst",
        "entrypoint": "example.typ", "compiler": "typst", "license": "MIT",
        "description": "理工科笔记/实验报告，含 ZJU 标识；首次编译 @preview 依赖可能需联网。",
    },
}
_TEXT_SUFFIXES = {".tex", ".cls", ".typ", ".bib", ".md", ".txt"}


def export_guide() -> str:
    return (_ROOT / "README.md").read_text(encoding="utf-8")


def template_catalog() -> dict:
    return {
        "ok": True,
        "templates": [{"id": key, **{k: v for k, v in spec.items() if k != "directory"}}
                      for key, spec in _TEMPLATES.items()],
        "guide_resource": "videonote://help/export",
        "next_steps": "process_media(action='template', template_id=...) 列配套文件；template_file='README.md' 读文件；out_dir 复制完整模板。template_file='GUIDE.md' 读离线导出指南（无需 template_id）。",
        "compilers": {name: shutil.which(name) is not None for name in ("xelatex", "typst")},
        "pdf_policy": "编译器存在不代表字体/宏包齐全；此操作不编译 PDF。模板中的样例 PDF 不是用户产物。",
    }


def _files(template_id: str) -> tuple[dict, dict[str, Path]]:
    if template_id not in _TEMPLATES:
        raise ValueError(f"未知 template_id: {template_id!r}；先 process_media(action='template') 列模板")
    spec = _TEMPLATES[template_id]
    root = _ROOT / spec["directory"]
    if not root.is_dir():
        raise FileNotFoundError("安装包缺少模板目录，请核对安装版本/包内容")
    files = {p.relative_to(root).as_posix(): p for p in sorted(root.rglob("*")) if p.is_file()}
    if spec["format"] == "latex":
        files.update({name: _ROOT / "latex" / name for name in ("NOTICE.md", "LICENSE-LPPL-1.3c.txt")})
    # 不允许经包内软链读取任意主机文件；模板名/文件名从目录白名单精确匹配。
    for source in files.values():
        if source.is_symlink() or not source.resolve().is_relative_to(_ROOT.resolve()):
            raise ValueError("模板包含不安全的符号链接")
    return spec, files


def _is_text(path: Path) -> bool:
    return path.suffix in _TEXT_SUFFIXES or path.name == "LICENSE"


def access_template(template_id: str = "", template_file: str = "", out_dir: Path | None = None) -> dict:
    """out_dir 是最终新目录，已有目录一律拒绝；调用者负责其写入权限/数据目录门禁。"""
    if not template_id:
        if out_dir is not None:
            raise ValueError("复制模板需要 template_id")
        if template_file == "GUIDE.md":
            return {"ok": True, "file": "GUIDE.md", "content": export_guide()}
        if template_file:
            raise ValueError("读取模板文件需要 template_id；通用指南用 template_file='GUIDE.md'")
        return template_catalog()
    spec, sources = _files(template_id)
    if template_file and out_dir is not None:
        raise ValueError("template_file 读取与 out_dir 复制请分开调用")
    if template_file and template_file not in sources:
        raise ValueError("template_file 不在该模板文件清单中；不接受任意路径")
    if out_dir is not None:
        out_dir = out_dir.resolve()
        # 明确拒绝覆盖，包括已有空目录；不用 shell，不在安装目录原地修改模板。
        if out_dir.is_relative_to(_ROOT.resolve()):
            raise ValueError("不能复制到安装包的模板目录；请选择独立的导出目录")
        out_dir.mkdir(parents=True, exist_ok=False)
        for name, source in sources.items():
            destination = out_dir / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as src, destination.open("xb") as dst:
                shutil.copyfileobj(src, dst)
        sources = {name: out_dir / name for name in sources}
    result = {
        "ok": True, "template_id": template_id, "name": spec["name"],
        "entrypoint": spec["entrypoint"], "license": spec["license"],
        "copied": out_dir is not None, "output_dir": str(out_dir) if out_dir else None,
        "files": [{"name": name, "uri": source.resolve().as_uri(), "text": _is_text(source),
                   "mime_type": mimetypes.guess_type(name)[0] or "application/octet-stream"}
                  for name, source in sources.items()],
        "next_steps": "先读 README.md 与入口源码；在副本中把样例替换为用户底稿，按 GUIDE.md 编译。未编译成功只交付源码，不交付模板样例 PDF。",
    }
    if template_file:
        source = sources[template_file]
        if not _is_text(source):
            raise ValueError("二进制配套文件请通过 files[].uri 读取或用 out_dir 复制，不以文本返回")
        result.update({"file": template_file, "content": source.read_text(encoding="utf-8")})
    return result
