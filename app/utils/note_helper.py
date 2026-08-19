import re


def strip_media_markers(markdown: str) -> str:
    """剥掉 LLM 可能输出的截图/时间戳字面标记（*Screenshot-[01:23]*、Content-04:16 等）。

    只用于「无视频文件 / 无 video_id 可做后处理」的素材包总结路径：
    这些路径的 prompt 不要求输出标记，但 LLM 仍可能自行带上；替换函数
    （replace_content_markers / 截图插入）执行不了时靠它兜底，避免残留
    对读者无意义的字面量（#122 A5）。
    """
    pattern = re.compile(
        r"(?:\*?)(?:Screenshot|Content)-(?:\[\d+:\d+\]|\d+:\d+)\*?"
    )
    return pattern.sub("", markdown)


def prepend_source_link(markdown: str | None, source_url: str) -> str | None:
    """
    在笔记开头添加来源链接；若首个非空行已包含来源链接，则更新该行并避免重复。
    """
    if markdown is None:
        return None

    source = (source_url or "").strip()
    if not source:
        return markdown

    header = f"> 来源链接：{source}"
    lines = markdown.splitlines()
    first_non_empty_idx = None
    for idx, line in enumerate(lines):
        if line.strip():
            first_non_empty_idx = idx
            break

    if first_non_empty_idx is not None:
        first_line = lines[first_non_empty_idx].strip()
        if first_line.startswith("> 来源链接：") or first_line.startswith("来源链接："):
            lines[first_non_empty_idx] = header
            return "\n".join(lines)

    if markdown.strip():
        return f"{header}\n\n{markdown}"
    return header


def replace_content_markers(markdown: str, video_id: str, platform: str = 'bilibili') -> str:
    """
    替换 *Content-04:16*、Content-04:16 或 Content-[04:16] 为超链接，跳转到对应平台视频的时间位置
    """
    # 匹配三种形式：*Content-04:16*、Content-04:16、Content-[04:16]
    # 前导/闭合星号都消费（旧正则只吃前导星号 → *Content-[04:16]* 替换后残留尾部 *，#122 B5）
    pattern = r"(?:\*?)Content-(?:\[(\d+):(\d+)\]|(\d+):(\d+))\*?"

    def replacer(match):
        mm = match.group(1) or match.group(3)
        ss = match.group(2) or match.group(4)
        total_seconds = int(mm) * 60 + int(ss)

        if platform == 'bilibili':
            # 单 P（BV1xx）：`?t=`；多 P（BV1xx_pN → BV1xx?p=N）：`&t=`。
            # 旧实现无脑 `&t=` 会把时间参数拼进 path，单 P 跳转失效。
            base = video_id.replace("_p", "?p=")
            sep = "&" if "?" in base else "?"
            url = f"https://www.bilibili.com/video/{base}{sep}t={total_seconds}"
        elif platform == 'youtube':
            url = f"https://www.youtube.com/watch?v={video_id}&t={total_seconds}s"
        elif platform == 'douyin':
            url = f"https://www.douyin.com/video/{video_id}"
            return f"[原片 @ {mm}:{ss}]({url})"
        else:
            return f"({mm}:{ss})"

        return f"[原片 @ {mm}:{ss}]({url})"

    return re.sub(pattern, replacer, markdown)

