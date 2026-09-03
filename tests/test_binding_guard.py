"""#32 同族绑定回归的结构性守卫（docs/05 #139 批次 1/4）。

issue #32 根因：悬空 @staticmethod 装饰器（被注释分割）把实例方法误绑为静态方法，
运行时 self.method() 抛 TypeError；700+ 测试全绿是因为 mock 整体打桩掩盖了绑定差异。

本模块把 #32 的签名固化为 AST/descriptor 编译期断言——任何同类回归（装饰器误加/误删/
注释分割、staticmethod 误用实例属性）在测试阶段即红，不依赖人工排查：

1. 装饰器与函数/类定义之间不得被注释或代码行分割（空行放行——合法风格）
2. @staticmethod 方法体不得引用 self.<attr> 实例属性
3. @staticmethod 首参不得是 self/cls；@classmethod 首参必须是 cls
4. 绑定敏感方法不得被 mock.patch.object 整体打桩（清单随发现扩充，见 B1）
5. 绑定敏感方法必须是普通实例方法（descriptor 断言）——对保留方法级 mock 的
   （Douyin/VideoReader/_load_checkpoint/__upload_part 等：下沉成本高或属方法本体
   单元隔离），用 inspect.getattr_static 兜底：误加 @staticmethod/@classmethod 即红
"""
import ast
import importlib
import inspect
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app", "videonote_mcp")

# 守卫 4 白名单：已迁移到依赖层打桩、今后不得再整体 mock 的绑定敏感方法（#32 教训）。
# 新增排查发现的方法往这里加；对应 mock 应下沉到方法体内的依赖（见 docs/05 #139 B1/C1）。
BINDING_SENSITIVE_METHODS = {
    "_init_transcriber", "_get_downloader", "_update_status", "_transcribe_audio",
    "_do_create", "_chat_completion_create", "_upload", "_create_task", "_query_result",
}

# 守卫 5 清单：所有绑定敏感方法（含保留方法级 mock 的）——descriptor 类型断言兜底绑定。
# 类路径 → 方法名列表。
BINDING_SENSITIVE_CLASSES = {
    "app.services.note.NoteGenerator": [
        "_init_transcriber", "_get_downloader", "_update_status", "_transcribe_audio",
    ],
    "app.gpt.universal_gpt.UniversalGPT": [
        "_do_create", "_chat_completion_create", "_load_checkpoint",
    ],
    "app.transcriber.bcut.BcutTranscriber": [
        "_upload", "_create_task", "_query_result",
        "_BcutTranscriber__upload_part", "_BcutTranscriber__commit_upload",
    ],
    "app.downloaders.douyin_downloader.DouyinDownloader": [
        "extract_video_id", "fetch_video_info",
    ],
    "app.utils.video_reader.VideoReader": ["extract_frames"],
    "app.services.cookie_manager.CookieConfigManager": ["get"],
}


def _py_files():
    for d in SCAN_DIRS:
        yield from sorted((REPO_ROOT / d).rglob("*.py"))


def _decorator_names(node) -> set:
    return {d.id for d in node.decorator_list if isinstance(d, ast.Name)}


def _parse(src: str):
    """ast.parse 包装：文件内 `\\d` 等正则字符串字面量触发 SyntaxWarning，压掉噪音。"""
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(src)


class DecoratorGapGuardTest(unittest.TestCase):
    """守卫 1：装饰器与函数/类定义之间不得被注释或代码行分割（#32 精确签名）。"""

    def test_no_comment_between_last_decorator_and_def(self):
        offenders = []
        for p in _py_files():
            src = p.read_text(encoding="utf-8")
            tree = _parse(src)
            lines = src.splitlines()
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if not node.decorator_list:
                    continue
                dec = node.decorator_list[-1]
                dec_end = dec.end_lineno or dec.lineno
                gap = lines[dec_end:node.lineno - 1]
                # 空行放行（合法风格）；注释/代码行都是 #32 签名（注释不打断装饰器绑定）
                bad = [i for i, l in enumerate(gap, dec_end + 1) if l.strip()]
                if bad:
                    offenders.append(
                        f"{p}:{node.lineno} {type(node).__name__.lower()} {node.name} "
                        f"decorator@{dec_end} 间隔行={bad}"
                    )
        self.assertEqual(
            offenders, [],
            "装饰器与定义之间被注释/代码分割（#32 同族）：\n" + "\n".join(offenders),
        )


