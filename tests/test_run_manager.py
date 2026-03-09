"""Tests for RunManager."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from horse_fish.models import Run, RunState
from horse_fish.orchestrator.run_manager import RunManager, create_orchestrator
from horse_fish.store.db import Store


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary database path."""
    return str(tmp_path / "test.db")


@pytest.fixture
def run_manager(tmp_db):
    """Create a RunManager instance with a temporary database."""
    return RunManager(db_path=tmp_db)


def make_store(db_path: str) -> Store:
    """Create a Store instance."""
    store = Store(db_path)
    store.migrate()
    return store


class TestSubmit:
    """Tests for RunManager.submit()."""

    def test_submit_creates_queued_run(self, tmp_db):
        """submit() should create a run in 'queued' state."""
        manager = RunManager(db_path=tmp_db)
        run_id = asyncio.run(manager.submit("Test task"))

        assert run_id is not None
        store = make_store(tmp_db)
        run = store.fetch_run(run_id)
        assert run is not None
        assert run["task"] == "Test task"
        assert run["state"] == "queued"
        store.close()

    def test_submit_multiple_runs(self, tmp_db):
        """submit() should queue multiple runs in order."""
        manager = RunManager(db_path=tmp_db)
        run_ids = []
        for i in range(3):
            run_id = asyncio.run(manager.submit(f"Task {i}"))
            run_ids.append(run_id)

        store = make_store(tmp_db)
        runs = store.fetch_queued_runs()
        assert len(runs) == 3
        # Should be ordered by created_at (oldest first)
        assert runs[0]["task"] == "Task 0"
        assert runs[1]["task"] == "Task 1"
        assert runs[2]["task"] == "Task 2"
        store.close()


class TestCancel:
    """Tests for RunManager.cancel()."""

    def test_cancel_queued_run(self, tmp_db):
        """cancel() should update a queued run to 'cancelled'."""
        manager = RunManager(db_path=tmp_db)
        run_id = asyncio.run(manager.submit("Test task"))

        result = asyncio.run(manager.cancel(run_id))
        assert result is True

        store = make_store(tmp_db)
        run = store.fetch_run(run_id)
        assert run["state"] == "cancelled"
        store.close()

    def test_cancel_nonexistent_run(self, tmp_db):
        """cancel() should return False for nonexistent run."""
        manager = RunManager(db_path=tmp_db)
        result = asyncio.run(manager.cancel("nonexistent-id"))
        assert result is False

    def test_cancel_already_terminal_run(self, tmp_db):
        """cancel() should return False for already terminal runs."""
        store = make_store(tmp_db)
        store.insert_queued_run("run-1", "Test task")
        store.update_run_state("run-1", "completed")
        store.close()

        manager = RunManager(db_path=tmp_db)
        result = asyncio.run(manager.cancel("run-1"))
        assert result is False

    def test_cancel_by_prefix(self, tmp_db):
        """cancel() should support prefix matching."""
        manager = RunManager(db_path=tmp_db)
        run_id = asyncio.run(manager.submit("Test task"))
        prefix = run_id[:8]

        result = asyncio.run(manager.cancel(prefix))
        assert result is True

        store = make_store(tmp_db)
        run = store.fetch_run(run_id)
        assert run["state"] == "cancelled"
        store.close()


class TestListRuns:
    """Tests for RunManager.list_runs()."""

    def test_list_runs(self, tmp_db):
        """list_runs() should return runs ordered by created_at DESC."""
        store = make_store(tmp_db)
        # Insert runs with explicit timestamps
        store.execute(
            "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
            ("run-1", "Task 1", "2026-03-09T01:00:00Z"),
        )
        store.execute(
            "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
            ("run-2", "Task 2", "2026-03-09T02:00:00Z"),
        )
        store.execute(
            "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
            ("run-3", "Task 3", "2026-03-09T03:00:00Z"),
        )
        store.close()

        manager = RunManager(db_path=tmp_db)
        runs = manager.list_runs()

        assert len(runs) == 3
        # Most recent first
        assert runs[0]["id"] == "run-3"
        assert runs[1]["id"] == "run-2"
        assert runs[2]["id"] == "run-1"

    def test_list_runs_limit(self, tmp_db):
        """list_runs() should respect the limit parameter."""
        store = make_store(tmp_db)
        for i in range(10):
            store.execute(
                "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
                (f"run-{i}", f"Task {i}", f"2026-03-09T{i:02d}:00:00Z"),
            )
        store.close()

        manager = RunManager(db_path=tmp_db)
        runs = manager.list_runs(limit=5)

        assert len(runs) == 5


class TestGetRun:
    """Tests for RunManager.get_run()."""

    def test_get_run_by_full_id(self, tmp_db):
        """get_run() should find run by full ID."""
        store = make_store(tmp_db)
        store.insert_queued_run("run-123", "Test task")
        store.close()

        manager = RunManager(db_path=tmp_db)
        run = manager.get_run("run-123")

        assert run is not None
        assert run["id"] == "run-123"
        assert run["task"] == "Test task"

    def test_get_run_by_prefix(self, tmp_db):
        """get_run() should find run by prefix if unique."""
        store = make_store(tmp_db)
        store.insert_queued_run("run-abc-123", "Test task")
        store.close()

        manager = RunManager(db_path=tmp_db)
        run = manager.get_run("run-abc")

        assert run is not None
        assert run["id"] == "run-abc-123"

    def test_get_run_not_found(self, tmp_db):
        """get_run() should return None for nonexistent run."""
        manager = RunManager(db_path=tmp_db)
        run = manager.get_run("nonexistent")
        assert run is None


