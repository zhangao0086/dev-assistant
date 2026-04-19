# Lessons Learned

## Environment variable issues when nesting Claude CLI calls

**Problem**: When spawning a child `claude` process from inside another Claude session, two things break:
1. The child `claude` CLI detects the `CLAUDECODE` environment variable, assumes it is running inside Claude Code, and behaves incorrectly.
2. The child `claude` CLI tries to call the token-counting API endpoint, which is unavailable in this environment, causing an error.

**Solution**: Strip both variables before launching the child process:
```python
env = os.environ.copy()
env.pop("CLAUDECODE", None)
env["ANTHROPIC_SKIP_TOKEN_COUNT"] = "1"
```

**Avoid next time**: Every `subprocess` call that launches the `claude` CLI must apply these two lines.

## `--permission-mode` limitations when running as root

**Problem**:
1. With `--permission-mode dontAsk`, Write/Edit and similar tool calls are all denied in the child `claude` process.
2. With `--permission-mode bypassPermissions`, running as root fails with: `--dangerously-skip-permissions cannot be used with root/sudo privileges for security reasons`.

**Solution**: Use `--permission-mode dontAsk` together with `--allowedTools` listing every tool the task needs up front:
```python
cmd = [
    "claude", "--print", prompt,
    "--permission-mode", "dontAsk",
    "--allowedTools", "Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task", "TodoWrite", "Skill",
    "--no-session-persistence",
]
```

**Exception**: When writing to a trusted, fixed file (e.g. writing `PLAN.md` during the `confirm_plan` stage), `dontAsk` is blocked by Claude's sensitive-file check. Fall back to `bypassPermissions` in that case — only viable for non-root users and when the write target is tightly controlled.

**Avoid next time**: In automated scenarios default to `dontAsk` + `--allowedTools`; do not use `bypassPermissions` (not available under root). Switch to `bypassPermissions` only when writing to a whitelisted file gets blocked.

## happy cannot be launched non-interactively via subprocess.Popen

**Problem**:
1. `happy` is an interactive tool that needs a tty — launching it via `subprocess.Popen` causes it to exit immediately.
2. `happy` manages its own `--allowedTools` internally (only allows `mcp__happy__change_title`); externally supplied arguments are ignored.
3. `happy` connects by scanning a QR code from the mobile app and never emits a URL to stdout, so the `_scan_url` thread never sees one.
4. After the process exits, `happy_pid` was not cleared. `os.kill(pid, 0)` returns success even for zombie processes, which left the status stuck on "running".

**Solution**: Create a **dedicated tmux session** per task (not a window) and use `tmux has-session` to check liveness:
```python
tmux_session_name = f"happy-{session_id[:8]}"
subprocess.run(
    ["tmux", "new-session", "-d", "-s", tmux_session_name,
     "sh", "-c", f"env -u CLAUDECODE DEBUG=1 happy --resume {session_id}"],
    capture_output=True, text=True
)
session.happy_window = tmux_session_name  # legacy field name; actually stores the session name

# Liveness check
subprocess.run(["tmux", "has-session", "-t", tmux_session_name], ...)
```

**Avoid next time**: `happy` must run inside a dedicated tmux session — one session per task, fully isolated. Check liveness with `tmux has-session`, not `os.kill`.

## Use `claude --resume` to preserve context continuity

**Improvement**:
1. Previously `_commit_and_create_mr` used `--no-session-persistence`, creating an isolated Claude invocation with a fresh context every time.
2. Now `--session-id` creates the session and subsequent stages use `--resume` to continue the same session.

**Implementation**:
- Plan stage (if enabled): uses `session_id[:-4] + "0000"` as a separate session ID (replace the last 4 chars with `0`).
- Development stage: `claude --session-id session.session_id` creates a new session.
- Commit stage: `claude --resume session.session_id` continues the development-stage session.
- The `/finishing-feature` skill handles commit and MR creation in one shot.

**Benefits**:
1. Commit messages can be generated from the full development history, producing more accurate descriptions.
2. Prompt cache reuse lowers API cost.
3. If committing fails, debugging can continue in the same session.
4. The plan stage stays isolated, so it does not affect the dev/commit context continuity.

**Session count**:
- Without plan mode: 1 Claude session (shared between dev and commit).
- With plan mode: 2 Claude sessions (plan is isolated, dev + commit share one).

**Key points**:
- `--session-id` can create a new session with any UUID.
- `--resume` continues an existing session.
- Use `session.session_id` directly as the Claude session ID for the dev and commit stages.
- For the plan stage, replace the last 4 chars of the session ID with `"0000"` to derive a distinct session ID — simple, and still a valid UUID.
