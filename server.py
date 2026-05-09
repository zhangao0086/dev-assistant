"""Dev Assistant - FastAPI backend server (multi-repo)"""

import asyncio
import json
import logging
import logging.handlers
import os
import shutil
import subprocess
import uuid

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from claude_session_manager import ClaudeSessionManager, SessionStatus
from cron_task_manager import CronTaskManager

# ---------------------------------------------------------------------------
# Config file: ~/.dev-assistant/config.json
# ---------------------------------------------------------------------------

_CONFIG_FILE = os.path.expanduser("~/.dev-assistant/config.json")

_GLOBAL_SETTINGS_KEYS = ["DATA_DIR", "PORT", "GLAB_CONFIG_DIR", "GH_CONFIG_DIR"]
_GLOBAL_DEFAULTS = {
    "DATA_DIR": "~/.dev-assistant",
    "PORT": "8089",
    "GLAB_CONFIG_DIR": os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gitlab"),
    "GH_CONFIG_DIR": os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github"),
}
_RESTART_REQUIRED_KEYS = {"PORT", "DATA_DIR"}


def _read_config() -> dict:
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _write_config(config: dict):
    os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _cfg(key: str) -> str:
    """Return setting value: shell env > config.json > hardcoded default."""
    return os.environ.get(key) or _read_config().get(key) or _GLOBAL_DEFAULTS.get(key, "")


# Apply config.json to os.environ so that downstream modules can use os.environ
for _k, _v in _read_config().items():
    if _k and _v and isinstance(_v, str):
        os.environ.setdefault(_k, _v)

# Logging setup
_data_dir = os.path.expanduser(_cfg("DATA_DIR"))
os.makedirs(_data_dir, exist_ok=True)

log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, "server.log")

handler = logging.handlers.TimedRotatingFileHandler(
    log_file, when="midnight", backupCount=30, encoding="utf-8"
)
handler.suffix = "%Y%m%d"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[handler, logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Repo registry
# ---------------------------------------------------------------------------

@dataclass
class RepoEntry:
    id: str
    name: str
    path: str
    default_branch: str
    glab_config_dir: str = ""
    gh_config_dir: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "path": self.path,
            "default_branch": self.default_branch,
            "glab_config_dir": self.glab_config_dir,
            "gh_config_dir": self.gh_config_dir,
        }


