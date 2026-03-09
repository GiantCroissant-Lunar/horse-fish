"""RunManager: multi-run queue with configurable concurrency."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.memory.lessons import LessonStore
from horse_fish.memory.store import MemoryStore
from horse_fish.models import Run
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

    Args:
        db_path: Path to SQLite database
        runtime: Default runtime for agents (claude, copilot, pi, etc.)
        model: Model override for agents
        max_agents: Maximum concurrent agents per orchestrator
        planner_runtime: Runtime for planning (defaults to runtime)
        project_context: Optional CLAUDE.md content for prompt context

    Returns:
        Tuple of (Orchestrator, Store, AgentPool)
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
    # Use user-specified model for agents; only fall back to planner default when no separate planner runtime
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
    """Manages multiple concurrent orchestrator runs with configurable concurrency.

    The RunManager maintains a queue of submitted runs and dispatches them
    to Orchestrator instances based on available concurrency slots. Each run
    gets its own Orchestrator with its own AgentPool - no agents are shared
    across runs.

    Example:
        ```python
        manager = RunManager(db_path="state.db", max_concurrent_runs=2)
        run_id = await manager.submit("Fix the bug in module X")
        await manager.start()  # Runs forever until stop() is called
        ```
    """

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
        """Initialize the RunManager.

        Args:
            db_path: Path to SQLite database for run queue persistence
            max_concurrent_runs: Maximum number of runs to execute concurrently
            runtime: Default runtime for agents
            model: Model override for agents
            max_agents: Maximum concurrent agents per orchestrator
            planner_runtime: Runtime for planning (defaults to runtime)
            poll_interval: Seconds between polling the queue for new runs
            project_context: Optional CLAUDE.md content for prompt context
        """
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
        self._active_tasks: dict[str, asyncio.Task[Run]] = {}  # run_id -> Task
        self._running = False
        self._shutdown_event = asyncio.Event()

    async def submit(self, task: str) -> str:
        """Submit a new task to the queue.

        Args:
            task: Task description to be executed

        Returns:
            run_id: Unique identifier for the submitted run
        """
        import uuid

        run_id = str(uuid.uuid4())
        self._store.insert_queued_run(run_id, task)
        logger.info("Submitted run %s: %s", run_id[:8], task[:50])
        return run_id

    async def cancel(self, run_id: str) -> bool:
        """Cancel a queued or running run.

        Args:
            run_id: The run ID to cancel (supports prefix match)

        Returns:
            True if cancelled, False if not found or already terminal
        """
        # Find the run
        run_data = self._store.fetch_run(run_id)
        if not run_data:
            logger.warning("Run %s not found for cancellation", run_id)
            return False

        state = run_data.get("state", "")
        terminal_states = {"completed", "failed", "cancelled"}
        if state in terminal_states:
            logger.info("Run %s already in terminal state: %s", run_id[:8], state)
            return False

        # If queued, just update state
        if state == "queued":
            self._store.update_run_state(run_data["id"], "cancelled")
            logger.info("Cancelled queued run %s", run_id[:8])
            return True

        # If active, cancel the asyncio.Task
        if run_data["id"] in self._active_tasks:
            task = self._active_tasks[run_data["id"]]
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            del self._active_tasks[run_data["id"]]

        # Update state in store
        self._store.update_run_state(run_data["id"], "cancelled")
        logger.info("Cancelled running run %s", run_id[:8])
        return True

    async def start(self) -> None:
        """Main event loop. Polls for queued runs and dispatches up to max_concurrent.

        This method runs forever until stop() is called. It:
        1. Counts active runs
        2. If active < max_concurrent, pops oldest queued run
        3. Creates Orchestrator instance for that run
        4. Launches orchestrator.run(task) as asyncio.Task
        5. Tracks task in self._active_tasks dict
        6. Checks completed tasks and frees slots
        7. Sleeps poll_interval
        """
        self._running = True
        self._shutdown_event.clear()
        logger.info("RunManager started (max_concurrent=%d)", self._max_concurrent_runs)

        try:
            while self._running:
                # Check for completed tasks first
                await self._check_completed_tasks()

                # Count active runs
                active_count = len(self._active_tasks)

                # If we have capacity, try to dispatch queued runs
                if active_count < self._max_concurrent_runs:
                    slots_available = self._max_concurrent_runs - active_count
                    await self._dispatch_queued_runs(slots_available)

                # Wait for poll interval or shutdown
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=self._poll_interval)
                    break  # Shutdown requested
                except TimeoutError:
                    pass  # Continue polling

        finally:
            self._running = False
            logger.info("RunManager stopped")

    async def stop(self) -> None:
        """Graceful shutdown — cancel all active tasks and cleanup."""
        logger.info("RunManager stopping...")
        self._running = False
        self._shutdown_event.set()

        # Cancel all active tasks
        for run_id, task in list(self._active_tasks.items()):
            logger.info("Cancelling run %s", run_id[:8])
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._active_tasks.clear()

        # Cleanup agent pools (they hold tmux sessions, worktrees)
        # Note: Each orchestrator manages its own pool, so we rely on
        # the task cancellation to trigger cleanup via finally blocks

        logger.info("RunManager shutdown complete")

    def list_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """List all runs from SQLite (recent first).

        Args:
            limit: Maximum number of runs to return

        Returns:
            List of run dictionaries with id, task, state, created_at, etc.
        """
        return self._store.fetch_recent_runs(limit)

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        """Get a specific run by ID (supports prefix match).

        Args:
            run_id: Run ID or prefix

        Returns:
            Run dictionary or None if not found
        """
        return self._store.fetch_run(run_id)

    async def _check_completed_tasks(self) -> None:
        """Check for completed tasks and update their state in the store."""
        completed = []
        for run_id, task in self._active_tasks.items():
            if task.done():
                completed.append(run_id)
                try:
                    run = task.result()
                    # Update state in store based on run result
                    completed_at = run.completed_at.isoformat() if run.completed_at else None
                    self._store.update_run_state(run_id, run.state.value, completed_at)
                    logger.info("Run %s completed with state: %s", run_id[:8], run.state.value)
                except asyncio.CancelledError:
                    logger.info("Run %s was cancelled", run_id[:8])
                    self._store.update_run_state(run_id, "cancelled")
                except Exception as exc:
                    logger.error("Run %s failed with exception: %s", run_id[:8], exc)
                    self._store.update_run_state(run_id, "failed")

        for run_id in completed:
            del self._active_tasks[run_id]

    async def _dispatch_queued_runs(self, slots: int) -> None:
        """Dispatch queued runs up to the available slot count.

        Args:
            slots: Number of available concurrency slots
        """
        queued_runs = self._store.fetch_queued_runs(limit=slots)

        for run_data in queued_runs:
            run_id = run_data["id"]
            task_desc = run_data["task"]

            # Update state to planning before starting
            self._store.update_run_state(run_id, "planning")

            # Create orchestrator and launch task
            task = asyncio.create_task(self._run_orchestrator(run_id, task_desc))
            self._active_tasks[run_id] = task
            logger.info("Dispatched run %s (task: %s...)", run_id[:8], task_desc[:30])

    async def _run_orchestrator(self, run_id: str, task_desc: str) -> Run:
        """Run an orchestrator for a single run.

        Args:
            run_id: The run ID
            task_desc: The task description

        Returns:
            The completed Run object
        """
        orchestrator, store, pool = create_orchestrator(
            db_path=self._db_path,
            runtime=self._runtime,
            model=self._model,
            max_agents=self._max_agents,
            planner_runtime=self._planner_runtime,
            project_context=self._project_context,
        )

        try:
            run = await orchestrator.run(task_desc)
            return run
        finally:
            # Cleanup: release all agents
            try:
                await pool.cleanup()
            except Exception as exc:
                logger.warning("Error cleaning up pool for run %s: %s", run_id[:8], exc)
            store.close()
