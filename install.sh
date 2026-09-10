#!/usr/bin/env bash
# VideoNote-Mcp 一键安装：创建 venv → 安装 → 直接注册 MCP → CLI 配置
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "==> 1/3 安装 Python 依赖"
if command -v uv >/dev/null 2>&1; then
  # --no-dev：生产安装不带 pytest/ruff（docs 审计 H 组）
  uv sync --no-dev
else
  echo "（未找到 uv，改用 python3 venv + pip）"
  python3 -m venv .venv
  ./.venv/bin/pip install -e .
fi

BIN="$REPO_DIR/.venv/bin/videonote"
if [ ! -x "$BIN" ]; then
  BIN="$(command -v videonote || true)"
fi
if [ ! -x "$BIN" ]; then
  echo "安装失败：找不到 videonote 可执行文件" >&2
  exit 1
fi

echo "==> 2/3 注册 MCP"
print_mcp_config() {
  "$REPO_DIR/.venv/bin/python" - "$BIN" <<'PYJSON'
import json
import sys
print(json.dumps({"mcpServers": {"videonote": {
    "type": "stdio", "command": sys.argv[1], "args": []
}}}, ensure_ascii=False))
PYJSON
}
if command -v claude >/dev/null 2>&1; then
  # 保留旧安装的配置隔离：用户级同名 MCP 会遮蔽插件的 userConfig env。
  # 只查询，不更新/卸载插件，也不自动改写已有配置。
  if claude plugin list 2>/dev/null | grep -q "videonote"; then
    echo "检测到 videonote 插件，跳过自动注册 MCP，避免遮蔽插件配置。"
    echo "若要改用源码 MCP，请先手动停用/移除旧入口，再核对以下配置："
    print_mcp_config
  elif claude mcp add --scope user videonote -- "$BIN"; then
    echo "已注册用户级 videonote MCP"
  else
    echo "MCP 注册未成功（可能已有同名配置）。未自动移除或覆盖；请核对后手动更新：" >&2
    print_mcp_config
  fi
else
  echo "未找到 claude CLI。请把下面的配置加入你的 MCP 客户端："
  print_mcp_config
fi

echo ""
echo "==> 3/3 初始化配置（语音转写引擎 + 可选 LLM 供应商）"
if [ -t 0 ]; then
  "$BIN" setup
else
  echo "（非交互终端，跳过。可稍后执行：$BIN setup）"
fi

echo ""
echo "==> 安装完成。验证："
echo "  重启或重连 MCP 后调用 health_check（无需额外工作流文件）"
echo "  $BIN setup             # 调整转写 / 可选 LLM / 平台登录配置"
echo "  health_check           # ffmpeg / db / whisper 状态"
echo "  claude mcp list        # 查看已注册的 MCP"