class RepoRegistry:
    """Manages multiple repo manager pairs."""

    def __init__(self, data_dir: str, repos_config: list[dict]):
        self.data_dir = data_dir
        self.repos: dict[str, RepoEntry] = {}
        self.managers: dict[str, ClaudeSessionManager] = {}
        self.cron_managers: dict[str, CronTaskManager] = {}
        self._errors: dict[str, str] = {}

        for r in repos_config:
            self._init_repo(r)

    def _init_repo(self, repo_dict: dict, skip_defaults: bool = True):
        """Initialize manager pair for a repo. Returns error message or None."""
        entry = RepoEntry(
            id=repo_dict["id"],
            name=repo_dict["name"],
            path=repo_dict["path"],
            default_branch=repo_dict.get("default_branch", "master"),
            glab_config_dir=repo_dict.get("glab_config_dir", ""),
            gh_config_dir=repo_dict.get("gh_config_dir", ""),
        )

        if not os.path.isdir(entry.path):
            self._errors[entry.id] = f"Repository path does not exist: {entry.path}"
            logger.warning("Repo %s (%s): path not found: %s", entry.name, entry.id, entry.path)
            self.repos[entry.id] = entry
            return self._errors[entry.id]

        repo_data_dir = os.path.join(self.data_dir, "repos", entry.id)
        os.makedirs(repo_data_dir, exist_ok=True)
        os.makedirs(os.path.join(repo_data_dir, "logs"), exist_ok=True)

        glab_dir = entry.glab_config_dir or _cfg("GLAB_CONFIG_DIR")
        gh_dir = entry.gh_config_dir or _cfg("GH_CONFIG_DIR")

        try:
            mgr = ClaudeSessionManager(
                target_repo=entry.path,
                data_file=os.path.join(repo_data_dir, "dev-tasks.json"),
                default_branch=entry.default_branch,
                glab_config_dir=glab_dir,
                gh_config_dir=gh_dir,
            )
            cron_mgr = CronTaskManager(
                session_manager=mgr,
                data_file=os.path.join(repo_data_dir, "cron-tasks.json"),
                skip_defaults=skip_defaults,
            )
            self.repos[entry.id] = entry
            self.managers[entry.id] = mgr
            self.cron_managers[entry.id] = cron_mgr
            return None
        except Exception as e:
            self._errors[entry.id] = str(e)
            self.repos[entry.id] = entry
            logger.error("Failed to init repo %s: %s", entry.name, e)
            return str(e)

    async def start_all(self):
        for repo_id, mgr in self.managers.items():
            await mgr.start_worker()
            cron_mgr = self.cron_managers.get(repo_id)
            if cron_mgr:
                await cron_mgr.start()

    async def shutdown_all(self):
        for mgr in self.managers.values():
            await mgr.shutdown()
        for cron_mgr in self.cron_managers.values():
            await cron_mgr.stop()

    def get_manager(self, repo_id: str) -> ClaudeSessionManager:
        mgr = self.managers.get(repo_id)
        if not mgr:
            raise HTTPException(status_code=404, detail=f"Repository not found or not initialized: {repo_id}")
        return mgr

    def get_cron_manager(self, repo_id: str) -> CronTaskManager:
        mgr = self.cron_managers.get(repo_id)
        if not mgr:
            raise HTTPException(status_code=404, detail=f"Repository not found or not initialized: {repo_id}")
        return mgr

    def find_manager_by_session(self, session_id: str) -> tuple[str, ClaudeSessionManager]:
        """Find which repo a session belongs to. Returns (repo_id, manager)."""
        for repo_id, mgr in self.managers.items():
            if session_id in mgr.sessions:
                return repo_id, mgr
        raise HTTPException(status_code=404, detail="Session not found")

    def find_cron_manager_by_task(self, task_id: str) -> tuple[str, CronTaskManager]:
        """Find which repo a cron task belongs to. Returns (repo_id, cron_manager)."""
        for repo_id, cron_mgr in self.cron_managers.items():
            if task_id in cron_mgr.tasks:
                return repo_id, cron_mgr
        raise HTTPException(status_code=404, detail="Cron task not found")

    async def add_repo(self, repo_dict: dict) -> Optional[str]:
        """Add a new repo at runtime. Returns error or None."""
        err = self._init_repo(repo_dict, skip_defaults=True)
        if err:
            return err
        repo_id = repo_dict["id"]
        mgr = self.managers.get(repo_id)
        if mgr:
            await mgr.start_worker()
            cron_mgr = self.cron_managers.get(repo_id)
            if cron_mgr:
                await cron_mgr.start()
        return None

    async def remove_repo(self, repo_id: str) -> Optional[str]:
        """Remove a repo. Returns error if active sessions exist."""
        mgr = self.managers.get(repo_id)
        if mgr:
            active = [
                s for s in mgr.sessions.values()
                if s.status not in (SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED)
            ]
            if active:
                return f"Cannot remove: {len(active)} active session(s) exist"
            await mgr.shutdown()

        cron_mgr = self.cron_managers.get(repo_id)
        if cron_mgr:
            await cron_mgr.stop()

        self.managers.pop(repo_id, None)
        self.cron_managers.pop(repo_id, None)
        self.repos.pop(repo_id, None)
        self._errors.pop(repo_id, None)
        return None


# ---------------------------------------------------------------------------
# Migration: single-repo config → multi-repo
# ---------------------------------------------------------------------------

