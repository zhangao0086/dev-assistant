# Dev Assistant

基于 Claude Code 的自动化开发任务管理系统。通过 Web UI 提交任务，系统在隔离的 git worktree 中启动 Claude Code 执行代码开发，完成后创建 Merge Request 供人工审查。

[English](./README.md) · [架构说明](./ARCHITECTURE.md)

## 截图

| 桌面端 | 移动端 |
|--------|--------|
| ![桌面端界面](./assets/pc-screenshot.png) | ![移动端界面](./assets/mobile-screenshot.png) |

---

> ### ⚠️ 运行前须知
>
> Dev Assistant **会在你的机器上执行 AI 生成的代码和 shell 命令**，设计定位是单用户本地工具。
>
> - **没有内置认证**。
> - 服务器默认绑定 `0.0.0.0:8089`，**不要**暴露到公网或不可信网络。
> - `TARGET_PROJECT_PATH` 下的所有代码都可能被 AI 自动修改、提交、推送，请使用你愿意仔细审阅的专属仓库。
> - 仅允许 localhost 访问：修改 [server.py](./server.py) 和 [manage.sh](./manage.sh) 中的 `0.0.0.0` 为 `127.0.0.1` 再启动。

---

## 工作原理

1. 你提交一个任务描述（"给用户 API 添加输入校验"）
2. Dev Assistant 在目标仓库中创建新分支 + git worktree
3. Claude Code 在该 worktree 中运行并编写代码
4. Claude 提交变更并创建 Merge/Pull Request
5. 你审查 MR/PR，决定合并或关闭

任务按队列逐个执行，互不干扰。

### VCS 支持

