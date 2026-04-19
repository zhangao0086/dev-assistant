# Architecture

This document explains how Dev Assistant works internally.

## Overview

Dev Assistant is a web service that manages a queue of AI coding tasks. Each task runs Claude Code CLI in an isolated git worktree of your target project, so tasks never interfere with each other or with your main branch.

```
Browser / API client
        │
        ▼
  FastAPI server (server.py)
        │
        ▼
  ClaudeSessionManager (claude_session_manager.py)
        │
        ├── Task queue (asyncio.Queue)
        │       └── Single worker coroutine — processes tasks one at a time
        │
        ├── Per-task:  git worktree  →  claude CLI subprocess
        │
        └── Background: MR status poller (glab)
```

## Key Components

### `server.py` — HTTP layer

FastAPI application. Exposes REST endpoints for task management and serves the Web UI. Translates HTTP requests into calls on `ClaudeSessionManager`.

### `claude_session_manager.py` — Core logic

All the interesting behaviour lives here.

**Task lifecycle:**

```
PENDING
  │
  ▼  (worker picks up task)
  ├── [if plan mode]  PLANNING  ──►  user sends messages via API
  │                              ─►  user calls /plan/confirm
  │
  ▼
DEVELOPING   ← claude CLI subprocess running in worktree
  │
  ▼
COMMITTING   ← claude CLI resumes same session, runs /finishing-feature skill
  │             (creates git commit + glab MR)
  ▼
REVIEW       ← human reviews the MR; optional: start happy for mobile pair-programming
  │
  ▼
COMPLETED    (human calls /complete) or FAILED / CANCELLED
```

**Git isolation:**

Each task gets its own branch and worktree created inside `TARGET_PROJECT_PATH/.worktrees/task-<id>/`. The worktree is deleted when the task reaches a terminal state. The branch is also deleted on cleanup.

**Session IDs:**

Dev Assistant reuses Claude's session IDs:
- Develop stage: uses `session.session_id` as the Claude `--session-id`
- Commit stage: `claude --resume session.session_id` (same context, cheaper via prompt cache)
- Plan stage (if enabled): uses `session.session_id` with last 4 chars replaced by `0000`

**MR creation:**

After development, Claude runs the `/finishing-feature` skill inside the same session. That skill calls `glab mr create` internally. Dev Assistant then scrapes the MR URL from stdout.

**MR status polling:**

A background coroutine polls `glab mr view` every 60 seconds for tasks in REVIEW state. When the MR is merged or closed, the task automatically moves to COMPLETED or CANCELLED.

### `cron_task_manager.py` — Scheduled tasks

Runs a background coroutine that evaluates cron expressions using `croniter`. When a trigger fires, it submits a new task to `ClaudeSessionManager` with a pre-configured prompt.

### Web UI (`index.html`, `cron.html`, `cost-center.html`)

Vanilla HTML/CSS/JS — no build step required. Communicates with the FastAPI server via `fetch()` and Server-Sent Events (SSE) for real-time log streaming.

## External Tool Dependencies

| Tool | Purpose | Required? |
|------|---------|-----------|
| `claude` CLI | Executes coding tasks | **Yes** |
| `git` | Creates / removes worktrees, branches | **Yes** |
| `glab` | Creates MRs, polls MR status | No — tasks succeed but no MR is created |
| `tmux` | Runs `happy` in a background window | Only if using happy |
| `happy` | Mobile pair-programming | No |

## Data Storage

All state is stored on disk as JSON (no database):

```
~/.dev-assistant/           # default, configurable via DATA_DIR env var
├── dev-tasks.json          # All task metadata (persisted atomically via rename)
├── cron-tasks.json         # Cron task definitions
└── logs/
    └── <session-id>.jsonl  # Per-task log stream (one JSON object per line)
```

Data lives outside the project directory so git operations can never affect it. Logs contain the full Claude conversation history, so treat them as sensitive.

## Environment Variables

| Variable | Used by | Effect |
|----------|---------|--------|
| `TARGET_PROJECT_PATH` | `ClaudeSessionManager.__init__` | Base git repo for worktrees |
| `DATA_DIR` | `server.py`, `ClaudeSessionManager.__init__` | Directory for task data and logs (default: `~/.dev-assistant`) |
| `DEFAULT_BRANCH` | `ClaudeSessionManager.__init__` | Default branch to pull before creating worktrees (default: `master`) |
| `PORT` | `manage.sh`, `server.main()` | HTTP listen port (default 8089) |
| `GLAB_CONFIG_DIR` | subprocess env when calling `glab` | Points glab to `.gitlab/config.yml` |
| `CLAUDECODE` | Removed before spawning child `claude` | Prevents nested-CLI detection |
| `ANTHROPIC_SKIP_TOKEN_COUNT` | Set to `1` before child `claude` | Suppresses token-count API call that fails in nested context |

## Concurrency Model

- **One worker coroutine** processes tasks sequentially from the queue — worktrees are created one at a time to avoid git conflicts.
- **Claude processes** run concurrently (each task's subprocess is awaited non-blockingly via `asyncio`).
- **Async lock** (`asyncio.Lock`) protects session state mutations and file writes.
- **MR poller** runs every 60 s as a separate background task.

## Assumptions About the Target Repository

- Default branch is named `master`. If your project uses `main`, update the `git pull origin master` call in `claude_session_manager.py`.
- The repository must have a configured `origin` remote (needed for `git pull` before each task).
- For MR creation, the remote must be a GitLab instance and `glab` must be authenticated for that host.
