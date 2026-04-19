"""Dev Assistant - FastAPI backend server"""

import asyncio
import json
import logging
import logging.handlers
import os
import subprocess

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
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

_SETTINGS_KEYS = [
    "TARGET_PROJECT_PATH",
    "DEFAULT_BRANCH",
    "DATA_DIR",
    "PORT",
    "GLAB_CONFIG_DIR",
    "GH_CONFIG_DIR",
]
_SETTINGS_DEFAULTS = {
    "TARGET_PROJECT_PATH": "",
    "DEFAULT_BRANCH": "master",
    "DATA_DIR": "~/.dev-assistant",
    "PORT": "8089",
    "GLAB_CONFIG_DIR": os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gitlab"),
    "GH_CONFIG_DIR": os.path.join(os.path.dirname(os.path.abspath(__file__)), ".github"),
}
_RESTART_REQUIRED_KEYS = {"PORT", "DATA_DIR", "TARGET_PROJECT_PATH"}


def _read_config() -> dict:
    if os.path.exists(_CONFIG_FILE):
        try:
            with open(_CONFIG_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _write_config(updates: dict):
    os.makedirs(os.path.dirname(_CONFIG_FILE), exist_ok=True)
    config = _read_config()
    config.update({k: v for k, v in updates.items() if v != ""})
    # Remove keys explicitly cleared
    for k, v in updates.items():
        if v == "" and k in config:
            del config[k]
    with open(_CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


def _cfg(key: str) -> str:
    """Return setting value: shell env > config.json > hardcoded default."""
    return os.environ.get(key) or _read_config().get(key) or _SETTINGS_DEFAULTS.get(key, "")


# Apply config.json to os.environ so that downstream modules (vcs_provider, etc.)
# can continue using os.environ.get() unchanged.
for _k, _v in _read_config().items():
    if _k and _v:
        os.environ.setdefault(_k, str(_v))

# Logging setup
_data_dir = os.path.expanduser(_cfg("DATA_DIR"))
os.makedirs(_data_dir, exist_ok=True)
os.makedirs(os.path.join(_data_dir, "logs"), exist_ok=True)

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
# Bootstrap managers (soft-fail if TARGET_PROJECT_PATH is missing/invalid)
# ---------------------------------------------------------------------------

_config_error: Optional[str] = None
manager: Optional[ClaudeSessionManager] = None
cron_manager: Optional[CronTaskManager] = None

_target = _cfg("TARGET_PROJECT_PATH")
if not _target:
    _config_error = "TARGET_PROJECT_PATH is not set. Open ⚙ Settings to configure it."
    logger.warning(_config_error)
elif not os.path.isdir(_target):
    _config_error = f"TARGET_PROJECT_PATH does not exist: {_target}"
    logger.warning(_config_error)
else:
    manager = ClaudeSessionManager(
        data_file=os.path.join(_data_dir, "dev-tasks.json")
    )
    cron_manager = CronTaskManager(
        session_manager=manager,
        data_file=os.path.join(_data_dir, "cron-tasks.json"),
    )


def _require_manager() -> ClaudeSessionManager:
    if manager is None:
        raise HTTPException(status_code=503, detail=_config_error or "Server not configured")
    return manager


def _require_cron_manager() -> CronTaskManager:
    if cron_manager is None:
        raise HTTPException(status_code=503, detail=_config_error or "Server not configured")
    return cron_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    if manager and cron_manager:
        await manager.start_worker()
        await cron_manager.start()
    yield
    if cron_manager:
        await cron_manager.stop()


app = FastAPI(title="Dev Assistant", version="1.0.0", lifespan=lifespan)
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
    session_id: Optional[str] = None
    use_plan_mode: bool = False


# ---- API routes ----

@app.get("/stats")
async def get_stats():
    """Return overall stats"""
    return await _require_manager().get_stats()


@app.get("/cost-stats")
async def get_cost_stats(start_date: Optional[str] = None, end_date: Optional[str] = None):
    """Return cost statistics."""
    return await _require_manager().get_cost_stats(start_date, end_date)


@app.get("/sessions")
async def list_sessions():
    """List all sessions"""
    sessions = await _require_manager().list_sessions()
    return [s.to_dict() for s in sessions]


@app.post("/sessions", status_code=201)
async def create_session(req: CreateSessionRequest):
    """Create a session and enqueue it"""
    mgr = _require_manager()
    session_id = await mgr.create_session(req.prompt, req.session_id, req.use_plan_mode)
    logger.info("Session created: %s | plan_mode: %s | prompt: %.80s", session_id, req.use_plan_mode, req.prompt)
    session = await mgr.get_session(session_id)
    return session.to_dict()


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    """Get a single session"""
    session = await _require_manager().get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict()


@app.post("/sessions/{session_id}/stop")
async def stop_session(session_id: str):
    """Cancel a session"""
    mgr = _require_manager()
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
    """Mark a session as completed (review → completed)"""
    ok = await _require_manager().complete_session(session_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Session not in review status")
    logger.info("Session completed: %s", session_id)
    return {"ok": True}


@app.post("/sessions/{session_id}/happy")
async def start_happy(session_id: str):
    """Start a happy session in the worktree"""
    mgr = _require_manager()
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
    mgr = _require_manager()
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    ok = await mgr.stop_happy(session_id)
    return {"ok": ok}


@app.get("/sessions/{session_id}/happy")
async def get_happy_status(session_id: str):
    """Get happy session status"""
    mgr = _require_manager()
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return mgr.get_happy_status(session_id)


@app.post("/sessions/{session_id}/plan/message")
async def send_plan_message(session_id: str, req: dict):
    """Send a message to the plan session"""
    mgr = _require_manager()
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
    mgr = _require_manager()
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    messages = await mgr.get_plan_messages(session_id, offset)
    return {"messages": messages, "total": len(messages)}


@app.post("/sessions/{session_id}/plan/confirm")
async def confirm_plan(session_id: str):
    """Confirm the plan and start development"""
    mgr = _require_manager()
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
    ok = await _require_manager().delete_session(session_id)
    if not ok:
        raise HTTPException(status_code=400, detail="Session not found or still active")
    return {"ok": True}


@app.get("/sessions/{session_id}/logs")
async def get_logs(session_id: str, limit: int = 0):
    """Get session logs"""
    mgr = _require_manager()
    session = await mgr.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return await mgr.get_session_logs(session_id, limit)


@app.get("/sessions/{session_id}/stream")
async def stream_logs(session_id: str):
    """SSE real-time log stream for a session"""
    mgr = _require_manager()
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


@app.get("/cron-tasks")
async def list_cron_tasks():
    """List all cron tasks"""
    return [t.to_dict() for t in _require_cron_manager().list_tasks()]


@app.post("/cron-tasks", status_code=201)
async def create_cron_task(req: dict):
    """Create a cron task"""
    try:
        task = _require_cron_manager().create_task(
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
    logger.info("Cron task created: %s (%s)", task.name, task.id)
    return task.to_dict()


@app.put("/cron-tasks/{task_id}")
async def update_cron_task(task_id: str, req: dict):
    """Update a cron task"""
    allowed = {"name", "prompt", "cron_expr", "enabled", "max_open",
               "mr_labels", "use_plan_mode"}
    updates = {k: v for k, v in req.items() if k in allowed}
    try:
        task = _require_cron_manager().update_task(task_id, **updates)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid cron expression: {e}")
    if not task:
        raise HTTPException(status_code=404, detail="Cron task not found")
    logger.info("Cron task updated: %s (%s)", task.name, task_id)
    return task.to_dict()


@app.delete("/cron-tasks/{task_id}")
async def delete_cron_task(task_id: str):
    """Delete a cron task"""
    ok = _require_cron_manager().delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cron task not found")
    logger.info("Cron task deleted: %s", task_id)
    return {"ok": True}


@app.post("/cron-tasks/{task_id}/trigger")
async def trigger_cron_task(task_id: str):
    """Trigger a cron task immediately"""
    ok = await _require_cron_manager().trigger_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Cron task not found")
    logger.info("Cron task triggered manually: %s", task_id)
    return {"ok": True}


@app.post("/admin/shutdown")
async def shutdown():
    """Clean up all active processes and shut down"""
    if manager:
        await manager.shutdown()
    logger.info("Shutdown requested, cleaning up processes...")
    asyncio.get_event_loop().call_later(0.5, os.kill, os.getpid(), 15)
    return {"ok": True}


# ---- Settings endpoints ----

@app.get("/settings")
async def get_settings():
    """Return current configuration values from config.json"""
    file_config = _read_config()
    result = {k: file_config.get(k, v) for k, v in _SETTINGS_DEFAULTS.items()}
    result["_config_error"] = _config_error
    result["_restart_required_keys"] = list(_RESTART_REQUIRED_KEYS)
    return result


@app.post("/settings")
async def update_settings(req: dict):
    """Save configuration to config.json; returns whether a restart is required"""
    updates = {k: str(v) for k, v in req.items() if k in _SETTINGS_KEYS}
    _write_config(updates)
    for key in ("DEFAULT_BRANCH", "GLAB_CONFIG_DIR", "GH_CONFIG_DIR"):
        if key in updates:
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
            # Look for any "Logged in to" line — glab exits 1 when even one
            # configured instance fails, but others may still be authenticated.
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


@app.get("/")
async def serve_frontend():
    """Serve the main UI"""
    # Prefer index_new.html if present, fall back to index.html
    new_html = os.path.join(os.path.dirname(__file__), "index_new.html")
    old_html = os.path.join(os.path.dirname(__file__), "index.html")
    html_path = new_html if os.path.exists(new_html) else old_html
    return FileResponse(html_path)


@app.get("/cron.html")
async def serve_cron():
    """Serve the cron jobs page"""
    cron_path = os.path.join(os.path.dirname(__file__), "cron.html")
    return FileResponse(cron_path)


@app.get("/cost-center.html")
async def serve_cost_center():
    """Serve the cost center page"""
    cost_center_path = os.path.join(os.path.dirname(__file__), "cost-center.html")
    return FileResponse(cost_center_path)


@app.get("/settings.html")
async def serve_settings():
    """Serve the settings page"""
    settings_path = os.path.join(os.path.dirname(__file__), "settings.html")
    return FileResponse(settings_path)


@app.get("/favicon.svg")
async def serve_favicon():
    """Serve the favicon"""
    favicon_path = os.path.join(os.path.dirname(__file__), "favicon.svg")
    return FileResponse(favicon_path, media_type="image/svg+xml")


def main():
    import uvicorn
    port = int(os.environ.get("PORT", 8089))
    uvicorn.run("server:app", host="0.0.0.0", port=port, reload=True)


if __name__ == "__main__":
    main()
