#!/usr/bin/env bash
# VideoNote-Mcp 一键安装：创建 venv → 安装 → 注册 MCP → 链接 Skill
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "==> 1/4 安装 Python 依赖"
if command -v uv >/dev/null 2>&1; then
  uv sync
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

echo "==> 2/3 安装 Skill + 注册 MCP"
HAVE_UV="0"
command -v uv >/dev/null 2>&1 && HAVE_UV="1"
PLUGIN_OK="0"
# 优先走 marketplace：Skill + MCP server 一起装，插件自带 MCP（带 userConfig env）。
# 不要同时做用户级 `claude mcp add`——同名的用户级条目 env 为空，会遮蔽插件 server 的 env。
if [ "$HAVE_UV" = "1" ] && command -v claude >/dev/null 2>&1; then
  if claude plugin marketplace add HuangYincan/VideoNote-MCP >/dev/null 2>&1 \
     && claude plugin install videonote@videonote >/dev/null 2>&1; then
    echo "Skill + MCP 已通过 marketplace 安装（videonote@videonote）"
    PLUGIN_OK="1"
  fi
fi
if [ "$PLUGIN_OK" != "1" ]; then
  # 回退：无 uv 或 marketplace 失败 → 用户级 MCP + 本地 Skill 链接
  if command -v claude >/dev/null 2>&1; then
    claude mcp add videonote -- "$BIN" && echo "已注册：claude mcp add videonote -- $BIN"
  else
    echo "未找到 claude CLI。请手动把下面的配置加入你的 MCP 配置："
    echo "  { \"mcpServers\": { \"videonote\": { \"command\": \"$BIN\" } } }"
  fi
  mkdir -p "$HOME/.claude/skills"
  ln -sfn "$REPO_DIR/skills/videonote" "$HOME/.claude/skills/videonote"
  echo "已本地链接：$HOME/.claude/skills/videonote"
fi

echo ""
echo "==> 3/3 初始化配置（LLM 供应商 + 语音转写引擎）"
if [ -t 0 ]; then
  "$BIN" setup
else
  echo "（非交互终端，跳过。可稍后执行：$BIN setup）"
fi

echo ""
echo "==> 安装完成。验证："
echo "  重启 Claude Code 会话后跑 /videonote-setup（体检 / 填 key / 转写）"
echo "  $BIN providers list    # 确认 LLM key 已填"
echo "  health_check           # ffmpeg / db / whisper 状态"
echo "  （marketplace 方式的 MCP 由插件提供，`claude mcp list` 不会列出；仅回退安装模式会显示）"
