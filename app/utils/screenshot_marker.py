import re
from typing import List, Tuple


def extract_screenshot_timestamps(markdown: str) -> List[Tuple[str, int]]:
    # group(1) 含前导+闭合星号（LLM 可能输出 *Screenshot-[01:23]*）——
    # marker 必须是完整原文，否则 replace 后残留尾部 *（#122 B5）
    pattern = r"(\*?Screenshot-(?:\[(\d+):(\d+)\]|(\d+):(\d+))\*?)"
    results: List[Tuple[str, int]] = []
    for match in re.finditer(pattern, markdown):
        mm = match.group(2) or match.group(4)
        ss = match.group(3) or match.group(5)
        total_seconds = int(mm) * 60 + int(ss)
        results.append((match.group(1), total_seconds))
    return results