class TestMaxConcurrent:
    """Tests for max_concurrent_runs enforcement."""

    @pytest.mark.asyncio
    async def test_max_concurrent_respected(self, tmp_db):
        """Only max_concurrent_runs should be active at once."""
        # Create a mock orchestrator factory that tracks active runs
        active_count = 0
        max_active = 0
        lock = asyncio.Lock()

        async def mock_run_orchestrator(self, run_id, task_desc):
            nonlocal active_count, max_active
            async with lock:
                active_count += 1
                if active_count > max_active:
                    max_active = active_count
            # Simulate work
            await asyncio.sleep(0.1)
            async with lock:
                active_count -= 1
            # Return a mock run
            run = Run.create(task_desc)
            run.state = RunState.completed
            run.completed_at = datetime.now(UTC)
            return run

        manager = RunManager(db_path=tmp_db, max_concurrent_runs=2, poll_interval=0.05)

        # Patch _run_orchestrator to use our mock
        with patch.object(RunManager, "_run_orchestrator", mock_run_orchestrator):
            # Submit 5 runs
            for i in range(5):
                await manager.submit(f"Task {i}")

            # Run the start loop briefly - it enforces concurrency limits
            async def run_briefly():
                # Start a task that runs the manager
                start_task = asyncio.create_task(manager.start())
                # Let it run for a bit to dispatch and complete runs
                await asyncio.sleep(0.5)
                # Stop the manager
                await manager.stop()
                # Wait for start task to finish
                try:
                    await asyncio.wait_for(start_task, timeout=1.0)
                except TimeoutError:
                    start_task.cancel()
                    try:
                        await start_task
                    except asyncio.CancelledError:
                        pass

            await run_briefly()

        # Verify max concurrent was respected
        assert max_active <= 2

    @pytest.mark.asyncio
    async def test_completed_run_frees_slot(self, tmp_db):
        """When a run completes, a new queued run should start."""
        completed_runs = []
        active_count = 0

        async def mock_run_orchestrator(self, run_id, task_desc):
            nonlocal active_count
            active_count += 1
            # Simulate work with varying durations
            if "Task 0" in task_desc or "Task 1" in task_desc:
                await asyncio.sleep(0.05)
            else:
                await asyncio.sleep(0.15)
            active_count -= 1
            completed_runs.append(run_id)
            run = Run.create(task_desc)
            run.state = RunState.completed
            run.completed_at = datetime.now(UTC)
            return run

        manager = RunManager(db_path=tmp_db, max_concurrent_runs=2, poll_interval=0.05)

        with patch.object(RunManager, "_run_orchestrator", mock_run_orchestrator):
            # Submit 3 runs
            for i in range(3):
                await manager.submit(f"Task {i}")

            # Run for a bit
            async def run_briefly():
                # First dispatch
                await manager._dispatch_queued_runs(2)
                await asyncio.sleep(0.1)
                await manager._check_completed_tasks()
                # Second dispatch (should pick up remaining queued run)
                await manager._dispatch_queued_runs(2)
                await asyncio.sleep(0.2)
                await manager._check_completed_tasks()
                await manager.stop()

            await run_briefly()

        # All runs should have completed
        assert len(completed_runs) == 3


class TestStartStop:
    """Tests for RunManager.start() and stop()."""

    @pytest.mark.asyncio
    async def test_stop_cancels_active_tasks(self, tmp_db):
        """stop() should cancel all active tasks."""
        started_tasks = []
        cancelled_tasks = []

        async def mock_run_orchestrator(self, run_id, task_desc):
            started_tasks.append(run_id)
            try:
                await asyncio.sleep(10)  # Long sleep to be cancelled
            except asyncio.CancelledError:
                cancelled_tasks.append(run_id)
                raise
            run = Run.create(task_desc)
            run.state = RunState.completed
            return run

        manager = RunManager(db_path=tmp_db, max_concurrent_runs=2)

        with patch.object(RunManager, "_run_orchestrator", mock_run_orchestrator):
            # Submit and dispatch
            await manager.submit("Task 1")
            await manager._dispatch_queued_runs(1)

            # Give task time to start
            await asyncio.sleep(0.05)

            # Stop
            await manager.stop()

        # Task should have been started and cancelled
        assert len(started_tasks) == 1
        assert len(cancelled_tasks) == 1
        assert started_tasks[0] == cancelled_tasks[0]


class TestCreateOrchestrator:
    """Tests for create_orchestrator() factory function."""

    def test_create_orchestrator_returns_components(self, tmp_db):
        """create_orchestrator() should return Orchestrator, Store, AgentPool."""
        from horse_fish.agents.pool import AgentPool
        from horse_fish.orchestrator.engine import Orchestrator
        from horse_fish.store.db import Store

        orchestrator, store, pool = create_orchestrator(
            db_path=tmp_db,
            runtime="claude",
            model="claude-sonnet-4.6",
            max_agents=3,
        )

        assert isinstance(orchestrator, Orchestrator)
        assert isinstance(store, Store)
        assert isinstance(pool, AgentPool)

        # Cleanup
        asyncio.run(pool.cleanup())
        store.close()

    def test_create_orchestrator_with_planner_runtime(self, tmp_db):
        """create_orchestrator() should use planner_runtime when provided."""
        orchestrator, store, pool = create_orchestrator(
            db_path=tmp_db,
            runtime="claude",
            model="claude-sonnet-4.6",
            max_agents=3,
            planner_runtime="pi",
        )

        assert orchestrator._runtime == "claude"
        # Planner should use pi runtime
        assert orchestrator._planner.runtime == "pi"

        # Cleanup
        asyncio.run(pool.cleanup())
        store.close()
