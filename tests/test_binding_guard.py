"""#32 同族绑定回归的结构性守卫（docs/05 #139 批次 1）。

issue #32 根因：悬空 @staticmethod 装饰器（被注释分割）把实例方法误绑为静态方法，
运行时 self.method() 抛 TypeError；700+ 测试全绿是因为 mock 整体打桩掩盖了绑定差异。

本模块把 #32 的签名固化为 AST 编译期断言——任何同类回归（装饰器误加/误删/注释分割、
staticmethod 误用实例属性）在测试阶段即红，不依赖人工排查：

1. 装饰器与函数/类定义之间不得被注释或代码行分割（空行放行——合法风格）
2. @staticmethod 方法体不得引用 self.<attr> 实例属性
3. @staticmethod 首参不得是 self/cls；@classmethod 首参必须是 cls
4. 绑定敏感方法不得被 mock.patch.object 整体打桩（清单随发现扩充，见 B1）
"""
import ast
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("app", "videonote_mcp")

# 绑定敏感方法：生产经 self.method() 真实调用、测试若整体打桩会掩盖绑定回归（#32 教训）。
# 新增排查发现的方法往这里加；对应 mock 应下沉到方法体内的依赖（见 docs/05 #139 B1/C1）。
BINDING_SENSITIVE_METHODS = {"_init_transcriber"}


def _py_files():
    for d in SCAN_DIRS:
        yield from sorted((REPO_ROOT / d).rglob("*.py"))


def _decorator_names(node) -> set:
    return {d.id for d in node.decorator_list if isinstance(d, ast.Name)}


class DecoratorGapGuardTest(unittest.TestCase):
    """守卫 1：装饰器与函数/类定义之间不得被注释或代码行分割（#32 精确签名）。"""

    def test_no_comment_between_last_decorator_and_def(self):
        offenders = []
        for p in _py_files():
            src = p.read_text(encoding="utf-8")
            tree = ast.parse(src)
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
            tree = ast.parse(p.read_text(encoding="utf-8"))
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
            tree = ast.parse(p.read_text(encoding="utf-8"))
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
