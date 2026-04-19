"""Cron task manager — schedules tasks using cron expressions"""

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Optional

from croniter import croniter

logger = logging.getLogger(__name__)

DEFAULT_TASKS = [
    {
        "id": "00000000-0000-0000-0000-000000000001",
        "name": "Auto refactor",
        "prompt": "/refactor Scan the entire codebase systematically and find areas that need improvement. This is an automated maintenance script — decide what to improve yourself, don't ask, just do it.",
        "cron_expr": "0 * * * *",
        "enabled": True,
        "max_open": 3,
        "mr_labels": ["refactor"],
        "use_plan_mode": False,
        "last_run_at": None,
        "next_run_at": None,
        "created_at": 0.0,
    },
    {
        "id": "00000000-0000-0000-0000-000000000002",
        "name": "Auto docs",
        "prompt": "/docs-maintain",
        "cron_expr": "0 0 * * *",
        "enabled": True,
        "max_open": 1,
        "mr_labels": ["docs"],
        "use_plan_mode": False,
        "last_run_at": None,
        "next_run_at": None,
        "created_at": 0.0,
    },
]


@dataclass
class CronTask:
    id: str
    name: str
    prompt: str
    cron_expr: str
    enabled: bool
    max_open: int
    mr_labels: list
    use_plan_mode: bool
    last_run_at: Optional[float]
    next_run_at: Optional[float]
    created_at: float

    def to_dict(self) -> dict:
        d = asdict(self)
        d["next_run_str"] = (
            datetime.fromtimestamp(self.next_run_at).strftime("%m-%d %H:%M")
            if self.next_run_at else None
        )
        d["last_run_str"] = (
            datetime.fromtimestamp(self.last_run_at).strftime("%m-%d %H:%M")
            if self.last_run_at else None
        )
        return d

    def to_storage_dict(self) -> dict:
        return asdict(self)


class CronTaskManager:
    def __init__(self, session_manager, data_file: str):
        self.session_manager = session_manager
        self.data_file = data_file
        self.tasks: dict[str, CronTask] = {}
        self._loop_task: Optional[asyncio.Task] = None
        self._load()

    def _load(self):
        try:
            with open(self.data_file) as f:
                data = json.load(f)
            for item in data:
                fields = {k: item.get(k) for k in CronTask.__dataclass_fields__}
                # Backwards compat: old field name was max_concurrent
                if fields.get("max_open") is None:
                    fields["max_open"] = item.get("max_concurrent", 1)
                task = CronTask(**fields)
                if task.enabled and task.next_run_at is None:
                    task.next_run_at = self._compute_next(task.cron_expr)
                self.tasks[task.id] = task
            logger.info(f"Loaded {len(self.tasks)} cron tasks")
        except FileNotFoundError:
            logger.info("Cron tasks file not found, creating defaults")
            now = time.time()
            for item in DEFAULT_TASKS:
                fields = {k: item[k] for k in CronTask.__dataclass_fields__}
                task = CronTask(**fields)
                task.created_at = now
                task.next_run_at = self._compute_next(task.cron_expr)
                self.tasks[task.id] = task
            self._save()
        except Exception as e:
            logger.error(f"Error loading cron tasks: {e}")

    def _save(self):
        data = [t.to_storage_dict() for t in self.tasks.values()]
        with open(self.data_file, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _compute_next(self, cron_expr: str, from_ts: Optional[float] = None) -> float:
        base = from_ts if from_ts is not None else time.time()
        return croniter(cron_expr, base).get_next(float)

    async def start(self):
        self._loop_task = asyncio.create_task(self._scheduler_loop())
        logger.info("CronTaskManager started")

    async def stop(self):
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        logger.info("CronTaskManager stopped")

    async def _scheduler_loop(self):
        """Check for due tasks every 30 seconds"""
        while True:
            try:
                now = time.time()
                for task in list(self.tasks.values()):
                    if not task.enabled:
                        continue
                    if task.next_run_at and now >= task.next_run_at:
                        logger.info(f"Triggering cron task: {task.name}")
                        await self._execute_task(task)
                        task.last_run_at = now
                        task.next_run_at = self._compute_next(task.cron_expr, now)
                        self._save()
            except Exception as e:
                logger.error(f"Scheduler loop error: {e}")
            await asyncio.sleep(30)

    async def _execute_task(self, task: CronTask):
        try:
            sessions = await self.session_manager.list_sessions()
            active_count = sum(
                1 for s in sessions
                if self._is_matching_session(s, task)
                and s.status not in ["completed", "failed", "cancelled"]
            )
            if active_count >= task.max_open:
                logger.info(f"Skipped {task.name}: {active_count}/{task.max_open} open")
                return
            session_id = await self.session_manager.create_session(
                prompt=task.prompt,
                use_plan_mode=task.use_plan_mode,
                is_refactor=False,  # cron tasks are never classified as refactor
                mr_labels=task.mr_labels,
                source_cron_task_id=task.id,
            )
            logger.info(f"Cron task '{task.name}' created session: {session_id}")
        except Exception as e:
            logger.error(f"Error executing cron task '{task.name}': {e}")

    def _is_matching_session(self, session, task: CronTask) -> bool:
        return session.source_cron_task_id == task.id

    # ---- CRUD ----

    def list_tasks(self) -> list[CronTask]:
        return list(self.tasks.values())

    def get_task(self, task_id: str) -> Optional[CronTask]:
        return self.tasks.get(task_id)

    def create_task(
        self,
        name: str,
        prompt: str,
        cron_expr: str,
        enabled: bool = True,
        max_open: int = 1,
        mr_labels: Optional[list] = None,
        use_plan_mode: bool = False,
    ) -> CronTask:
        # Validate cron expression
        croniter(cron_expr)  # raises ValueError if invalid
        task = CronTask(
            id=str(uuid.uuid4()),
            name=name,
            prompt=prompt,
            cron_expr=cron_expr,
            enabled=enabled,
            max_open=max_open,
            mr_labels=mr_labels or [],
            use_plan_mode=use_plan_mode,
            last_run_at=None,
            next_run_at=self._compute_next(cron_expr) if enabled else None,
            created_at=time.time(),
        )
        self.tasks[task.id] = task
        self._save()
        return task

    def update_task(self, task_id: str, **kwargs) -> Optional[CronTask]:
        task = self.tasks.get(task_id)
        if not task:
            return None
        if "cron_expr" in kwargs:
            croniter(kwargs["cron_expr"])  # validate
        for k, v in kwargs.items():
            if k in CronTask.__dataclass_fields__ and k not in ("id", "created_at"):
                setattr(task, k, v)
        # Recompute next_run_at when cron_expr or enabled changes
        if "cron_expr" in kwargs or "enabled" in kwargs:
            task.next_run_at = self._compute_next(task.cron_expr) if task.enabled else None
        self._save()
        return task

    def delete_task(self, task_id: str) -> bool:
        if task_id not in self.tasks:
            return False
        del self.tasks[task_id]
        self._save()
        return True

    async def trigger_task(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if not task:
            return False
        await self._execute_task(task)
        task.last_run_at = time.time()
        self._save()
        return True
