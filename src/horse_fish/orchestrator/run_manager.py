"""RunManager: multi-run queue with configurable concurrency."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from pathlib import Path
from typing import Any

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.memory.lessons import LessonStore
from horse_fish.memory.store import MemoryStore
from horse_fish.models import Run
from horse_fish.observability.log_context import setup_logging, warn_if_no_langfuse
from horse_fish.observability.traces import Tracer
from horse_fish.orchestrator.engine import Orchestrator
from horse_fish.planner.decompose import Planner
from horse_fish.store.db import Store
from horse_fish.validation.gates import ValidationGates

try:
    from horse_fish.memory.cognee_store import CogneeMemory
except ImportError:
    CogneeMemory = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = ".horse-fish/state.db"
DEFAULT_MAX_CONCURRENT_RUNS = 2
DEFAULT_MAX_AGENTS = 3
DEFAULT_POLL_INTERVAL = 2.0


def create_orchestrator(
    db_path: str,
    runtime: str = "claude",
    model: str | None = None,
    max_agents: int = 3,
    planner_runtime: str | None = None,
    project_context: str | None = None,
) -> tuple[Orchestrator, Store, AgentPool]:
    """Create a fully wired Orchestrator instance.

    This is a shared factory function used by both CLI and RunManager.
    """
    repo_root = str(Path.cwd())
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    store = Store(db_path)
    store.migrate()
    tmux = TmuxManager()
    worktrees = WorktreeManager(repo_root)
    tracer = Tracer()
    pool = AgentPool(store, tmux, worktrees, project_context=project_context, tracer=tracer)
    planner = Planner(runtime=planner_runtime or runtime, model=model if not planner_runtime else None, tracer=tracer)
    effective_model = model or (planner.model if not planner_runtime else "")
    gates = ValidationGates()
    memory = MemoryStore(store=store)
    lesson_store = LessonStore(store)
    has_llm_key = os.environ.get("INCEPTION_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    cognee_memory = CogneeMemory() if CogneeMemory and has_llm_key else None
    orchestrator = Orchestrator(
        pool=pool,
        planner=planner,
        gates=gates,
        runtime=runtime,
        model=effective_model,
        max_agents=max_agents,
        tracer=tracer,
        memory=memory,
        lesson_store=lesson_store,
        cognee_memory=cognee_memory,
        store=store,
    )
    return orchestrator, store, pool


class RunManager:
    """Manages multiple concurrent orchestrator runs with configurable concurrency."""

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT_RUNS,
        runtime: str = "claude",
        model: str | None = None,
        max_agents: int = DEFAULT_MAX_AGENTS,
        planner_runtime: str | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        project_context: str | None = None,
    ):
        self._db_path = db_path
        self._max_concurrent_runs = max_concurrent_runs
        self._runtime = runtime
        self._model = model
        self._max_agents = max_agents
        self._planner_runtime = planner_runtime
        self._poll_interval = poll_interval
        self._project_context = project_context

        self._store = Store(db_path)
        self._store.migrate()
        self._active_tasks: dict[str, asyncio.Task[Run]] = {}
        self._running = False
        self._shutdown_event = asyncio.Event()

    async def submit(self, task: str) -> str:
        """Submit a new task to the queue. Returns run_id immediately."""
        run_id = str(uuid.uuid4())
        self._store.insert_queued_run(run_id, task)
        logger.info("Submitted run %s: %s", run_id[:8], task[:50])
        return run_id

    async def cancel(self, run_id: str) -> bool:
        """Cancel a queued or running run. Returns True if cancelled."""
        run_data = self._store.fetch_run(run_id)
        if not run_data:
            return False

        state = run_data.get("state", "")
        if state in {"completed", "failed", "cancelled"}:
            return False

        run_id_full = run_data["id"]

        if state == "queued":
            self._store.update_run_state(run_id_full, "cancelled")
            return True

        # If active, cancel the asyncio.Task and kill associated agents
        if run_id_full in self._active_tasks:
            task = self._active_tasks[run_id_full]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            del self._active_tasks[run_id_full]

        # Kill any spawned agent processes for this run
        await self._kill_agents_for_run(run_id_full)

        self._store.update_run_state(run_id_full, "cancelled")
        return True

    async def _kill_agents_for_run(self, run_id: str) -> None:
        """Kill all agents associated with a run."""
        from horse_fish.agents.pool import AgentPool
        from horse_fish.agents.tmux import TmuxManager
        from horse_fish.agents.worktree import WorktreeManager

        repo_root = str(Path.cwd())
        tmux = TmuxManager()
        worktrees = WorktreeManager(repo_root)
        pool = AgentPool(self._store, tmux, worktrees)

        try:
            result = await pool.kill_agents_for_run(run_id)
            if result["killed"] > 0 or result["timed_out"] > 0:
                logger.info(
                    "Killed %d agents for run %s (%d timed out, %d failed)",
                    result["killed"],
                    run_id[:8],
                    result["timed_out"],
                    result["failed"],
                )
        except Exception as exc:
            logger.warning("Error killing agents for run %s: %s", run_id[:8], exc)

    async def start(self) -> None:
        """Main event loop. Polls for queued runs and dispatches up to max_concurrent."""
        self._running = True
        self._shutdown_event.clear()
        setup_logging()
        warn_if_no_langfuse()
        logger.info("RunManager started (max_concurrent=%d)", self._max_concurrent_runs)

        try:
            while self._running:
                await self._check_completed_tasks()

                active_count = len(self._active_tasks)
                if active_count < self._max_concurrent_runs:
                    slots_available = self._max_concurrent_runs - active_count
                    await self._dispatch_queued_runs(slots_available)

                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=self._poll_interval)
                    break
                except TimeoutError:
                    pass
        finally:
            self._running = False

    async def stop(self) -> None:
        """Graceful shutdown — cancel all active tasks and cleanup."""
        self._running = False
        self._shutdown_event.set()

        for _run_id, task in list(self._active_tasks.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._active_tasks.clear()

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """List all runs from SQLite (recent first)."""
        return self._store.fetch_recent_runs(limit)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Get a specific run by ID (supports prefix match)."""
        return self._store.fetch_run(run_id)

    async def _check_completed_tasks(self) -> None:
        """Check for completed tasks and update their state in the store."""
        completed = []
        for run_id, task in self._active_tasks.items():
            if task.done():
                completed.append(run_id)
                try:
                    run = task.result()
                    completed_at = run.completed_at.isoformat() if run.completed_at else None
                    self._store.update_run_state(run_id, run.state.value, completed_at)
                except asyncio.CancelledError:
                    self._store.update_run_state(run_id, "cancelled")
                except Exception as exc:
                    logger.error("Run %s failed: %s", run_id[:8], exc)
                    self._store.update_run_state(run_id, "failed")

        for run_id in completed:
            del self._active_tasks[run_id]

    async def _dispatch_queued_runs(self, slots: int) -> None:
        """Dispatch queued runs up to the available slot count."""
        queued_runs = self._store.fetch_queued_runs(limit=slots)

        for run_data in queued_runs:
            run_id = run_data["id"]
            task_desc = run_data["task"]
            self._store.update_run_state(run_id, "planning")
            task = asyncio.create_task(self._run_orchestrator(run_id, task_desc))
            self._active_tasks[run_id] = task

    async def _run_orchestrator(self, run_id: str, task_desc: str) -> Run:
        """Run an orchestrator for a single run."""
        orchestrator, store, pool = create_orchestrator(
            db_path=self._db_path,
            runtime=self._runtime,
            model=self._model,
            max_agents=self._max_agents,
            planner_runtime=self._planner_runtime,
            project_context=self._project_context,
        )

        try:
            return await orchestrator.run(task_desc)
        finally:
            try:
                await pool.cleanup()
            except Exception as exc:
                logger.warning("Error cleaning up pool for run %s: %s", run_id[:8], exc)
            store.close()
