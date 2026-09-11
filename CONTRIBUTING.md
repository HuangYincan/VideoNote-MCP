# Contributing

## 分支策略：长期只保留 `dev` 和 `main`

- **`dev`**：集成分支，日常开发从最新 `dev` 出发，始终保持可运行。
- **`main`**：已交付主线，通过 `dev → main` 的 PR 接收经过验证的改动；合并不等于发版。
- **临时功能分支**：仅在开发与 PR 期间存在，合并后删除本地与远程分支，不留备份分支。恢复旧实现（包括已移除的 Skills）使用 Git commit 历史。
- **交付完成时**：本地和远程仅留 `dev`、`main`，两者同步到同一个最新提交；开发中的短暂分叉须有对应 PR，不长期放置。
- CI 必须通过，审批与合并权限以仓库当前保护规则为准；不得使用强制推送或绕过保护来“同步”分支。

### 开发与合并

先检查 `git status`，有未提交改动时妥善保留，不能覆盖他人的工作：

```bash
git fetch --prune origin
git switch dev
git merge --ff-only origin/dev
git switch -c fix/short-description
# 实现、测试、同步文档后提交，推送此临时分支，并开 PR → dev
```

临时分支命名：`feat/`（新功能）、`fix/`（修复）、`docs/`（文档）、`refactor/`（重构）、`chore/`（配置/依赖/构建），后接 kebab-case 描述。

1. 功能 PR → `dev`，等待 CI 的 Python 3.11 / 3.12 / 3.13 和汇总 `Smoke test` 全绿再合并。
2. 开 PR `dev → main`，同样通过 CI 后合并。
3. 确认期间没有新的独有 `dev` 提交，将两分支快进同步。若 `--ff-only` 失败，先分析分叉并走正常合并，**不要 reset 或 force push**。

```bash
git fetch --prune origin
git switch main
git merge --ff-only origin/main
git switch dev
git merge --ff-only origin/dev
git merge --ff-only origin/main
git push origin dev
```

`dev → main` 的 PR 不应使用“删除源分支”；不要为清理功能分支而无差别开启自动删分支，误删长期 `dev`。

### 清理与交付检查

- 清理前用 `git branch --merged origin/main` / `git log origin/main..分支名` 核对独有提交。若曾 squash/cherry-pick，再用 `git cherry origin/main 分支名` 检查等价补丁；存在 `+` 或无法确认时先保留并核查，不能直接强删。
- 用 `git worktree list` 检查占用。临时 worktree 必须干净、没有要保留的运行数据，才能移除；不要删除仍在工作的目录。
- 明确指定已合入的临时分支后，执行 `git push origin --delete 分支名`、`git branch -d 分支名`。不得使用通配符批量删除，也不要删除 `dev` / `main`。
- 已合入等价补丁但 `-d` 拒绝的旧分支，必须人工确认没有独有内容再单独清理。
- 最后检查 `git status --short`、`git branch --list` 和 `git ls-remote --heads origin`；工作区干净，本地/远程各只剩两分支，提交一致。
- `git rev-parse dev main origin/dev origin/main` 应输出四个相同的提交哈希。不保留仅为备份而创建的分支；未提交改动的临时 stash 在确认完整提交后才可清理，不动他人的 stash。

## 本地开发与验证

```bash
uv sync --frozen                 # 含开发依赖，锁文件必须匹配
uv run --frozen pytest -q
uv run --frozen ruff check --select F,I .
git diff --check
bash -n install.sh
```

CLI 的 `setup` / `login` 可能写入真实配置，请在本机终端按需运行，不在自动化测试或 Agent 对话中填写凭证。单测应隔离数据目录、mock 网络，不读取真实 Cookie、API key 或下载模型。

