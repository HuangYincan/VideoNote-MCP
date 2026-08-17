# VideoNote-Mcp —— MCP server（stdio）
# 用途：Docker 部署 / Glama (glama.ai/mcp/servers) 校验（提交时把本文件内容粘到 Glama 的 Dockerfile 栏）
# 启动：无参数运行 = MCP stdio server（客户端 attach stdin/stdout）
FROM python:3.12-slim

# FFmpeg（音频/视频处理必需）
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 uv
RUN pip install --no-cache-dir uv

# 复制项目（源码必须与依赖一起就位，uv sync 会构建并安装本包，wheel 含 app/**）
COPY pyproject.toml uv.lock README.md ./
COPY videonote_mcp/ videonote_mcp/
COPY app/ app/

# 按锁文件安装依赖 + 本项目（--no-dev：不带可选依赖）
RUN uv sync --no-dev --frozen

# MCP server（无参数 = stdio 模式）
CMD ["uv", "run", "videonote"]
