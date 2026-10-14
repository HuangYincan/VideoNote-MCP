"""Shared terminal prompts; imports stay lightweight for installer previews."""
import os
import sys

BACK_KEYS = {"interrupt": [{"key": "left"}, {"key": "c-c"}]}
COLORS = {"title": "1;36", "section": "1;33", "label": "36", "value": "1;32",
          "info": "36", "success": "1;32", "warning": "1;33", "error": "1;31",
          "muted": "2", "reset": "0"}
PROMPT_THEME = {
    "": "ansicyan", "questionmark": "ansimagenta bold", "answermark": "ansigreen bold",
    "question": "ansimagenta bold", "answered_question": "ansicyan",
    "answer": "ansigreen bold", "input": "ansigreen", "pointer": "bg:ansiyellow ansiblack bold",
    "checkbox": "ansigreen bold", "marker": "ansigreen bold", "instruction": "ansibrightblack",
    "long_instruction": "ansibrightblack", "separator": "ansibrightblack", "skipped": "ansibrightblack",
    "validator": "ansired", "fuzzy_prompt": "ansimagenta", "fuzzy_info": "ansicyan",
    "fuzzy_border": "ansicyan", "fuzzy_match": "ansiyellow bold", "spinner_pattern": "ansiyellow",
    "spinner_text": "ansicyan",
}


def _windows_colors(stream) -> bool:
    try:
        import ctypes
        import msvcrt

        handle = msvcrt.get_osfhandle(stream.fileno())
        kernel = ctypes.windll.kernel32
        mode = ctypes.c_ulong()
        kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        kernel.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        return bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))
                    and kernel.SetConsoleMode(handle, mode.value | 0x0004))
    except (ImportError, AttributeError, OSError, ValueError):
        return False


def color_enabled(stream=None) -> bool:
    stream = sys.stdout if stream is None else stream
    if "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return False
    if not getattr(stream, "isatty", lambda: False)():
        return False
    return os.name != "nt" or _windows_colors(stream)


def ansi(role: str, *, stream=None) -> str:
    return f"\033[{COLORS[role]}m" if color_enabled(stream) else ""


def paint(text, role="info", *, stream=None) -> str:
    code = ansi(role, stream=stream)
    return f"{code}{text}\033[0m" if code else str(text)


def message(text, role="info", *, file=None, **kwargs) -> None:
    file = sys.stdout if file is None else file
    print(paint(text, role, stream=file), file=file, **kwargs)


def field(label, value, note="", *, role="value", indent="  ") -> None:
    print(indent + paint(label + "：", "label") + paint(value, role) + paint(note, "muted"), file=sys.stdout)


def prompt_style():
    from InquirerPy import get_style

    enabled = color_enabled()
    return get_style({key: value if enabled else "" for key, value in PROMPT_THEME.items()})


class _ThemedPrompts:
    """Apply one theme to all prompt types without modifying choice names/values."""

    def __init__(self, inquirer):
        self.inquirer = inquirer

    def __getattr__(self, name):
        factory = getattr(self.inquirer, name)

        def themed(*args, **kwargs):
            kwargs.setdefault("style", prompt_style())
            return factory(*args, **kwargs)

        return themed


def header(title: str, section: str = "", *, clear: bool = True) -> None:
    if clear and color_enabled():
        print("\033[2J\033[H", end="", file=sys.stdout)
    message(f"⚙  VideoNote {title}", "title")
    message("   ↑↓ 选择 · 空格多选 · 回车确认 · ← 返回/取消 · Ctrl-C 取消", "muted")
    if section:
        message(f"▶ {section}", "section")
    print("", file=sys.stdout)


def prompts():
    try:
        from InquirerPy import inquirer
    except ImportError:
        return None
    return _ThemedPrompts(inquirer)


def checkbox(message: str, choices: list[dict], *, inq=None) -> list:
    inq = inq or prompts()
    if inq:
        return inq.checkbox(message=message, choices=choices, keybindings=BACK_KEYS,
                            instruction="↑↓ 移动 · 空格勾选 · 回车应用 · ← 取消",
                            raise_keyboard_interrupt=True).execute()
    for index, choice in enumerate(choices, 1):
        mark = "✓" if choice.get("enabled") else " "
        print(paint(f"  {index}) [{mark}] {choice['name']}", "success" if choice.get("enabled") else "info"), file=sys.stdout)
    while True:
        answer = input(message + "（逗号分隔；留空保留当前，0 清空，q 取消）: ").strip()
        if answer.lower() == "q":
            raise KeyboardInterrupt
        if not answer:
            return [item["value"] for item in choices if item.get("enabled")]
        if answer == "0":
            return []
        try:
            indexes = list(dict.fromkeys(int(item.strip()) - 1 for item in answer.split(",")))
            if all(0 <= index < len(choices) for index in indexes):
                return [choices[index]["value"] for index in indexes]
        except ValueError:
            pass
        print(paint("请输入列表中的序号。", "warning"), file=sys.stdout)


def select(message: str, choices: list[dict], default=None, *, inq=None):
    inq = inq or prompts()
    if inq:
        return inq.select(message=message, choices=choices, default=default,
                          keybindings=BACK_KEYS, raise_keyboard_interrupt=True).execute()
    for index, choice in enumerate(choices, 1):
        print(paint(f"  {index}) {choice['name']}", "success" if choice["value"] == default else "info"), file=sys.stdout)
    while True:
        answer = input(message + "（序号；留空使用默认，q 取消）: ").strip()
        if answer.lower() == "q":
            raise KeyboardInterrupt
        if not answer and default is not None:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]["value"]


def confirm(message: str, default: bool = False, *, inq=None) -> bool:
    inq = inq or prompts()
    if inq:
        return inq.confirm(message=message, default=default, keybindings=BACK_KEYS,
                           raise_keyboard_interrupt=True).execute()
    answer = input(message + (" [Y/n]: " if default else " [y/N]: ")).strip().lower()
    return default if not answer else answer in ("y", "yes")