- 发布包 MCP 配置：[`examples/mcp.example.json`](examples/mcp.example.json)。
- 源码 MCP 配置：[`examples/mcp.source.example.json`](examples/mcp.source.example.json)。替换仓库绝对路径；登录 CLI 与 MCP 使用同一份源码和 `VIDEONOTE_DATA_DIR` / `VIDEONOTE_CONFIG_DIR`。
- MCP 冒烟用官方 SDK 的 stdio 客户端初始化并校验 **10 个工具的精确名单**，由 `.github/workflows/ci.yml` 调用 `tests/mcp_stdio_smoke.py`（名单在 `EXPECTED`）；同时验证来源说明、Resources、三套模板及许可证复制，只 import server 不能代替协议验证。
- 历史第三方模板按 `videonote_mcp/templates/provenance.json` 校验原字节；`.gitattributes` 仅对恢复的原件关闭换行转换/空白检查，并将 PDF 标为二进制。不要格式化原件，新指南和代码仍走正常检查。
- 分发相关改动还需 `uv build --no-sources`，检查 wheel/sdist 不包含 Skills/commands，但须含 `videonote_mcp/templates/` 及原许可证。执行 `uv run python tests/mcp_stdio_smoke.py --wheel dist/videonote-<版本>.whl`，从解包 wheel 在无关工作目录启动独立 stdio 冒烟，不可误用源码资源。

## 模块改动准则

贡献流程完全记录在仓库中，不需要个人记忆或私有开发 Skill。先读 `docs/00-新手上路.md`、架构文档，编辑 `app/` 前核对 `VENDOR.md`。

- 先找多处调用的真实共享逻辑，再提取模块；不要仅按行数拆文件，也不要让底层模块反向 import MCP/CLI 入口。
- 路径规整与权限判定用 `app/utils/local_paths.py`；平台检测用 `app/utils/media_source.py`。任务状态/转写读取用 `videonote_mcp/task_artifacts.py`，调用方自行执行状态准入和结果展示。
- 文件导出不得为了拿默认目录而 import 整个笔记流水线；模型/DB 初始化只留在需要它们的路径。
- 回归先证明原问题，再验证共享模块与所有入口。重点保留未知状态旧任务导出、失败任务拒绝、MCP/CLI 目录授权差异和缓存原件不被覆盖的契约。
- 依赖解耦用 `tests/test_core_boundaries.py` 的隔离子进程验证；同一 pytest 进程中模块可能已被其它测试导入，不能仅凭 `sys.modules` 判定无启动副作用。模板原件保持字节不变。
- CLI 和任务生命周期的大模块仍可逐步整理；跨进程 TOCTOU、平台真实登录/下载/ASR 等未验证范围要明确保留，不宣称全库问题已解决。

## 文档与提交自查

- 修改行为时同步 README 中英文版、`docs/04-使用手册.md` 和 MCP 工具描述。
- 修改架构时同步 `docs/02-架构设计.md`、可编辑架构图及 `VENDOR.md` 的分叉清单。
- 工具名称/数量变化同步 CI 精确名单和契约测试。最新测试基线集中在 `docs/00-新手上路.md`。
- `docs/05-优化清单.md`、`docs/06-执行进度.md`、`docs/CHANGELOG.md` 是历史日志，只追加；不要把历史 Skills 用法当成当前安装说明。
- 只提交可移植配置示例；不提交真实凭证、个人 `.mcp.json`、数据库、日志、浏览器会话或机器绝对路径。

## 发版（需要明确发布指令）

1. 确认目标版本和范围，更新 `pyproject.toml`、`videonote_mcp/__init__.py`、`.claude-plugin/plugin.json`，同步 `uv.lock` 的项目版本及发布说明。
2. 按上述 PR 流程合入并同步两分支，完成 CI 和打包验证。
3. 明确获得发布授权后，才在已验证提交上创建并推送 `vX.Y.Z` tag；[release.yml](.github/workflows/release.yml) 会执行发布流程。
4. 核对 GitHub Release / 包发布结果，不将“已推送代码”写成“已发布”。合并文档、清理分支或更新配置本身不触发发版。
