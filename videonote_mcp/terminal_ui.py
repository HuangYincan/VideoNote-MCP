"""Shared terminal prompts; imports stay lightweight for installer previews."""
import sys

BACK_KEYS = {"interrupt": [{"key": "left"}, {"key": "c-c"}]}


def header(title: str, section: str = "", *, clear: bool = True) -> None:
    if clear:
        print("\033[2J\033[H", end="", file=sys.stdout)
    print(f"\033[1;36m⚙  VideoNote {title}\033[0m  "
          "\033[2m↑↓ 选择 · 空格多选 · 回车确认 · ← 返回/取消 · Ctrl-C 取消\033[0m", file=sys.stdout)
    if section:
        print(f"\033[1;33m▶ {section}\033[0m", file=sys.stdout)
    print("", file=sys.stdout)


def prompts():
    try:
        from InquirerPy import inquirer
    except ImportError:
        return None
    return inquirer


def checkbox(message: str, choices: list[dict], *, inq=None) -> list:
    inq = inq or prompts()
    if inq:
        return inq.checkbox(message=message, choices=choices, keybindings=BACK_KEYS,
                            instruction="↑↓ 移动 · 空格勾选 · 回车应用 · ← 取消",
                            raise_keyboard_interrupt=True).execute()
    for index, choice in enumerate(choices, 1):
        mark = "✓" if choice.get("enabled") else " "
        print(f"  {index}) [{mark}] {choice['name']}", file=sys.stdout)
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
        print("请输入列表中的序号。", file=sys.stdout)


def select(message: str, choices: list[dict], default=None, *, inq=None):
    inq = inq or prompts()
    if inq:
        return inq.select(message=message, choices=choices, default=default,
                          keybindings=BACK_KEYS, raise_keyboard_interrupt=True).execute()
    for index, choice in enumerate(choices, 1):
        print(f"  {index}) {choice['name']}", file=sys.stdout)
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