def _migrate_config():
    """Migrate old TARGET_PROJECT_PATH config to repos array."""
    config = _read_config()

    if "repos" in config:
        return  # Already migrated

    target_path = config.get("TARGET_PROJECT_PATH") or os.environ.get("TARGET_PROJECT_PATH", "")
    if not target_path:
        return  # Nothing to migrate

    repo_id = str(uuid.uuid4())
    repo_name = os.path.basename(target_path.rstrip("/")) or "default"
    default_branch = config.get("DEFAULT_BRANCH") or os.environ.get("DEFAULT_BRANCH", "master")

    repo_entry = {
        "id": repo_id,
        "name": repo_name,
        "path": target_path,
        "default_branch": default_branch,
        "glab_config_dir": "",
        "gh_config_dir": "",
    }

    # Move existing data files to per-repo directory
    data_dir = os.path.expanduser(config.get("DATA_DIR") or _GLOBAL_DEFAULTS["DATA_DIR"])
    repo_data_dir = os.path.join(data_dir, "repos", repo_id)
    os.makedirs(repo_data_dir, exist_ok=True)
    os.makedirs(os.path.join(repo_data_dir, "logs"), exist_ok=True)

    old_tasks = os.path.join(data_dir, "dev-tasks.json")
    if os.path.exists(old_tasks):
        shutil.move(old_tasks, os.path.join(repo_data_dir, "dev-tasks.json"))
        logger.info("Migrated dev-tasks.json to repos/%s/", repo_id)

    old_cron = os.path.join(data_dir, "cron-tasks.json")
    if os.path.exists(old_cron):
        shutil.move(old_cron, os.path.join(repo_data_dir, "cron-tasks.json"))
        logger.info("Migrated cron-tasks.json to repos/%s/", repo_id)

    old_logs = os.path.join(data_dir, "logs")
    new_logs = os.path.join(repo_data_dir, "logs")
    if os.path.isdir(old_logs) and os.listdir(old_logs):
        for fname in os.listdir(old_logs):
            src = os.path.join(old_logs, fname)
            dst = os.path.join(new_logs, fname)
            if os.path.isfile(src) and not os.path.exists(dst):
                shutil.move(src, dst)
        logger.info("Migrated log files to repos/%s/logs/", repo_id)

    # Update config
    config["repos"] = [repo_entry]
    config.pop("TARGET_PROJECT_PATH", None)
    config.pop("DEFAULT_BRANCH", None)
    _write_config(config)
    logger.info("Migration complete: single-repo → multi-repo (repo_id=%s, name=%s)", repo_id, repo_name)


# Run migration at startup
_migrate_config()

# ---------------------------------------------------------------------------
# Bootstrap registry
# ---------------------------------------------------------------------------

_config = _read_config()
_repos_config = _config.get("repos", [])
registry = RepoRegistry(data_dir=_data_dir, repos_config=_repos_config)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await registry.start_all()
    yield
    await registry.shutdown_all()


app = FastAPI(title="Dev Assistant", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


# ---- Request models ----

class CreateSessionRequest(BaseModel):
    prompt: str
    repo_id: str
    session_id: Optional[str] = None
    use_plan_mode: bool = False


class CreateRepoRequest(BaseModel):
    name: str
    path: str
    default_branch: str = "master"
    glab_config_dir: str = ""
    gh_config_dir: str = ""


class UpdateRepoRequest(BaseModel):
    name: Optional[str] = None
    path: Optional[str] = None
    default_branch: Optional[str] = None
    glab_config_dir: Optional[str] = None
    gh_config_dir: Optional[str] = None


# ---- Helpers ----

def _inject_repo_info(d: dict, repo_id: str) -> dict:
    """Inject repo_id and repo_name into a response dict."""
    d["repo_id"] = repo_id
    entry = registry.repos.get(repo_id)
    d["repo_name"] = entry.name if entry else ""
    return d


# ---- Repo management endpoints ----

@app.get("/repos")
async def list_repos():
    """List all configured repositories."""
    result = []
    for repo_id, entry in registry.repos.items():
        d = entry.to_dict()
        d["error"] = registry._errors.get(repo_id)
        d["initialized"] = repo_id in registry.managers
        result.append(d)
    return result


@app.post("/repos", status_code=201)
async def create_repo(req: CreateRepoRequest):
    """Add a new repository."""
    if not os.path.isdir(req.path):
        raise HTTPException(status_code=400, detail=f"Path does not exist: {req.path}")

    # Verify it's a git repo
    git_check = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=req.path, capture_output=True, text=True
    )
    if git_check.returncode != 0:
        raise HTTPException(status_code=400, detail=f"Not a git repository: {req.path}")

    repo_id = str(uuid.uuid4())
    repo_dict = {
        "id": repo_id,
        "name": req.name,
        "path": req.path,
        "default_branch": req.default_branch,
        "glab_config_dir": req.glab_config_dir,
        "gh_config_dir": req.gh_config_dir,
    }

    err = await registry.add_repo(repo_dict)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Persist to config
    config = _read_config()
    config.setdefault("repos", []).append(repo_dict)
    _write_config(config)

    logger.info("Repo added: %s (%s) at %s", req.name, repo_id, req.path)
    return registry.repos[repo_id].to_dict()


