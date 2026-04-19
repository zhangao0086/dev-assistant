# Project Goal

This project explores automated task development based on Claude sessions,
managing Claude Code task execution through a Web UI.

## How It Works

1. Accept development tasks submitted by the user
2. Create an isolated git worktree in the target project (a new branch per task)
3. Launch a Claude session inside that worktree to execute the task
4. When the task is done, the user reviews the changes and decides whether to merge

## Target Project Configuration

The target project path is configured via the `TARGET_PROJECT_PATH` environment
variable, or by editing the default in `claude_session_manager.py`.

## Rules

1. Before creating a worktree, make sure the current `master` branch is up to date
2. Never switch branches directly — always operate inside the worktree

## Capturing Lessons Learned

After hitting a problem or finishing a meaningful change, record it in
[PROGRESS.md](./PROGRESS.md):
- What went wrong
- How it was resolved
- How to avoid it next time

**Never make the same mistake twice.**

Always check PROGRESS.md to understand the current project state before starting tasks.
