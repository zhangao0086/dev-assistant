"""Dev Assistant - manages multiple Claude CLI processes and automated tasks"""

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, Optional

from vcs_provider import VCSProvider


class SessionStatus(str, Enum):
    """Session status values"""
    PENDING = "pending"      # queued, waiting for a worktree
    PLANNING = "planning"    # in plan-mode conversation
    DEVELOPING = "developing"  # claude process running
    COMMITTING = "committing"  # running finishing-feature skill
    REVIEW = "review"        # MR/PR open, awaiting human review
    COMPLETED = "completed"  # merged / manually marked done
    FAILED = "failed"        # process exited with error
    CANCELLED = "cancelled"  # cancelled by user or MR closed


@dataclass
class SessionLog:
    """A single log entry for a session"""
    timestamp: float
    level: str  # stdout, stderr, error, info
    content: str


@dataclass
class ClaudeSession:
    """Claude session data model"""
    session_id: str
    prompt: str
    status: SessionStatus
    created_at: float
    log_file: str = ""
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    process: Optional[subprocess.Popen] = None
    exit_code: Optional[int] = None
    worktree_path: Optional[str] = None
    branch_name: Optional[str] = None
    mr_url: Optional[str] = None
    mr_number: Optional[str] = None
    happy_pid: Optional[int] = None       # in-memory only, not persisted
    happy_window: Optional[str] = None    # tmux session name, in-memory only
    happy_session_id: Optional[str] = None  # used to build the Web URL
    use_plan_mode: bool = False
    plan_process: Optional[subprocess.Popen] = None  # in-memory only
    plan_confirmed: bool = False
    is_refactor: bool = False
    mr_labels: Optional[list] = None
    source_cron_task_id: Optional[str] = None
    mr_approved: Optional[bool] = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_usd: float = 0.0

    def add_log(self, level: str, content: str):
        """Append a log entry to the session log file"""
        if not self.log_file:
            return

        log_dir = os.path.dirname(self.log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        entry = {
            "id": str(uuid.uuid4()),
            "timestamp": time.time(),
            "level": level,
            "content": content
        }
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_logs(self, offset: int = 0, limit: int = 0) -> list:
        """Read log entries from the log file"""
        if not self.log_file or not os.path.exists(self.log_file):
            return []
        logs = []
        with open(self.log_file, encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < offset:
                    continue
                line = line.strip()
                if line:
                    try:
                        logs.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
                if limit and len(logs) >= limit:
                    break
        return logs

    def to_dict(self) -> dict:
        """Serialize to dict for API responses"""
        log_count = 0
        if self.log_file and os.path.exists(self.log_file):
            with open(self.log_file, encoding="utf-8") as f:
                log_count = sum(1 for _ in f)
        elapsed = None
        if self.started_at:
            end = self.completed_at if self.completed_at else time.time()
            elapsed = round(end - self.started_at)
        return {
            "session_id": self.session_id,
            "prompt": self.prompt,
            "status": self.status.value,
            "created_at": datetime.fromtimestamp(self.created_at).isoformat(),
            "started_at": datetime.fromtimestamp(self.started_at).isoformat() if self.started_at else None,
            "completed_at": datetime.fromtimestamp(self.completed_at).isoformat() if self.completed_at else None,
            "elapsed_seconds": elapsed,
            "exit_code": self.exit_code,
            "log_count": log_count,
            "worktree_path": self.worktree_path,
            "branch_name": self.branch_name,
            "mr_url": self.mr_url,
            "mr_number": self.mr_number,
            "mr_approved": self.mr_approved,
            "happy_window": self.happy_window,
            "happy_session_id": self.happy_session_id,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost_usd": round(self.total_cost_usd, 4),
        }


class ClaudeSessionManager:
    """Manages Claude CLI processes and session lifecycle"""

    def __init__(self, target_repo: str = "", data_file: str = ""):
        if not target_repo:
            target_repo = os.environ.get("TARGET_PROJECT_PATH", "")
        if not target_repo:
            raise ValueError(
                "target_repo is required. Set TARGET_PROJECT_PATH environment variable "
                "or pass target_repo parameter."
            )
        if not data_file:
            data_dir = os.path.expanduser(os.environ.get("DATA_DIR", "~/.dev-assistant"))
            data_file = os.path.join(data_dir, "dev-tasks.json")
        self.sessions: Dict[str, ClaudeSession] = {}
        self.target_repo = target_repo
        self.default_branch = os.environ.get("DEFAULT_BRANCH", "master")
        self.worktrees_dir = os.path.join(target_repo, ".worktrees")
        self._data_file = data_file
        self._logs_dir = os.path.join(os.path.dirname(os.path.abspath(data_file)), "logs")
        self._lock = asyncio.Lock()
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: Optional[asyncio.Task] = None
        self._mr_checker_task: Optional[asyncio.Task] = None
        self.vcs: VCSProvider = VCSProvider.detect(target_repo)

        os.makedirs(self.worktrees_dir, exist_ok=True)
        os.makedirs(os.path.dirname(os.path.abspath(data_file)), exist_ok=True)
        os.makedirs(self._logs_dir, exist_ok=True)
        self._load_sessions()

    async def start_worker(self):
        """Start the queue worker and MR status checker"""
        self._worker_task = asyncio.create_task(self._queue_worker())
        self._mr_checker_task = asyncio.create_task(self._mr_status_checker())

    def _session_to_persist(self, session: "ClaudeSession") -> dict:
        """Serialize a session for storage (excludes subprocess handles)"""
        return {
            "session_id": session.session_id,
            "prompt": session.prompt,
            "status": session.status.value,
            "created_at": session.created_at,
            "started_at": session.started_at,
            "completed_at": session.completed_at,
            "exit_code": session.exit_code,
            "worktree_path": session.worktree_path,
            "branch_name": session.branch_name,
            "mr_url": session.mr_url,
            "mr_number": session.mr_number,
            "happy_session_id": session.happy_session_id,
            "use_plan_mode": session.use_plan_mode,
            "plan_confirmed": session.plan_confirmed,
            "is_refactor": session.is_refactor,
            "source_cron_task_id": session.source_cron_task_id,
            "total_input_tokens": session.total_input_tokens,
            "total_output_tokens": session.total_output_tokens,
            "total_cost_usd": session.total_cost_usd,
        }

    def _persist(self):
        """Atomically write session state to JSON (call inside lock)"""
        data = [self._session_to_persist(s) for s in self.sessions.values()]
        tmp = self._data_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._data_file)

    def _load_sessions(self):
        """Restore sessions from the JSON data file on startup"""
        if not os.path.exists(self._data_file):
            return
        try:
            with open(self._data_file, encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                status_str = item["status"]
                # Backwards compat: "merged" was renamed to "completed"
                if status_str == "merged":
                    status_str = "completed"
                status = SessionStatus(status_str)
                # After a server restart, developing/committing processes are dead — mark failed.
                # Planning sessions are preserved so the conversation can be resumed.
                if status in (SessionStatus.DEVELOPING, SessionStatus.COMMITTING):
                    status = SessionStatus.FAILED
                session = ClaudeSession(
                    session_id=item["session_id"],
                    prompt=item["prompt"],
                    status=status,
                    created_at=item["created_at"],
                    log_file=os.path.join(self._logs_dir, f"{item['session_id']}.jsonl"),
                    started_at=item.get("started_at"),
                    completed_at=item.get("completed_at"),
                    exit_code=item.get("exit_code"),
                    worktree_path=item.get("worktree_path"),
                    branch_name=item.get("branch_name"),
                    mr_url=item.get("mr_url"),
                    mr_number=item.get("mr_number"),
                    happy_session_id=item.get("happy_session_id"),
                    use_plan_mode=item.get("use_plan_mode", False),
                    plan_confirmed=item.get("plan_confirmed", False),
                    is_refactor=item.get("is_refactor", False),
                    source_cron_task_id=item.get("source_cron_task_id"),
                    total_input_tokens=item.get("total_input_tokens", 0),
                    total_output_tokens=item.get("total_output_tokens", 0),
                    total_cost_usd=item.get("total_cost_usd", 0.0),
                )
                # Restore happy tmux session reference
                if session.happy_session_id and status == SessionStatus.REVIEW:
                    tmux_session_name = f"happy-{session.session_id[:8]}"
                    if self._tmux_session_alive(tmux_session_name):
                        session.happy_window = tmux_session_name
                    else:
                        session.happy_session_id = None
                self.sessions[session.session_id] = session
        except Exception as e:
            print(f"Warning: failed to load sessions from {self._data_file}: {e}")

    async def _queue_worker(self):
        """Process worktree creation serially; claude processes run in parallel"""
        while True:
            session_id = await self._queue.get()
            session = self.sessions.get(session_id)
            if session and session.status != SessionStatus.CANCELLED:
                await self._prepare_and_launch(session_id)
            self._queue.task_done()

    async def _mr_status_checker(self):
        """Periodically check MR status and auto-recover missing MR info"""
        while True:
            await asyncio.sleep(60)

            # 1. Auto-recover sessions that have a branch but no MR URL
            recover_candidates = [
                s for s in self.sessions.values()
                if not s.mr_url and s.branch_name and s.status not in [SessionStatus.COMPLETED, SessionStatus.CANCELLED]
            ]
            for session in recover_candidates:
                try:
                    mrs = await asyncio.to_thread(self.vcs.list_mr_by_branch, session.branch_name)
                    if mrs:
                        mr = mrs[0]
                        async with self._lock:
                            session.mr_url = mr.url
                            session.mr_number = mr.number
                            # If still in DEVELOPING/COMMITTING etc., advance to REVIEW
                            if session.status != SessionStatus.REVIEW:
                                session.status = SessionStatus.REVIEW

                            # Update task title to match the MR title
                            if mr.title:
                                if session.is_refactor:
                                    session.prompt = f"[Refactor] {mr.title}"
                                elif session.prompt.startswith("/docs-maintain"):
                                    session.prompt = f"[Docs] {mr.title}"
                                else:
                                    session.prompt = mr.title
                                session.add_log("info", f"Updated task title to: {session.prompt}")

                            session.add_log("info", f"Auto-recovered missing MR info: !{mr.number} ({mr.url})")
                            self._persist()
                except Exception:
                    pass

            # 2. Check MR status for sessions in REVIEW
            review_sessions = [
                s for s in self.sessions.values()
                if s.status == SessionStatus.REVIEW and s.mr_url
            ]

            for session in review_sessions:
                try:
                    # Extract MR ID from URL
                    mr_id = self.vcs.extract_mr_id(session.mr_url)
                    if not mr_id:
                        continue

                    # Fetch MR status
                    mr_info = await asyncio.to_thread(self.vcs.get_mr, mr_id)
                    if mr_info is None:
                        continue

                    state = mr_info.state

                    # Sync MR title back to task prompt
                    if mr_info.title:
                        if session.is_refactor:
                            new_prompt = f"[Refactor] {mr_info.title}"
                        elif session.prompt.startswith("/docs-maintain"):
                            new_prompt = f"[Docs] {mr_info.title}"
                        else:
                            new_prompt = mr_info.title
                        if session.prompt != new_prompt:
                            async with self._lock:
                                session.prompt = new_prompt
                                self._persist()
                            session.add_log("info", f"Synced task title to: {session.prompt}")

                    # Fetch approval status
                    new_approved = await asyncio.to_thread(self.vcs.get_mr_approved, mr_id)
                    if session.mr_approved != new_approved:
                        async with self._lock:
                            session.mr_approved = new_approved
                            self._persist()

                    if state == "merged":
                        async with self._lock:
                            session.status = SessionStatus.COMPLETED
                            session.add_log("info", f"MR {mr_id} has been merged, auto-completing task")
                            self._persist()

                        await self._cleanup_worktree(session)

                    elif state == "closed":
                        async with self._lock:
                            session.status = SessionStatus.CANCELLED
                            session.add_log("info", f"MR {mr_id} has been closed, auto-cancelling task")
                            self._persist()

                        await self._cleanup_worktree(session)

                except Exception:
                    pass  # Fail silently; don't affect other sessions

    async def _prepare_and_launch(self, session_id: str):
        """Create a worktree serially, then launch the claude process asynchronously"""
        session = self.sessions.get(session_id)
        if not session:
            return

        try:
            branch_name = f"task-{session_id[:8]}"
            worktree_path = os.path.join(self.worktrees_dir, branch_name)

            session.add_log("info", f"Updating {self.default_branch} branch before creating worktree")
            pull_result = await asyncio.to_thread(
                subprocess.run,
                ["git", "pull", "origin", self.default_branch],
                cwd=self.target_repo,
                capture_output=True,
                text=True,
            )
            if pull_result.returncode != 0:
                session.add_log("warning", f"git pull failed (proceeding anyway): {pull_result.stderr}")
            else:
                session.add_log("info", f"git pull: {pull_result.stdout.strip()}")

            session.add_log("info", f"Creating worktree: {worktree_path}")
            session.add_log("info", f"Branch: {branch_name}")

            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "worktree", "add", "-b", branch_name, worktree_path],
                cwd=self.target_repo,
                capture_output=True,
                text=True,
            )

            if result.returncode != 0:
                raise Exception(f"Failed to create worktree: {result.stderr}")

            session.add_log("info", "Worktree created successfully")

            async with self._lock:
                session.worktree_path = worktree_path
                session.branch_name = branch_name
                self._persist()

        except Exception as e:
            async with self._lock:
                session.status = SessionStatus.FAILED
                session.completed_at = time.time()
                self._persist()
            session.add_log("error", f"Failed to create worktree: {e}")
            return

        # Worktree is ready — start plan mode or go straight to development
        if session.use_plan_mode:
            asyncio.create_task(self._start_planning_session(session_id))
        else:
            asyncio.create_task(self._run_session(session_id))

    async def create_session(self, prompt: str, session_id: Optional[str] = None, use_plan_mode: bool = False, is_refactor: bool = False, mr_labels: Optional[list] = None, source_cron_task_id: Optional[str] = None) -> str:
        """Create a new session and enqueue it"""
        # For refactor tasks, enforce a cap on concurrent active sessions
        if is_refactor:
            async with self._lock:
                active_refactor_count = sum(
                    1 for s in self.sessions.values()
                    if s.is_refactor and s.status not in [SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED]
                )
                if active_refactor_count >= 3:
                    raise Exception(f"Already have {active_refactor_count} active refactor tasks, max is 3")

        if session_id is None:
            session_id = str(uuid.uuid4())

        session = ClaudeSession(
            session_id=session_id,
            prompt=prompt,
            status=SessionStatus.PENDING,
            created_at=time.time(),
            log_file=os.path.join(self._logs_dir, f"{session_id}.jsonl"),
            use_plan_mode=use_plan_mode,
            is_refactor=is_refactor,
            mr_labels=mr_labels,
            source_cron_task_id=source_cron_task_id,
        )

        async with self._lock:
            self.sessions[session_id] = session
            self._persist()

        session.add_log("info", f"Session created with prompt: {prompt[:100]}...")
        if use_plan_mode:
            session.add_log("info", "Plan mode enabled")
        if is_refactor:
            session.add_log("info", "Refactor task")
        if mr_labels:
            session.add_log("info", f"MR labels: {mr_labels}")
        await self._queue.put(session_id)
        return session_id

    async def _start_planning_session(self, session_id: str) -> bool:
        """Start a plan-mode conversation session"""
        async with self._lock:
            session = self.sessions.get(session_id)
            if not session or session.status != SessionStatus.PENDING:
                return False
            session.status = SessionStatus.PLANNING
            session.started_at = time.time()
            self._persist()

        session = self.sessions.get(session_id)
        worktree_path = session.worktree_path

        try:
            claude_dir = os.path.join(worktree_path, ".claude")
            os.makedirs(claude_dir, exist_ok=True)
            session.add_log("info", f"Created .claude directory: {claude_dir}")

            session.add_log("info", f"Claude session ID: {session_id}")

            # Initial plan prompt
            initial_prompt = f"""You are in Plan mode. Have a multi-turn conversation with the user to clarify:

1. **Task background and goal**: understand why the user wants this done and what success looks like.
2. **Implementation approach**: discuss the technical plan, files to touch, key steps, etc.

**Constraints**:
- In Plan mode you may only research, analyse, and plan — do NOT modify any code files.
- Do not run git commit, git push, or similar commands.
- The only file you are allowed to write is `.claude/PLAN.md`.

The user's original task description:
{session.prompt}

Ask clarifying questions and confirm the approach with the user. Once you both agree on the plan, write the complete plan to `.claude/PLAN.md`, including:
- Task background and goal
- Implementation approach and steps

Then tell the user the plan is ready and wait for confirmation."""

            session.add_log("plan_user", initial_prompt)
            await self._send_plan_request(session_id, initial_prompt)
            return True

        except Exception as e:
            async with self._lock:
                session.status = SessionStatus.FAILED
                session.completed_at = time.time()
                self._persist()
            session.add_log("error", f"Failed to start plan session: {e}")
            await self._cleanup_worktree(session)
            return False

    async def _send_plan_request(self, session_id: str, user_message: str, allow_write: bool = False):
        """Send a plan request to Claude"""
        session = self.sessions.get(session_id)
        if not session or not session.worktree_path:
            return

        worktree_path = session.worktree_path

        try:
            # First request uses --session-id to create a new session;
            # subsequent requests use --resume to continue the same session.
            logs = session.read_logs(offset=0)
            has_assistant_response = any(log.get("level") == "plan_assistant" for log in logs)

            # confirm_plan needs to write PLAN.md, so we use bypassPermissions.
            # During normal planning we use dontAsk to prevent code changes.
            permission_mode = "bypassPermissions" if allow_write else "dontAsk"

            cmd = [
                "claude",
                "--print",
                user_message,
                "--output-format", "stream-json",
                "--permission-mode", permission_mode,
                # Plan stage: read/search only; Write is restricted to PLAN.md
                "--allowedTools", "Read", "Glob", "Grep", "Write",
                "--verbose",
            ]

            # Plan stage uses a separate session ID (last 4 chars replaced with "0000")
            plan_session_id = session_id[:-4] + "0000"
            if not has_assistant_response:
                cmd.extend(["--session-id", plan_session_id])
                session.add_log("info", f"Creating new Claude plan session: {plan_session_id}")
            else:
                cmd.extend(["--resume", plan_session_id])
                session.add_log("info", f"Resuming Claude plan session: {plan_session_id}")

            session.add_log("info", "Sending plan request...")

            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            env["ANTHROPIC_SKIP_TOKEN_COUNT"] = "1"

            result = await asyncio.to_thread(
                subprocess.run,
                cmd,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                env=env,
            )

            if result.stdout:
                # Merge content blocks that share the same message.id
                messages_by_id = {}
                for line in result.stdout.strip().splitlines():
                    if line.strip():
                        try:
                            msg = json.loads(line)
                            msg_type = msg.get("type")

                            # Skip system init and result summary messages
                            if msg_type == "system" and msg.get("subtype") == "init":
                                continue
                            if msg_type == "result":
                                continue

                            msg_id = msg.get("message", {}).get("id")
                            if msg_id:
                                if msg_id not in messages_by_id:
                                    messages_by_id[msg_id] = msg
                                else:
                                    existing = messages_by_id[msg_id]
                                    new_content = msg.get("message", {}).get("content", [])
                                    if new_content:
                                        existing.setdefault("message", {}).setdefault("content", []).extend(new_content)
                            else:
                                session.add_log("plan_assistant", line)
                        except json.JSONDecodeError:
                            session.add_log("plan_assistant", line)

                # Record merged messages
                for msg in messages_by_id.values():
                    session.add_log("plan_assistant", json.dumps(msg))

            if result.stderr:
                session.add_log("plan_stderr", result.stderr)

            if result.returncode != 0:
                session.add_log("error", f"Plan request failed with exit code: {result.returncode}")

        except Exception as e:
            session.add_log("error", f"Error sending plan request: {e}")

    async def send_plan_message(self, session_id: str, message: str) -> bool:
        """Send a user message to the plan session"""
        session = self.sessions.get(session_id)
        if not session or session.status != SessionStatus.PLANNING:
            return False

        session.add_log("plan_user", message)
        await self._send_plan_request(session_id, message)
        return True

    async def get_plan_messages(self, session_id: str, offset: int = 0) -> list:
        """Return plan conversation history"""
        session = self.sessions.get(session_id)
        if not session:
            return []

        # Read all logs and filter to plan-related entries
        logs = session.read_logs(offset=0)
        messages = []
        for log in logs:
            level = log.get("level", "")
            if level in ["plan_user", "plan_assistant", "plan_stderr"]:
                messages.append({
                    "id": log.get("id", str(uuid.uuid4())),
                    "timestamp": log.get("timestamp"),
                    "role": "user" if level == "plan_user" else "assistant" if level == "plan_assistant" else "system",
                    "content": log.get("content", ""),
                })

        return messages[offset:]

    async def confirm_plan(self, session_id: str) -> bool:
        """Confirm the plan and transition to development"""
        try:
            session = self.sessions.get(session_id)
            if not session:
                return False
            if session.status != SessionStatus.PLANNING:
                return False

            # Ask Claude to write the agreed plan to PLAN.md
            write_plan_message = """Based on our conversation, please write the complete implementation plan to `.claude/PLAN.md`.

The plan should include:
1. Task background and goal
2. Implementation approach and key steps
3. List of files to modify"""

            session.add_log("plan_user", write_plan_message)

            # Record current log count to detect when Claude has responded
            logs_before = len(session.read_logs(offset=0))

            await self._send_plan_request(session_id, write_plan_message, allow_write=True)

            # Wait for Claude to respond and write PLAN.md (up to 120 s)
            plan_file = os.path.join(session.worktree_path, ".claude", "PLAN.md")
            max_wait = 120
            wait_interval = 2
            elapsed = 0

            while elapsed < max_wait:
                await asyncio.sleep(wait_interval)
                elapsed += wait_interval

                # New log entries mean Claude has responded
                logs_after = len(session.read_logs(offset=0))
                if logs_after > logs_before:
                    await asyncio.sleep(2)
                    if os.path.exists(plan_file):
                        session.add_log("info", "PLAN.md created successfully")
                        break
            else:
                session.add_log("error", "Timeout waiting for PLAN.md to be created")
                return False
            if not os.path.exists(plan_file):
                session.add_log("error", "PLAN.md file not found after confirmation")
                return False

            async with self._lock:
                session.plan_confirmed = True
                session.status = SessionStatus.PENDING  # reset to PENDING, ready for development
                self._persist()

            session.add_log("info", "Plan confirmed, starting development...")
            asyncio.create_task(self._run_session(session_id))
            return True
        except Exception as e:
            session = self.sessions.get(session_id)
            if session:
                session.add_log("error", f"Failed to confirm plan: {e}")
            return False

    async def _run_session(self, session_id: str) -> bool:
        """Run the claude development process (worktree already created)"""
        async with self._lock:
            session = self.sessions.get(session_id)
            if not session or session.status != SessionStatus.PENDING:
                return False
            session.status = SessionStatus.DEVELOPING
            session.started_at = time.time()
            self._persist()

        session = self.sessions.get(session_id)
        worktree_path = session.worktree_path

        try:
            # In plan-confirmed mode, replace the prompt with a reference to PLAN.md
            prompt = session.prompt
            if session.use_plan_mode and session.plan_confirmed:
                plan_file = os.path.join(worktree_path, ".claude", "PLAN.md")
                if os.path.exists(plan_file):
                    prompt = f"""Please implement the task according to the plan in `.claude/PLAN.md`.

Original task description:
{session.prompt}

Read `.claude/PLAN.md` first, then follow its steps to complete the implementation."""
                    session.add_log("info", "Using PLAN.md as development guide")

            cmd = [
                "claude",
                "--print",
                prompt,
                "--session-id", session.session_id,
                "--output-format", "stream-json",
                "--permission-mode", "dontAsk",
                "--allowedTools", "Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task", "TodoWrite", "Skill",
                "--verbose",
            ]

            session.add_log("info", f"Starting process: {' '.join(cmd)}")
            session.add_log("info", f"Working directory: {worktree_path}")

            env = os.environ.copy()
            env.pop("CLAUDECODE", None)
            env["ANTHROPIC_SKIP_TOKEN_COUNT"] = "1"

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
                cwd=worktree_path,
            )

            async with self._lock:
                session.process = process

            session.add_log("info", f"Process started with PID: {process.pid}")

            await self._monitor_session(session_id)
            return True

        except Exception as e:
            async with self._lock:
                session.status = SessionStatus.FAILED
                session.completed_at = time.time()
                self._persist()
            session.add_log("error", f"Failed to start process: {e}")
            await self._cleanup_worktree(session)
            return False

        except Exception as e:
            async with self._lock:
                session.status = SessionStatus.FAILED
                session.completed_at = time.time()
                self._persist()
            session.add_log("error", f"Failed to start process: {e}")
            await self._cleanup_worktree(session)
            return False

    async def _monitor_session(self, session_id: str):
        """Monitor the claude process output and handle completion"""
        session = self.sessions.get(session_id)
        if not session or not session.process:
            return

        process = session.process
        result_success = False

        # Read stdout, parse stream-json to detect task success
        def read_stdout():
            nonlocal result_success
            try:
                for line in iter(process.stdout.readline, ""):
                    if not line:
                        break
                    stripped = line.strip()
                    session.add_log("stdout", stripped)
                    # Parse result message
                    try:
                        msg = json.loads(stripped)
                        if msg.get("type") == "result" and msg.get("subtype") == "success":
                            result_success = True
                        # Parse token usage and cost
                        if msg.get("type") == "result":
                            usage = msg.get("usage", {})
                            if usage:
                                session.total_input_tokens += usage.get("input_tokens", 0)
                                session.total_output_tokens += usage.get("output_tokens", 0)
                            cost = msg.get("total_cost_usd", 0)
                            if cost:
                                session.total_cost_usd += cost
                    except (json.JSONDecodeError, AttributeError):
                        pass
            except Exception as e:
                session.add_log("error", f"Error reading stdout: {e}")

        # Read stderr
        def read_stderr():
            try:
                for line in iter(process.stderr.readline, ""):
                    if not line:
                        break
                    session.add_log("stderr", line.strip())
            except Exception as e:
                session.add_log("error", f"Error reading stderr: {e}")

        # Read stdout and stderr concurrently
        await asyncio.gather(
            asyncio.to_thread(read_stdout),
            asyncio.to_thread(read_stderr),
        )

        # Wait for process to exit
        exit_code = await asyncio.to_thread(process.wait)

        if exit_code == 0 and result_success:
            # Development done — commit and open MR
            await self._commit_and_create_mr(session_id)
        elif exit_code == 0 and not result_success:
            session.add_log("error", "Process exited 0 but no success result found in output")

        async with self._lock:
            session.exit_code = exit_code
            session.completed_at = time.time()
            if exit_code == 0 and result_success:
                session.status = SessionStatus.REVIEW
                session.add_log("info", "Process completed, awaiting review")
                await self.start_happy(session_id)
            else:
                session.status = SessionStatus.FAILED
                session.add_log("error", f"Process failed with exit code: {exit_code}")
            self._persist()

    async def _commit_and_create_mr(self, session_id: str):
        """Run the finishing-feature skill to commit and open a MR/PR"""
        session = self.sessions.get(session_id)
        if not session or not session.worktree_path:
            return

        async with self._lock:
            session.status = SessionStatus.COMMITTING
            self._persist()

        env = os.environ.copy()
        env.pop("CLAUDECODE", None)
        env["ANTHROPIC_SKIP_TOKEN_COUNT"] = "1"

        # Set GLAB_CONFIG_DIR so glab uses .gitlab/config.yml
        glab_config_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gitlab")
        env["GLAB_CONFIG_DIR"] = glab_config_dir

        # Prepare labels
        labels = ["dev-assistant"]
        if session.mr_labels:
            labels.extend(session.mr_labels)
        elif session.is_refactor:
            labels.append("refactor")
        elif session.prompt.startswith("/docs-maintain"):
            labels.append("docs")

        label_instruction = f"When creating MR/PR, apply the following labels: {','.join(labels)} (use --label flag)."

        session.add_log("info", f"Running finishing-feature skill with labels: {labels}")
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["claude", "--print", f"/finishing-feature {label_instruction}", "--resume", session.session_id,
                 "--permission-mode", "dontAsk",
                 "--allowedTools", "Bash", "Read", "Write", "Edit", "Glob", "Grep", "Task", "TodoWrite", "Skill"],
                cwd=session.worktree_path,
                capture_output=True,
                text=True,
                env=env,
            )
            if result.stdout:
                for line in result.stdout.strip().splitlines():
                    session.add_log("info", f"[finishing-feature] {line}")
            if result.returncode != 0:
                session.add_log("error", f"finishing-feature failed (exit {result.returncode}): {result.stderr[:200]}")
            else:
                session.add_log("info", "finishing-feature completed successfully")
                # Extract MR URL from output
                if result.stdout:
                    match = re.search(r'https://[^\s)\]]+', result.stdout)
                    if match:
                        mr_url = match.group(0)
                        mr_id = self.vcs.extract_mr_id(mr_url)
                        async with self._lock:
                            session.mr_url = mr_url
                            session.mr_number = mr_id or ""
                            self._persist()
                        if session.mr_number:
                            session.add_log("info", f"Task complete — MR !{session.mr_number} created.")
                        session.add_log("info", f"MR URL: {session.mr_url}")
                        # Update task prompt with the MR title
                        if mr_id:
                            mr_info = await asyncio.to_thread(self.vcs.get_mr, mr_id)
                            if mr_info and mr_info.title:
                                async with self._lock:
                                    if session.is_refactor:
                                        session.prompt = f"[Refactor] {mr_info.title}"
                                    elif session.prompt.startswith("/docs-maintain"):
                                        session.prompt = f"[Docs] {mr_info.title}"
                                    else:
                                        session.prompt = mr_info.title
                                    self._persist()
                                session.add_log("info", f"Updated task title to: {session.prompt}")
        except Exception as e:
            session.add_log("error", f"finishing-feature error: {e}")

    async def _cleanup_worktree(self, session: "ClaudeSession"):
        """Remove the git worktree and branch"""
        if not session.worktree_path or not session.branch_name:
            return

        await self.stop_happy(session.session_id)

        try:
            # 1. Remove worktree
            result = await asyncio.to_thread(
                subprocess.run,
                ["git", "worktree", "remove", "--force", session.worktree_path],
                cwd=self.target_repo,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                session.add_log("info", f"Worktree removed: {session.worktree_path}")
            else:
                session.add_log("error", f"Failed to remove worktree: {result.stderr}")

            # 2. Delete the branch
            branch_result = await asyncio.to_thread(
                subprocess.run,
                ["git", "branch", "-D", session.branch_name],
                cwd=self.target_repo,
                capture_output=True,
                text=True,
            )
            if branch_result.returncode == 0:
                session.add_log("info", f"Branch deleted: {session.branch_name}")
            else:
                session.add_log("error", f"Failed to delete branch: {branch_result.stderr}")

        except Exception as e:
            session.add_log("error", f"Error cleaning up worktree/branch: {e}")

    def _tmux_session_alive(self, tmux_session_name: str) -> bool:
        """Check whether a tmux session still exists"""
        try:
            result = subprocess.run(
                ["tmux", "has-session", "-t", tmux_session_name],
                capture_output=True
            )
            return result.returncode == 0
        except Exception:
            return False

    async def _add_happy_comment_to_mr(self, session: "ClaudeSession", web_url: str):
        """Post a Happy Web link as an MR/PR comment"""
        try:
            mr_id = self.vcs.extract_mr_id(session.mr_url or "")
            if not mr_id:
                return
            comment = f"🔗 Happy Web: {web_url}"
            ok = await asyncio.to_thread(self.vcs.add_mr_comment, mr_id, comment)
            if ok:
                session.add_log("info", f"Added Happy link to MR #{mr_id}")
            else:
                session.add_log("error", f"Failed to add MR comment")
        except Exception as e:
            session.add_log("error", f"Failed to add MR comment: {e}")

    async def _extract_happy_session_id(self, session_id: str, tmux_session_name: str):
        """Extract the happy session ID from tmux pane output"""
        session = self.sessions.get(session_id)
        if not session:
            return

        await asyncio.sleep(3)  # Give happy time to start up

        for _ in range(15):
            try:
                result = subprocess.run(
                    ["tmux", "capture-pane", "-t", tmux_session_name, "-p", "-S", "-100"],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    # Match [DEV] Session: <id> format
                    match = re.search(r'\[DEV\]\s+Session:\s+([a-z0-9]+)', result.stdout)
                    if match:
                        happy_session_id = match.group(1)
                        session.happy_session_id = happy_session_id
                        web_url = f"https://app.happy.engineering/session/{happy_session_id}"
                        session.add_log("info", f"Happy session id: {happy_session_id}")
                        session.add_log("info", f"Web URL: {web_url}")
                        self._persist()
                        if session.mr_url:
                            asyncio.create_task(self._add_happy_comment_to_mr(session, web_url))
                        return
            except Exception as e:
                session.add_log("error", f"Failed to extract happy session id: {e}")
                return

            await asyncio.sleep(1)

    async def start_happy(self, session_id: str) -> Optional[int]:
        """Create a dedicated tmux session and start happy, resuming the Claude session"""
        session = self.sessions.get(session_id)
        if not session or not session.worktree_path:
            return None

        tmux_session_name = f"happy-{session_id[:8]}"

        # Reuse an existing tmux session if it is still alive
        if session.happy_window and self._tmux_session_alive(session.happy_window):
            return session.happy_pid

        # Launch happy with DEBUG=1 so we can capture the session ID
        happy_cmd = f"sh -c 'env -u CLAUDECODE DEBUG=1 happy --resume {session_id}'"

        # Each task gets its own isolated tmux session
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", tmux_session_name,
             "-c", session.worktree_path,
             happy_cmd],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            session.add_log("error", f"Failed to create tmux session: {result.stderr}")
            return None

        session.happy_window = tmux_session_name
        session.happy_pid = None

        # Asynchronously extract the happy session ID from tmux output
        asyncio.create_task(self._extract_happy_session_id(session_id, tmux_session_name))

        session.add_log("info", f"Happy started in tmux session '{tmux_session_name}', resuming Claude session {session_id}")
        return -1

    async def stop_happy(self, session_id: str) -> bool:
        """Stop the happy tmux session"""
        session = self.sessions.get(session_id)
        if not session or not session.happy_window:
            return False

        try:
            subprocess.run(["tmux", "kill-session", "-t", session.happy_window],
                           capture_output=True)
            session.add_log("info", f"Happy tmux session '{session.happy_window}' killed")
        except Exception:
            pass
        session.happy_window = None
        session.happy_pid = None
        session.happy_session_id = None
        self._persist()
        return True

    def get_happy_status(self, session_id: str) -> dict:
        """Return the happy session status"""
        session = self.sessions.get(session_id)
        if not session or not session.happy_window:
            return {"running": False, "pid": None, "url": None}
        running = self._tmux_session_alive(session.happy_window)
        if not running:
            session.happy_window = None
            session.happy_pid = None
        url = f"https://app.happy.engineering/session/{session.happy_session_id}" if session.happy_session_id else None
        return {"running": running, "pid": session.happy_pid, "url": url}

    async def cancel_session(self, session_id: str) -> bool:
        """Cancel a session"""
        async with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return False

            if session.status not in [SessionStatus.PLANNING, SessionStatus.DEVELOPING, SessionStatus.PENDING, SessionStatus.COMMITTING, SessionStatus.REVIEW]:
                return False

            # Terminate the running process
            process_to_kill = None
            if session.status == SessionStatus.PLANNING and session.plan_process:
                process_to_kill = session.plan_process
            elif session.process:
                process_to_kill = session.process

            if process_to_kill:
                try:
                    process_to_kill.terminate()
                    try:
                        process_to_kill.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process_to_kill.kill()
                        process_to_kill.wait()
                except Exception as e:
                    session.add_log("error", f"Failed to terminate process: {e}")
                    return False

            session.status = SessionStatus.CANCELLED
            session.completed_at = time.time()
            session.add_log("info", "Session cancelled by user")
            self._persist()

        # Stop happy
        await self.stop_happy(session_id)

        # Close the MR/PR if one was opened
        if session.mr_url:
            await self._close_mr(session)

        await self._cleanup_worktree(session)
        return True

    async def _close_mr(self, session: "ClaudeSession"):
        """Close the MR/PR associated with this session"""
        if not session.mr_url:
            return
        try:
            mr_id = self.vcs.extract_mr_id(session.mr_url)
            if not mr_id:
                session.add_log("error", f"Cannot parse MR number from URL: {session.mr_url}")
                return

            session.add_log("info", f"Closing MR !{mr_id}...")
            ok = await asyncio.to_thread(self.vcs.close_mr, mr_id)
            if ok:
                session.add_log("info", "MR closed successfully")
            else:
                session.add_log("error", "Failed to close MR")
        except Exception as e:
            session.add_log("error", f"Error closing MR: {e}")

    async def complete_session(self, session_id: str) -> bool:
        """Manually mark a session as completed (review → completed)"""
        async with self._lock:
            session = self.sessions.get(session_id)
            if not session or session.status != SessionStatus.REVIEW:
                return False
            session.status = SessionStatus.COMPLETED
            session.add_log("info", "Session marked as completed")

            # Stop happy if still running
            if session.happy_pid:
                await self.stop_happy(session_id)

            self._persist()
            return True

    async def get_session(self, session_id: str) -> Optional[ClaudeSession]:
        """Get a single session"""
        return self.sessions.get(session_id)

    async def list_sessions(self) -> list:
        """Return all sessions"""
        return list(self.sessions.values())

    async def get_session_logs(self, session_id: str, limit: int = 0) -> list:
        """Return session logs from the log file"""
        session = self.sessions.get(session_id)
        if not session:
            return []
        return session.read_logs(limit=limit)

    async def delete_session(self, session_id: str) -> bool:
        """Delete a completed/failed/cancelled session"""
        session = None
        async with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return False

            if session.status not in [SessionStatus.COMPLETED, SessionStatus.FAILED, SessionStatus.CANCELLED]:
                return False

            del self.sessions[session_id]
            self._persist()

        await self._cleanup_worktree(session)
        return True

    async def shutdown(self):
        """Kill all active processes (called on server shutdown)"""
        for session in self.sessions.values():
            # Kill claude process
            if session.process and session.process.poll() is None:
                try:
                    session.process.terminate()
                except Exception:
                    pass
            # Kill happy tmux session
            if session.happy_window:
                try:
                    subprocess.run(["tmux", "kill-session", "-t", session.happy_window],
                                   capture_output=True)
                except Exception:
                    pass
                session.happy_window = None
                session.happy_pid = None

    async def get_stats(self) -> dict:
        """Return status counts for all sessions"""
        status_count = {}
        for status in SessionStatus:
            status_count[status.value] = sum(
                1 for s in self.sessions.values() if s.status == status
            )

        return {
            "total": len(self.sessions),
            "by_status": status_count,
        }

    async def get_cost_stats(self, start_date: Optional[str] = None, end_date: Optional[str] = None) -> dict:
        """Return cost statistics, optionally filtered by date range.

        Args:
            start_date: Lower bound (YYYY-MM-DD), or None for no lower bound
            end_date: Upper bound (YYYY-MM-DD), or None for no upper bound
        """
        from datetime import datetime, timedelta

        # Parse date range
        start_ts = None
        end_ts = None
        if start_date:
            start_ts = datetime.strptime(start_date, "%Y-%m-%d").timestamp()
        if end_date:
            # end_date is inclusive, so advance by one day
            end_ts = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).timestamp()

        # Filter sessions in range
        filtered_sessions = []
        for s in self.sessions.values():
            if start_ts and s.created_at < start_ts:
                continue
            if end_ts and s.created_at >= end_ts:
                continue
            filtered_sessions.append(s)

        # Aggregate totals
        total_cost = sum(s.total_cost_usd for s in filtered_sessions)
        total_input_tokens = sum(s.total_input_tokens for s in filtered_sessions)
        total_output_tokens = sum(s.total_output_tokens for s in filtered_sessions)

        # Group by task type
        by_type = {
            "refactor": {"count": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0},
            "docs": {"count": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0},
            "normal": {"count": 0, "cost": 0.0, "input_tokens": 0, "output_tokens": 0},
        }

        for s in filtered_sessions:
            if s.is_refactor:
                task_type = "refactor"
            elif s.prompt.startswith("/docs-maintain") or s.prompt.startswith("[Docs]"):
                task_type = "docs"
            else:
                task_type = "normal"

            by_type[task_type]["count"] += 1
            by_type[task_type]["cost"] += s.total_cost_usd
            by_type[task_type]["input_tokens"] += s.total_input_tokens
            by_type[task_type]["output_tokens"] += s.total_output_tokens

        # Daily aggregates
        daily_stats = {}
        for s in filtered_sessions:
            date_str = datetime.fromtimestamp(s.created_at).strftime("%Y-%m-%d")
            if date_str not in daily_stats:
                daily_stats[date_str] = {"cost": 0.0, "count": 0, "input_tokens": 0, "output_tokens": 0, "completed": 0}
            daily_stats[date_str]["cost"] += s.total_cost_usd
            daily_stats[date_str]["count"] += 1
            daily_stats[date_str]["input_tokens"] += s.total_input_tokens
            daily_stats[date_str]["output_tokens"] += s.total_output_tokens
            if s.status == SessionStatus.COMPLETED:
                daily_stats[date_str]["completed"] += 1

        # Top 10 most expensive tasks
        top_tasks = sorted(
            [{"session_id": s.session_id, "prompt": s.prompt, "cost": s.total_cost_usd,
              "input_tokens": s.total_input_tokens, "output_tokens": s.total_output_tokens,
              "created_at": datetime.fromtimestamp(s.created_at).isoformat()}
             for s in filtered_sessions],
            key=lambda x: x["cost"],
            reverse=True
        )[:10]

        return {
            "total_cost_usd": round(total_cost, 4),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "total_tasks": len(filtered_sessions),
            "by_type": by_type,
            "daily_stats": daily_stats,
            "top_tasks": top_tasks,
        }