class StaticBindingGuardTest(unittest.TestCase):
    """守卫 2/3：@staticmethod 不得用实例属性/首参不得 self·cls；@classmethod 首参须 cls。"""

    def test_staticmethod_and_classmethod_binding_sane(self):
        offenders = []
        for p in _py_files():
            tree = _parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decs = _decorator_names(node)
                first = node.args.args[0].arg if node.args.args else None
                uses_self = any(
                    isinstance(n, ast.Attribute)
                    and isinstance(n.value, ast.Name) and n.value.id == "self"
                    for n in ast.walk(node)
                )
                if "staticmethod" in decs:
                    if first in ("self", "cls"):
                        offenders.append(
                            f"{p}:{node.lineno} staticmethod 首参是 {first!r}（#32 签名） def {node.name}"
                        )
                    elif uses_self:
                        offenders.append(
                            f"{p}:{node.lineno} staticmethod 方法体引用 self 实例属性 def {node.name}"
                        )
                if "classmethod" in decs and first != "cls":
                    offenders.append(
                        f"{p}:{node.lineno} classmethod 首参是 {first!r}（应为 cls） def {node.name}"
                    )
        self.assertEqual(
            offenders, [],
            "静态/类方法绑定异常：\n" + "\n".join(offenders),
        )


class MockTargetGuardTest(unittest.TestCase):
    """守卫 4：绑定敏感方法不得被 mock.patch.object 整体打桩（掩盖绑定回归，#32 教训）。"""

    def test_binding_sensitive_methods_not_whole_mocked(self):
        offenders = []
        for p in sorted((REPO_ROOT / "tests").rglob("test_*.py")):
            tree = _parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                # mock.patch.object(obj, "NAME", ...) —— 第二位置参数为方法名
                if not (isinstance(fn, ast.Attribute) and fn.attr == "object"):
                    continue
                if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                    target = node.args[1].value
                    if target in BINDING_SENSITIVE_METHODS:
                        offenders.append(f"{p}:{node.lineno} mock.patch.object(..., {target!r})")
        self.assertEqual(
            offenders, [],
            "绑定敏感方法被整体 mock（应下沉到方法体内依赖，见 docs/05 #139）：\n"
            + "\n".join(offenders),
        )


class BindingDescriptorGuardTest(unittest.TestCase):
    """守卫 5：绑定敏感方法必须是普通实例方法（descriptor 断言）。"""

    def test_binding_sensitive_methods_are_instance_methods(self):
        offenders = []
        for dotted, methods in BINDING_SENSITIVE_CLASSES.items():
            mod_name, cls_name = dotted.rsplit(".", 1)
            importlib.import_module(mod_name)
            cls = getattr(sys.modules[mod_name], cls_name)
            for m in methods:
                desc = inspect.getattr_static(cls, m)
                if isinstance(desc, (staticmethod, classmethod)):
                    offenders.append(f"{dotted}.{m} → {type(desc).__name__}")
        self.assertEqual(
            offenders, [],
            "绑定敏感方法被静态/类方法绑定（#32 同族）：\n" + "\n".join(offenders),
        )


class StaticMethodCallSiteGuardTest(unittest.TestCase):
    """守卫 6：@staticmethod 必须经类名调用，不得经 self.<name>()（#139 C3 约定）。

    经 self 调用静态方法当前合法（首参对得上），但改绑（加 self 参数/改签名）时
    调用点与 mock 不对称即崩——统一类名调用让绑定错误在定义处即红。
    abogus.py 例外：JS 移植风格内部大量 self 调用 static/classmethod（约 40 处），
    绑定由真实调用测试兜底，白名单处理（docs/05 #139 C3）。
    """

    def test_staticmethods_called_via_class_not_self(self):
        offenders = []
        for p in _py_files():
            if p.name == "abogus.py":
                continue
            tree = _parse(p.read_text(encoding="utf-8"))
            for cls_node in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                # 本类定义的 staticmethod 名
                static_names = {
                    n.name for n in cls_node.body
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and "staticmethod" in _decorator_names(n)
                }
                if not static_names:
                    continue
                for fn in (n for n in ast.walk(cls_node) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
                    for call in ast.walk(fn):
                        if not isinstance(call, ast.Call):
                            continue
                        func = call.func
                        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                                and func.value.id == "self" and func.attr in static_names):
                            offenders.append(f"{p}:{call.lineno} self.{func.attr}()（应类名调用）")
        self.assertEqual(
            offenders, [],
            "@staticmethod 经 self 调用（应改类名调用，见 docs/05 #139 C3）：\n"
            + "\n".join(offenders),
        )
