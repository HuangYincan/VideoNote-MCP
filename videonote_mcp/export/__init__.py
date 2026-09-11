"""多格式导出 —— 只做**确定性机械格式**（时间轴纯渲染，免 token、零错误）。

与输出层的分工：
  - 这里只产出 SRT / VTT / JSON 三种**无歧义**格式，结果可离线核对、可复用；
  - 思维导图 / 闪卡 / LaTeX / typst / 用户自定义模板等**创意格式**不在这里——
    由当前对话 Agent 基于 MD 底稿转换；templates 模块提供独立模板的读取/复制，不执行编译；无需 Skills。

核心入口：`export_transcript(source, formats, out_dir)`（见 exporter.py），返回
`{fmt: file://绝对路径}` 供 Agent 直接 Read。

import 纪律：`srt/vtt/json` 渲染器只依赖纯 dataclass，可无副作用导入；
`exporter` 会触发 `app.services.note`（加载 vendored 流水线），因此**惰性导入**，
避免 `import videonote_mcp.export` 时提前加载 app.* 并产生 stdout 噪音。
"""
from typing import Dict, List, Optional, Union

from .srt import to_srt
from .vtt import to_vtt

# 支持的格式注册表：确定性、纯渲染、零依赖。
FORMATS = ("srt", "vtt", "json")


def export_transcript(
    source,
    formats: Optional[List[str]] = None,
    out_dir: Optional[Union[str, object]] = None,
    task_id: Optional[str] = None,
) -> Dict[str, str]:
    """惰性入口：避免模块顶层加载 app.*（见模块 docstring）。"""
    from .exporter import export_transcript as _impl

    return _impl(source, formats=formats, out_dir=out_dir, task_id=task_id)


__all__ = ["FORMATS", "export_transcript", "to_srt", "to_vtt"]