| 托管平台 | 状态 | 所需 CLI |
|---------|------|---------|
| GitLab（自建或 gitlab.com） | ✅ 主力支持，充分验证 | [`glab`](https://gitlab.com/gitlab-org/cli) |
| GitHub | ⚠️ 通过 [vcs_provider.py](./vcs_provider.py) 支持，验证较少，欢迎通过 [issues](https://github.com/your-username/dev-assistant/issues) 反馈问题 | [`gh`](https://cli.github.com/) |

VCS 层是可插拔的——在 [vcs_provider.py](./vcs_provider.py) 中实现 `VCSProvider` 即可接入其他平台。

## 前置条件

开始之前请逐一验证以下工具已安装。

### 必须

| 工具 | 安装方式 | 验证命令 |
|------|---------|---------|
| Python 3.11+ | [python.org](https://www.python.org/downloads/) | `python --version` |
| Git 2.15+ | [git-scm.com](https://git-scm.com/) | `git --version` |
| [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) | `npm install -g @anthropic-ai/claude-code` | `claude --version` |
| VCS CLI（二选一）：[glab](https://gitlab.com/gitlab-org/cli#installation) **或** [gh](https://cli.github.com/) | 见链接 | `glab version` / `gh --version` |
| tmux | `brew install tmux` / `apt install tmux` | `tmux -V` |
| [happy](https://github.com/slopus/happy-cli) | `npm install -g happy-coder` | `happy --version` |

> **关于 `glab`/`gh`：** 自动创建 MR/PR 所需。缺失时任务仍会运行并写代码，但在 commit/MR 步骤会失败。参见[不使用 VCS 提供商](#不使用-vcs-提供商)。
>
> **关于 `tmux` + `happy`：** 目前属于必需依赖。任务进入 REVIEW 状态时，Dev Assistant 会无条件在 `tmux` session 里启动 `happy`（[claude_session_manager.py:865](./claude_session_manager.py#L865)），缺失任一工具都会导致任务收尾出错。[happy](https://github.com/slopus/happy-cli) 是一个可选的第三方工具（与 Anthropic 无关联），启用后 Dev Assistant 会在 UI 和 MR/PR 评论中展示 `https://app.happy.engineering/session/<id>` 链接。"通过环境变量关闭"目前是已知待改进项，欢迎贡献。

## 快速开始

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

验证：`uvicorn --version` 能正常输出版本号。

### 2. 配置

先启动服务（第 5 步），然后打开 **http://localhost:8089/settings.html**，在界面中设置 `TARGET_PROJECT_PATH` 及其他选项。

> **请慎重选择这个仓库。** Claude 会在里面自主创建分支、提交、推送。选一个你自己拥有或有明确授权修改的项目，并确保它的远端是你愿意接收自动生成 MR/PR 的地方。

### 3. 配置 VCS CLI（用于创建 MR/PR）

**GitLab：**

```bash
cp .gitlab/config.yml.example .gitlab/config.yml
```

打开 `.gitlab/config.yml`，在对应 hostname 下填入你的 GitLab token。创建 token：GitLab → Settings → Access Tokens，勾选 `api` 权限。

**GitHub：**

```bash
gh auth login
```

Dev Assistant 会根据目标仓库的 remote URL 自动选择使用哪个 provider。

认证完成后，可在 **Settings → VCS Authentication Status** 中确认连接状态。

### 4. 在目标项目中安装 `finishing-feature` skill

任务完成后，Dev Assistant 会在目标项目的 worktree 中调用一个名为 `finishing-feature` 的 Claude Code skill，负责提交代码并通过 `glab` 创建 Merge Request。

该 skill 必须存在于目标项目的 `.claude/skills/` 目录中。本仓库已提供一份开箱即用的默认实现：

```bash
cp -r .agents/skills/finishing-feature /path/to/your-target-project/.claude/skills/
```

默认实现适用于任何 GitLab 或 GitHub 项目，无需修改即可使用。如需定制 commit message 风格、MR 模板、标签等，编辑复制后目录中的 `SKILL.md` 即可。

> **为什么放在目标项目里，而不是由 Dev Assistant 内置？** 每个仓库都有自己的规范——commit message 风格、MR/PR 模板、标签、审核规则、CI 门槛。这些规则理应由仓库本身拥有和维护，而不是硬编码进一个通用的任务执行器。把 `finishing-feature` 放在目标项目下，让每个仓库完全掌控自己的 commit/MR 行为，规则演进时也不需要改 Dev Assistant。

### 5. 启动服务

```bash
./manage.sh restart
```

推荐用 `restart` 而不是 `start`——它是幂等的：没在跑就直接起，在跑就干净地重启。

预期输出：
```
[INFO]  启动服务 (port: 8089)...
[SUCCESS] 服务启动成功 (PID: 12345, port: 8089)
[INFO]  访问: http://localhost:8089
```

浏览器打开 `http://localhost:8089`。

> **提醒：** 服务器默认绑定 `0.0.0.0`，同网段其他机器也能访问。如需限定本机访问，启动前将 [server.py:402](./server.py#L402) 和 [manage.sh:74](./manage.sh#L74) 中的 `0.0.0.0` 改为 `127.0.0.1`。

### 6. 提交第一个任务

在 Web UI 中输入任务描述，点击**提交**，实时查看日志输出。

---

## 不使用 VCS 提供商

如果没有配置 `glab` 或 `gh`，任务在 COMMITTING 阶段会失败。此时的选择：

- 任务在 DEVELOPING 阶段会成功完成代码编写
- 在任务进入 COMMITTING 之前手动取消，然后到 `TARGET_PROJECT_PATH/.worktrees/task-<id>/` 查看代码
- 或：贡献一个"跳过 MR 创建"的选项，参见 [CONTRIBUTING.md](./CONTRIBUTING.md)

## 使用方法

### 任务模式

| 模式 | 行为 |
|------|------|
| **直接模式** | Claude 立即开始编写代码 |
| **Plan 模式** | Claude 先生成实现方案，你确认后再开始开发 |

### 任务状态流转

```
PENDING
  │
  ├─(plan 模式)─► PLANNING ─► (你确认) ─►┐
  │                                      │
  └──────────────────────────────────────►
                                         │
                                    DEVELOPING
                                         │
                                    COMMITTING
                                         │
                                      REVIEW  ◄── MR/PR 已创建，去 GitLab 或 GitHub 审查
                                         │
                                    COMPLETED  （点击"标记完成"）
                                   或 FAILED / CANCELLED
```

### 定时任务

在 `/cron` 页面配置定时任务，支持标准 Cron 表达式。适用于夜间定时重构、自动文档更新等场景。

### 服务管理命令

```bash
./manage.sh start           # 启动服务
./manage.sh stop            # 停止（优雅清理 Claude 子进程）
./manage.sh restart         # 重启
./manage.sh status          # 查看 PID 和端口
./manage.sh logs [N]        # 打印最近 N 行日志（默认 50）
./manage.sh follow          # 实时跟踪日志
```

---

## 配置参考

所有配置均可通过 **Settings 页面**（`/settings.html`）在线修改。配置存储在 `~/.dev-assistant/config.json`，重启后仍然有效。Shell 环境变量优先级高于已保存的配置。

`PORT`、`DATA_DIR`、`TARGET_PROJECT_PATH` 修改后需要重启服务才生效。

| 变量 | 必填 | 默认值 | 说明 |
|-----|------|--------|------|
| `TARGET_PROJECT_PATH` | **是** | — | Claude 工作的 git 仓库绝对路径 |
| `DEFAULT_BRANCH` | 否 | `master` | 默认分支名，使用 main 分支的仓库改为 `main` |
| `DATA_DIR` | 否 | `~/.dev-assistant` | 任务数据和会话日志的存放目录 |
| `PORT` | 否 | `8089` | HTTP 端口 |
| `GLAB_CONFIG_DIR` | 否 | `.gitlab/` | 存放 glab `config.yml` 的目录 |
| `GH_CONFIG_DIR` | 否 | `.github/` | 存放 gh `config.yml` 的目录 |

---

## 项目结构

```
dev-assistant/
├── server.py                  # FastAPI 主服务
├── claude_session_manager.py  # 核心任务调度逻辑
├── cron_task_manager.py       # Cron 定时调度
├── manage.sh                  # 启动 / 停止 / 日志
├── index.html                 # 主界面
├── cron.html                  # 定时任务界面
├── cost-center.html           # 成本与 token 用量看板
├── settings.html              # 配置与 VCS 认证界面
├── static/                    # CSS 和 JS
├── .gitlab/
│   └── config.yml.example     # glab 配置模板  ← 复制为 config.yml
├── ARCHITECTURE.md            # 内部工作原理
└── PROGRESS.md                # 开发过程中的经验教训
```

运行时目录（自动创建）：

```
~/.dev-assistant/              # 配置与任务数据（在项目目录之外，不受 git 影响）
├── config.json                # 所有配置（通过 /settings.html 编辑）
├── dev-tasks.json             # 任务数据库
├── cron-tasks.json            # Cron 任务定义
└── logs/<session-id>.jsonl    # 每个任务的完整对话日志
logs/                          # 服务器日志（每日轮转，已 git-ignore）
run/                           # PID 文件（已 git-ignore）
```

---

## 常见问题

**`manage.sh start` 提示"uvicorn not found"**

```bash
pip install -r requirements.txt
```

**`manage.sh start` 提示"TARGET_PROJECT_PATH is not set"**

打开 `http://localhost:8089/settings.html`，在界面中设置 `TARGET_PROJECT_PATH`，然后重启服务。

**服务启动了，但第一个任务立刻失败**

查看服务日志：
```bash
./manage.sh logs 100
```

常见原因：
- `claude` CLI 未安装或未登录 → 运行 `claude --version` 和 `claude login`
- `TARGET_PROJECT_PATH` 没有 `master` 分支 → 在 Settings 中设置 `DEFAULT_BRANCH=main`（若仓库使用 `main`）
- `glab` 或 `gh` 未认证 → 在 **Settings → VCS Authentication Status** 查看具体错误和修复命令

**任务一直卡在 DEVELOPING**

Claude 子进程可能已挂起。用 `./manage.sh follow` 查看日志，然后通过 UI 或 `DELETE /sessions/<id>` 取消任务。

**端口 8089 已被占用**

在 **Settings → PORT** 中修改端口，然后重启服务。

---

## 安全说明

Dev Assistant 是一个**单用户、本地机器**工具，并非为多用户或公网部署而设计。

**服务器行为：**
- 以你的用户身份运行，没有登录或访问控制
- 默认绑定 `0.0.0.0:8089`（同局域网可访问）
- 在 `TARGET_PROJECT_PATH` 中启动 `claude` 子进程，授予广泛的工具权限（`Bash`、`Write`、`Edit` 等）
- 允许 Claude 执行任意 shell 命令、提交代码、推送到远端

**推荐做法：**
- 仅绑定 localhost——在共享网络环境下使用前，将 [server.py:402](./server.py#L402) 和 [manage.sh:74](./manage.sh#L74) 改为绑定 `127.0.0.1`。
- 使用专门的 `TARGET_PROJECT_PATH`，做好被 AI 改动的心理准备。不要指向生产代码库。
- 合并前逐个审查 MR/PR——将 AI 输出视为不可信。
- 不要把 `.gitlab/config.yml` 纳入版本控制（默认已 git-ignore）。
- `$DATA_DIR/logs/` 中的会话日志包含 Claude 的完整对话历史，包括所有代码和工具输出，请视为敏感数据。

---

## 贡献

参见 [CONTRIBUTING.md](./CONTRIBUTING.md)。

## License

MIT