@app.put("/repos/{repo_id}")
async def update_repo(repo_id: str, req: UpdateRepoRequest):
    """Update a repository's settings."""
    entry = registry.repos.get(repo_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Repository not found")

    if req.name is not None:
        entry.name = req.name
    if req.path is not None:
        if not os.path.isdir(req.path):
            raise HTTPException(status_code=400, detail=f"Path does not exist: {req.path}")
        entry.path = req.path
    if req.default_branch is not None:
        entry.default_branch = req.default_branch
    if req.glab_config_dir is not None:
        entry.glab_config_dir = req.glab_config_dir
    if req.gh_config_dir is not None:
        entry.gh_config_dir = req.gh_config_dir

    # Persist to config
    config = _read_config()
    repos = config.get("repos", [])
    for r in repos:
        if r["id"] == repo_id:
            r.update(entry.to_dict())
            break
    _write_config(config)

    logger.info("Repo updated: %s (%s)", entry.name, repo_id)
    return entry.to_dict()


@app.delete("/repos/{repo_id}")
async def delete_repo(repo_id: str):
    """Remove a repository (only if no active sessions)."""
    if repo_id not in registry.repos:
        raise HTTPException(status_code=404, detail="Repository not found")

    err = await registry.remove_repo(repo_id)
    if err:
        raise HTTPException(status_code=400, detail=err)

    # Persist to config
    config = _read_config()
    config["repos"] = [r for r in config.get("repos", []) if r["id"] != repo_id]
    _write_config(config)

    logger.info("Repo removed: %s", repo_id)
    return {"ok": True}


# ---- Session endpoints ----

@app.get("/stats")
async def get_stats(repo_id: Optional[str] = Query(None)):
    """Return overall stats, optionally filtered by repo."""
    if repo_id:
        mgr = registry.get_manager(repo_id)
        return await mgr.get_stats()

    # Aggregate across all repos
    total = 0
    by_status = {}
    for mgr in registry.managers.values():
        stats = await mgr.get_stats()
        total += stats["total"]
        for status, count in stats["by_status"].items():
            by_status[status] = by_status.get(status, 0) + count
    return {"total": total, "by_status": by_status}


@app.get("/cost-stats")
async def get_cost_stats(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    repo_id: Optional[str] = Query(None),
):
    """Return cost statistics, optionally filtered by repo."""
    if repo_id:
        mgr = registry.get_manager(repo_id)
        return await mgr.get_cost_stats(start_date, end_date)

    # Aggregate across all repos
    total_cost = 0.0
    total_input_tokens = 0
    total_output_tokens = 0
    total_tasks = 0
    by_type = {
        "refactor": {"count": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0},
        "docs": {"count": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0},
        "normal": {"count": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0},
    }
    daily_stats = {}
    all_top_tasks = []

    for r_id, mgr in registry.managers.items():
        stats = await mgr.get_cost_stats(start_date, end_date)
        total_cost += stats["total_cost_usd"]
        total_input_tokens += stats["total_input_tokens"]
        total_output_tokens += stats["total_output_tokens"]
        total_tasks += stats["total_tasks"]

        for t_type, t_data in stats["by_type"].items():
            if t_type in by_type:
                by_type[t_type]["count"] += t_data["count"]
                by_type[t_type]["cost"] += t_data["cost"]
                by_type[t_type]["input_tokens"] += t_data["input_tokens"]
                by_type[t_type]["output_tokens"] += t_data["output_tokens"]

        for date_str, day_data in stats["daily_stats"].items():
            if date_str not in daily_stats:
                daily_stats[date_str] = {"cost": 0.0, "count": 0, "input_tokens": 0, "output_tokens": 0, "completed": 0}
            daily_stats[date_str]["cost"] += day_data["cost"]
            daily_stats[date_str]["count"] += day_data["count"]
            daily_stats[date_str]["input_tokens"] += day_data["input_tokens"]
            daily_stats[date_str]["output_tokens"] += day_data["output_tokens"]
            daily_stats[date_str]["completed"] += day_data["completed"]

        repo_name = registry.repos[r_id].name if r_id in registry.repos else ""
        for task in stats["top_tasks"]:
            task["repo_id"] = r_id
            task["repo_name"] = repo_name
            all_top_tasks.append(task)

    top_tasks = sorted(all_top_tasks, key=lambda x: x["cost"], reverse=True)[:10]

    return {
        "total_cost_usd": round(total_cost, 4),
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "total_tasks": total_tasks,
        "by_type": by_type,
        "daily_stats": daily_stats,
        "top_tasks": top_tasks,
    }


@app.get("/sessions")
async def list_sessions(repo_id: Optional[str] = Query(None)):
    """List all sessions, optionally filtered by repo."""
    result = []
    targets = [(repo_id, registry.get_manager(repo_id))] if repo_id else list(registry.managers.items())

    for r_id, mgr in targets:
        sessions = await mgr.list_sessions()
        for s in sessions:
            d = s.to_dict()
            _inject_repo_info(d, r_id)
            result.append(d)
    return result


@app.post("/sessions", status_code=201)
async def create_session(req: CreateSessionRequest):
    """Create a session and enqueue it"""
    mgr = registry.get_manager(req.repo_id)
    session_id = await mgr.create_session(req.prompt, req.session_id, req.use_plan_mode)
    logger.info("Session created: %s | repo: %s | plan_mode: %s | prompt: %.80s",
                session_id, req.repo_id, req.use_plan_mode, req.prompt)
    session = await mgr.get_session(session_id)
    d = session.to_dict()
    _inject_repo_info(d, req.repo_id)
    return d


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Get a single session"""
    repo_id, mgr = registry.find_manager_by_session(session_id)
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    d = session.to_dict()
    _inject_repo_info(d, repo_id)
    return d


@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str):
    """Cancel a session"""
    repo_id, mgr = registry.find_manager_by_session(session_id)
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    ok = await mgr.cancel_session(session_id)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Cannot cancel session in status: {session.status}")

    logger.info("Session cancelled: %s", session_id)
    return {"ok": True}


@app.post("/sessions/{session_id}/complete")
async def complete_session(session_id: str):
    """Mark a session as completed (review -> completed)"""
    _, mgr = registry.find_manager_by_session(session_id)
    ok = await mgr.complete_session(session_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Session not in review status")
    logger.info("Session completed: %s", session_id)
    return {"ok": True}


@app.post("/sessions/{session_id}/happy")
async def start_happy(session_id: str):
    """Start a happy session in the worktree"""
    _, mgr = registry.find_manager_by_session(session_id)
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != SessionStatus.REVIEW:
        raise HTTPException(status_code=400, detail="Session must be in review status")
    pid = await mgr.start_happy(session_id)
    if pid is None:
        raise HTTPException(status_code=500, detail="Failed to start happy session")
    logger.info("Happy session started: %s (pid=%s)", session_id, pid)
    return {"pid": pid}


@app.delete("/sessions/{session_id}/happy")
async def stop_happy(session_id: str):
    """Stop the happy session"""
    _, mgr = registry.find_manager_by_session(session_id)
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    ok = await mgr.stop_happy(session_id)
    return {"ok": ok}


@app.get("/sessions/{session_id}/happy")
async def get_happy_status(session_id: str):
    """Get happy session status"""
    _, mgr = registry.find_manager_by_session(session_id)
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return mgr.get_happy_status(session_id)


@app.post("/sessions/{session_id}/plan/message")
async def send_plan_message(session_id: str, req: dict):
    """Send a message to the plan session"""
    _, mgr = registry.find_manager_by_session(session_id)
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != SessionStatus.PLANNING:
        raise HTTPException(status_code=400, detail="Session is not in planning status")

    message = req.get("message", "")
    if not message:
        raise HTTPException(status_code=400, detail="Message is required")

    ok = await mgr.send_plan_message(session_id, message)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to send message")

    logger.info("Plan message sent: %s | message: %.80s", session_id, message)
    return {"ok": True}


@app.get("/sessions/{session_id}/plan/messages")
async def get_plan_messages(session_id: str, offset: int = 0):
    """Get plan conversation history (poll)"""
    _, mgr = registry.find_manager_by_session(session_id)
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = await mgr.get_plan_messages(session_id, offset)
    return {"messages": messages, "total": len(messages)}


@app.post("/sessions/{session_id}/plan/confirm")
async def confirm_plan(session_id: str):
    """Confirm the plan and start development"""
    _, mgr = registry.find_manager_by_session(session_id)
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != SessionStatus.PLANNING:
        raise HTTPException(status_code=400, detail="Session is not in planning status")

    ok = await mgr.confirm_plan(session_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Failed to confirm plan, PLAN.md may not exist")

    logger.info("Plan confirmed: %s", session_id)
    return {"ok": True}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a completed/failed/cancelled session"""
    _, mgr = registry.find_manager_by_session(session_id)
    ok = await mgr.delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Session not found or still active")
    return {"ok": True}


@app.get("/sessions/{session_id}/logs")
async def get_logs(session_id: str, limit: int = 0):
    """Get session logs"""
    _, mgr = registry.find_manager_by_session(session_id)
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return await mgr.get_session_logs(session_id, limit)


@app.get("/sessions/{session_id}/stream")
async def stream_logs(session_id: str):
    """SSE real-time log stream for a session"""
    _, mgr = registry.find_manager_by_session(session_id)
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    async def event_generator():
        seen = 0
        while True:
            logs = session.read_logs(offset=seen)
            for log in logs:
                data = f"data: {log['level']}|{log['content']}\n\n"
                yield data
            seen += len(logs)

            if session.status in [SessionStatus.REVIEW, SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED]:
                yield "data: [DONE]\n\n"
                break

            await asyncio.sleep(0.3)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---- Cron task endpoints ----

@app.get("/cron-tasks")
async def list_cron_tasks(repo_id: Optional[str] = Query(None)):
    """List all cron tasks, optionally filtered by repo."""
    result = []
    targets = [(repo_id, registry.get_cron_manager(repo_id))] if repo_id else list(registry.cron_managers.items())

    for r_id, cron_mgr in targets:
        for t in cron_mgr.list_tasks():
            d = t.to_dict()
            _inject_repo_info(d, r_id)
            result.append(d)
    return result


@app.post("/cron-tasks", status_code=201)
async def create_cron_task(req: dict):
    """Create a cron task"""
    repo_id = req.get("repo_id")
    if not repo_id:
        raise HTTPException(status_code=400, detail="repo_id is required")

    cron_mgr = registry.get_cron_manager(repo_id)
    try:
        task = cron_mgr.create_task(
            name=req["name"],
            prompt=req["prompt"],
            cron_expr=req["cron_expr"],
            enabled=req.get("enabled", True),
            max_open=req.get("max_open", req.get("max_concurrent", 1)),
            mr_labels=req.get("mr_labels", []),
            use_plan_mode=req.get("use_plan_mode", False),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e}")
    logger.info("Cron task created: %s (%s) for repo %s", task.name, task.id, repo_id)
    d = task.to_dict()
    _inject_repo_info(d, repo_id)
    return d


@app.put("/cron-tasks/{task_id}")
async def update_cron_task(task_id: str, req: dict):
    """Update a cron task"""
    repo_id, cron_mgr = registry.find_cron_manager_by_task(task_id)
    allowed = {"name", "prompt", "cron_expr", "enabled", "max_open",
               "mr_labels", "use_plan_mode"}
    updates = {k: v for k, v in req.items() if k in allowed}
    try:
        task = cron_mgr.update_task(task_id, **updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e}")
    if not task:
        raise HTTPException(status_code=404, detail="Cron task not found")
    logger.info("Cron task updated: %s (%s)", task.name, task_id)
    d = task.to_dict()
    _inject_repo_info(d, repo_id)
    return d


@app.delete("/cron-tasks/{task_id}")
async def delete_cron_task(task_id: str):
    """Delete a cron task"""
    _, cron_mgr = registry.find_cron_manager_by_task(task_id)
    ok = cron_mgr.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cron task not found")
    logger.info("Cron task deleted: %s", task_id)
    return {"ok": True}


@app.post("/cron-tasks/{task_id}/trigger")
async def trigger_cron_task(task_id: str):
    """Trigger a cron task immediately"""
    _, cron_mgr = registry.find_cron_manager_by_task(task_id)
    ok = await cron_mgr.trigger_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cron task not found")
    logger.info("Cron task triggered manually: %s", task_id)
    return {"ok": True}


# ---- Settings endpoints ----

@app.get("/settings")
async def get_settings():
    """Return current global configuration values."""
    file_config = _read_config()
    result = {k: file_config.get(k, _GLOBAL_DEFAULTS.get(k, "")) for k in _GLOBAL_SETTINGS_KEYS}
    result["_restart_required_keys"] = list(_RESTART_REQUIRED_KEYS)
    result["_has_repos"] = len(registry.repos) > 0
    return result


@app.post("/settings")
async def update_settings(req: dict):
    """Save global configuration to config.json."""
    updates = {k: str(v) for k, v in req.items() if k in _GLOBAL_SETTINGS_KEYS}
    config = _read_config()
    for k, v in updates.items():
        if v:
            config[k] = v
        elif k in config:
            del config[k]
    _write_config(config)

    for key in ("GLAB_CONFIG_DIR", "GH_CONFIG_DIR"):
        if key in updates and updates[key]:
            os.environ[key] = updates[key]

    restart_required = bool(updates.keys() & _RESTART_REQUIRED_KEYS)
    logger.info("Settings updated: %s (restart_required=%s)", list(updates.keys()), restart_required)
    return {"ok": True, "restart_required": restart_required}


@app.get("/settings/vcs-status")
async def get_vcs_status():
    """Check gh and glab CLI authentication status"""
    def _check(cmd: list[str]) -> dict:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            output = (r.stdout + r.stderr).strip()
            logged_in = [l.strip() for l in output.splitlines() if "Logged in to" in l]
            if logged_in:
                return {"available": True, "authenticated": True, "detail": logged_in[0]}
            if r.returncode == 0:
                first_line = next((l.strip() for l in output.splitlines() if l.strip()), "")
                return {"available": True, "authenticated": True, "detail": first_line}
            return {"available": True, "authenticated": False, "detail": output}
        except FileNotFoundError:
            return {"available": False, "authenticated": False, "detail": f"{cmd[0]} CLI not installed"}
        except Exception as e:
            return {"available": False, "authenticated": False, "detail": str(e)}

    return {
        "gh": _check(["gh", "auth", "status"]),
        "glab": _check(["glab", "auth", "status"]),
    }


# ---- Admin ----

@app.post("/admin/shutdown")
async def shutdown():
    """Clean up all active processes and shut down"""
    await registry.shutdown_all()
    logger.info("Shutdown requested, cleaning up processes...")
    asyncio.get_event_loop().call_later(0.5, os.kill, os.getpid(), 15)
    return {"ok": True}


# ---- Static pages ----

@app.get("/")
async def serve_frontend():
    """Serve the main UI"""
    new_html = os.path.join(os.path.dirname(__file__), "index_new.html")
    old_html = os.path.join(os.path.dirname(__file__), "index.html")
    html_path = new_html if os.path.exists(new_html) else old_html
    return FileResponse(html_path)


@app.get("/cron.html")
async def serve_cron():
    return FileResponse(os.path.join(os.path.dirname(__file__), "cron.html"))


@app.get("/cost-center.html")
async def serve_cost_center():
    return FileResponse(os.path.join(os.path.dirname(__file__), "cost-center.html"))


@app.get("/settings.html")
async def serve_settings():
    return FileResponse(os.path.join(os.path.dirname(__file__), "settings.html"))


@app.get("/favicon.svg")
async def serve_favicon():
    return FileResponse(os.path.join(os.path.dirname(__file__), "favicon.svg"), media_type="image/svg+xml")


def main():
    import uvicorn
    port = int(os.environ.get("PORT", 8089))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)


if __name__ == "__main__":
    main()
