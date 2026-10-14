#!/usr/bin/env bash
# 源码安装入口；跨平台客户端检测与 ASR 推荐统一由 Python install 命令负责。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_DIR"

echo "==> 安装 Python 依赖"
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

if ! command -v uv >/dev/null 2>&1; then
  echo "已安装源码依赖；自动客户端安装需要 uv。请先安装 uv，或手动配置 MCP。"
  echo "MCP command: $BIN"
  echo "配置向导: $BIN setup"
  exit 0
fi

exec "$BIN" install "$@"
