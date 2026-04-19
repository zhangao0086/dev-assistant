# Contributing

Contributions are welcome! Please read this guide before opening a PR.

## Understanding the codebase

Start with [ARCHITECTURE.md](./ARCHITECTURE.md) — it explains the task lifecycle, concurrency model, and every external tool the system calls. Then read [PROGRESS.md](./PROGRESS.md) for hard-won lessons about edge cases.

## Development setup

```bash
git clone https://github.com/your-username/dev-assistant.git
cd dev-assistant
pip install -r requirements.txt
cp .env.example .env
# Edit .env — set TARGET_PROJECT_PATH to any existing git repo for testing
```

Start the server in foreground so you can see all output:

```bash
uvicorn server:app --host 0.0.0.0 --port 8089 --reload
```

The `--reload` flag restarts the server automatically when you save a Python file.

## Running the integration tests

```bash
# Make sure TARGET_PROJECT_PATH is set first
python test_integration.py
```

The integration test creates real worktrees in your `TARGET_PROJECT_PATH`, runs a minimal Claude session, and cleans up afterwards. It requires `claude` CLI to be installed and authenticated.

## Project layout

| File | What to edit |
|------|--------------|
| `claude_session_manager.py` | Task lifecycle, git operations, Claude subprocess calls |
| `server.py` | HTTP endpoints |
| `cron_task_manager.py` | Scheduled task logic |
| `index.html` / `static/` | Web UI |
| `.env.example` | Add new config variables here first |

## Adding a new configuration variable

1. Add it to `.env.example` with a comment explaining what it does and its default value
2. Read it in the appropriate Python file with `os.environ.get("MY_VAR", "default")`
3. Document it in the **Configuration reference** table in both `README.md` and `README.zh.md`
4. If it affects the task lifecycle, mention it in `ARCHITECTURE.md`

## Common contribution areas

- **GitHub support** — MR creation is currently GitLab-only (`glab`). Adding a `gh` code path would make the project much more widely usable.
- **Skip-MR mode** — a `SKIP_MR=true` env var that lets tasks complete without needing GitLab at all.
- **`main` branch as default** — currently defaults to `master`; now configurable via `DEFAULT_BRANCH` but could auto-detect.
- **Authentication** — even basic token auth on the HTTP API would make it safer to expose on a shared server.
- **Test coverage** — `test_integration.py` covers the happy path; unit tests for `ClaudeSessionManager` methods would be valuable.

## Submitting a PR

1. Fork the repo and create a branch: `git checkout -b feat/your-feature`
2. Make your changes
3. Update `PROGRESS.md` if you hit any non-obvious problem or learned something worth remembering
4. Open a pull request with a clear description of what it does and why

## Commit message format

Follow [Conventional Commits](https://www.conventionalcommits.org/):

| Prefix | Use for |
|--------|---------|
| `feat:` | New functionality |
| `fix:` | Bug fixes |
| `refactor:` | Code changes with no behaviour change |
| `docs:` | Documentation only |
| `chore:` | Build, deps, tooling |

## Hard rules

- **Never hardcode paths, tokens, or credentials** — use environment variables and document them in `.env.example`
- **Never commit `.env` or `.gitlab/config.yml`** — both are git-ignored for a reason
- **Keep the README bilingual** — update both `README.md` (English, authoritative) and `README.zh.md` together
