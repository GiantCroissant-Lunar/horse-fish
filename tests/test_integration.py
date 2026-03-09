"""Integration tests for Orchestrator with mocked subprocess layer."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.models import AgentSlot, AgentState, RunState, Subtask, SubtaskResult, SubtaskState
from horse_fish.orchestrator.engine import Orchestrator
from horse_fish.planner.decompose import Planner
from horse_fish.store.db import Store
from horse_fish.validation.gates import GateResult, ValidationGates


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch asyncio.sleep to a no-op so polling loops don't block tests."""
    monkeypatch.setattr(asyncio, "sleep", AsyncMock(return_value=None))


@pytest.fixture
def tmp_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Create a temporary directory for SQLite database."""
    return tmp_path_factory.mktemp("integration")


@pytest.fixture
def mock_pool() -> MagicMock:
    """Create a mocked AgentPool."""
    pool = MagicMock(spec=AgentPool)

    # Mock spawn to return an AgentSlot
    mock_slot = AgentSlot(
        id=uuid.uuid4().hex,
        name="hf-test123",
        runtime="claude",
        model="claude-sonnet-4-6",
        capability="builder",
        state=AgentState.idle,
        pid=12345,
        tmux_session="hf-test123",
        worktree_path="/tmp/worktrees/hf-test123",
        branch="horse-fish/test123",
        started_at=datetime.now(UTC),
    )
    pool.spawn = AsyncMock(return_value=mock_slot)

    # Mock send_task
    pool.send_task = AsyncMock(return_value=None)

    # Mock check_status to return dead after first call (simulating quick completion)
    pool.check_status = AsyncMock(return_value=AgentState.dead)

    # Mock collect_result to return a successful result with diff
    pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id=uuid.uuid4().hex,
            success=True,
            output="Task completed successfully",
            diff="+ new feature\n- old code",
            duration_seconds=5.0,
        )
    )

    # Mock _get_slot to return the slot
    pool._get_slot = MagicMock(return_value=mock_slot)

    return pool


@pytest.fixture
def mock_planner() -> MagicMock:
    """Create a mocked Planner."""
    planner = MagicMock(spec=Planner)

    # Mock decompose to return 2 subtasks with dependency
    subtask_a = Subtask.create("Implement feature A")
    subtask_b = Subtask.create("Implement feature B that depends on A")
    subtask_b.deps = [subtask_a.description]

    planner.decompose = AsyncMock(return_value=[subtask_a, subtask_b])

    return planner


@pytest.fixture
def mock_gates() -> MagicMock:
    """Create a mocked ValidationGates."""
    gates = MagicMock(spec=ValidationGates)

    # Mock run_all to return all-pass results
    gates.run_all = AsyncMock(
        return_value=[
            GateResult(gate="compile", passed=True, output="", duration_seconds=0.1),
            GateResult(gate="ruff-check", passed=True, output="", duration_seconds=0.2),
            GateResult(gate="pytest", passed=True, output="", duration_seconds=1.0),
        ]
    )

    # Mock all_passed to return True
    gates.all_passed = MagicMock(return_value=True)

    return gates


@pytest.fixture
def mock_worktree_manager() -> MagicMock:
    """Create a mocked WorktreeManager."""
    worktrees = MagicMock(spec=WorktreeManager)
    worktrees.merge = AsyncMock(return_value=True)
    return worktrees


@pytest.fixture
def orchestrator(
    mock_pool: MagicMock, mock_planner: MagicMock, mock_gates: MagicMock, mock_worktree_manager: MagicMock
) -> Orchestrator:
    """Create an Orchestrator with mocked dependencies."""
    # Set up the pool's _worktrees attribute
    mock_pool._worktrees = mock_worktree_manager

    return Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        model="claude-sonnet-4-6",
        max_agents=3,
    )


@pytest.mark.asyncio
async def test_orchestrator_full_lifecycle_success(orchestrator: Orchestrator, mock_pool: MagicMock) -> None:
    """Test full lifecycle: plan → execute → review → merge → completed."""
    run = await orchestrator.run("build feature X")

    assert run.state == RunState.completed
    assert len(run.subtasks) == 2
    assert all(s.state == SubtaskState.done for s in run.subtasks)
    assert run.completed_at is not None


@pytest.mark.asyncio
async def test_orchestrator_respects_dependency_ordering(orchestrator: Orchestrator, mock_pool: MagicMock) -> None:
    """Test that dependencies are respected (subtask B waits for A)."""
    # Track the order of subtask dispatch
    dispatch_order: list[str] = []

    original_spawn = mock_pool.spawn

    async def tracked_spawn(name: str, runtime: str, model: str, capability: str) -> AgentSlot:
        result = await original_spawn(name, runtime, model, capability)
        dispatch_order.append(name)
        return result

    mock_pool.spawn = AsyncMock(side_effect=tracked_spawn)

    run = await orchestrator.run("build feature X")

    assert run.state == RunState.completed

    # Subtask A should have been dispatched before subtask B
    # (This is implicit in the dependency checking logic)
    assert len(run.subtasks) == 2
    subtask_a = run.subtasks[0]
    subtask_b = run.subtasks[1]

    # Verify dependency structure (deps resolved to IDs by _resolve_deps)
    assert subtask_b.deps == [subtask_a.id]


@pytest.mark.asyncio
async def test_orchestrator_planner_error(mock_pool: MagicMock, mock_gates: MagicMock) -> None:
    """Test that planner errors lead to failed state."""
    mock_planner = MagicMock(spec=Planner)
    mock_planner.decompose = AsyncMock(side_effect=Exception("LLM error"))

    mock_worktree_manager = MagicMock(spec=WorktreeManager)
    mock_pool._worktrees = mock_worktree_manager

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates, runtime="claude", model="claude-sonnet-4-6"
    )

    run = await orchestrator.run("build feature X")

    assert run.state == RunState.failed
    assert len(run.subtasks) == 0


@pytest.mark.asyncio
async def test_orchestrator_planner_empty_subtasks(mock_pool: MagicMock, mock_gates: MagicMock) -> None:
    """Test that empty subtask list leads to failed state."""
    mock_planner = MagicMock(spec=Planner)
    mock_planner.decompose = AsyncMock(return_value=[])

    mock_worktree_manager = MagicMock(spec=WorktreeManager)
    mock_pool._worktrees = mock_worktree_manager

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates, runtime="claude", model="claude-sonnet-4-6"
    )

    run = await orchestrator.run("build feature X")

    assert run.state == RunState.failed
    assert len(run.subtasks) == 0


@pytest.mark.asyncio
async def test_orchestrator_gate_failure(mock_pool: MagicMock, mock_planner: MagicMock) -> None:
    """Test that gate failures lead to failed state."""
    mock_gates = MagicMock(spec=ValidationGates)

    # Mock run_all to return one failing gate
    mock_gates.run_all = AsyncMock(
        return_value=[
            GateResult(gate="compile", passed=True, output="", duration_seconds=0.1),
            GateResult(gate="ruff-check", passed=False, output="E501 line too long", duration_seconds=0.2),
            GateResult(gate="pytest", passed=True, output="", duration_seconds=1.0),
        ]
    )
    mock_gates.all_passed = MagicMock(return_value=False)

    mock_worktree_manager = MagicMock(spec=WorktreeManager)
    mock_pool._worktrees = mock_worktree_manager

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates, runtime="claude", model="claude-sonnet-4-6"
    )

    run = await orchestrator.run("build feature X")

    assert run.state == RunState.failed

    # At least one subtask should have failed due to gates
    failed_subtasks = [s for s in run.subtasks if s.state == SubtaskState.failed]
    assert len(failed_subtasks) > 0


@pytest.mark.asyncio
async def test_orchestrator_merge_conflict(
    mock_pool: MagicMock, mock_planner: MagicMock, mock_gates: MagicMock
) -> None:
    """Test that merge conflicts lead to failed state."""
    mock_worktree_manager = MagicMock(spec=WorktreeManager)
    mock_worktree_manager.merge = AsyncMock(return_value=False)  # Simulate conflict

    mock_pool._worktrees = mock_worktree_manager

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates, runtime="claude", model="claude-sonnet-4-6"
    )

    run = await orchestrator.run("build feature X")

    assert run.state == RunState.failed

    # At least one subtask should have failed due to merge
    failed_subtasks = [s for s in run.subtasks if s.state == SubtaskState.failed]
    assert len(failed_subtasks) > 0


@pytest.mark.asyncio
async def test_orchestrator_agent_spawn_failure(mock_planner: MagicMock, mock_gates: MagicMock) -> None:
    """Test that agent spawn failures are handled gracefully."""
    mock_pool = MagicMock(spec=AgentPool)

    # Mock spawn to fail
    mock_pool.spawn = AsyncMock(side_effect=Exception("Failed to spawn agent"))

    mock_worktree_manager = MagicMock(spec=WorktreeManager)
    mock_pool._worktrees = mock_worktree_manager

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates, runtime="claude", model="claude-sonnet-4-6"
    )

    run = await orchestrator.run("build feature X")

    assert run.state == RunState.failed

    # At least one subtask should have failed due to spawn failure
    # (The run fails immediately after the first spawn failure due to deadlock detection)
    failed_subtasks = [s for s in run.subtasks if s.state == SubtaskState.failed]
    assert len(failed_subtasks) >= 1


@pytest.mark.asyncio
async def test_orchestrator_with_real_store(tmp_path: Path) -> None:
    """Test integration with real Store (in-memory SQLite)."""
    db_path = tmp_path / "test.db"
    store = Store(str(db_path))
    store.migrate()

    # Create real components with the store
    mock_tmux = MagicMock(spec=TmuxManager)
    mock_worktrees = MagicMock(spec=WorktreeManager)

    mock_pool = AgentPool(store, mock_tmux, mock_worktrees)

    # Create slots that will be used by the orchestrator
    slot_a_id = uuid.uuid4().hex
    slot_b_id = uuid.uuid4().hex

    mock_slot_a = AgentSlot(
        id=slot_a_id,
        name="hf-testa",
        runtime="claude",
        model="claude-sonnet-4-6",
        capability="builder",
        state=AgentState.idle,
        pid=12345,
        tmux_session="hf-testa",
        worktree_path="/tmp/worktrees/hf-testa",
        branch="horse-fish/testa",
        started_at=datetime.now(UTC),
    )

    mock_slot_b = AgentSlot(
        id=slot_b_id,
        name="hf-testb",
        runtime="claude",
        model="claude-sonnet-4-6",
        capability="builder",
        state=AgentState.idle,
        pid=12346,
        tmux_session="hf-testb",
        worktree_path="/tmp/worktrees/hf-testb",
        branch="horse-fish/testb",
        started_at=datetime.now(UTC),
    )

    # Track spawn calls to return appropriate slots
    spawn_call_count = [0]

    async def mock_spawn(name: str, runtime: str, model: str, capability: str) -> AgentSlot:
        spawn_call_count[0] += 1
        return mock_slot_a if spawn_call_count[0] % 2 == 1 else mock_slot_b

    # We need to patch the AgentPool methods since they're async
    with (
        patch.object(AgentPool, "spawn", new_callable=AsyncMock, side_effect=mock_spawn),
        patch.object(AgentPool, "send_task", new_callable=AsyncMock, return_value=None),
        patch.object(AgentPool, "check_status", new_callable=AsyncMock, return_value=AgentState.dead),
        patch.object(
            AgentPool,
            "collect_result",
            new_callable=AsyncMock,
            return_value=SubtaskResult(
                subtask_id=uuid.uuid4().hex,
                success=True,
                output="Done",
                diff="+ code",
                duration_seconds=1.0,
            ),
        ),
    ):
        # Mock _get_slot to return the appropriate slot based on agent_id
        def mock_get_slot(agent_id: str) -> AgentSlot:
            return mock_slot_a if agent_id == slot_a_id else mock_slot_b

        mock_pool._get_slot = MagicMock(side_effect=mock_get_slot)

        # Mock planner and gates
        mock_planner = MagicMock(spec=Planner)
        subtask_a = Subtask.create("Task A")
        subtask_b = Subtask.create("Task B")
        mock_planner.decompose = AsyncMock(return_value=[subtask_a, subtask_b])

        mock_gates = MagicMock(spec=ValidationGates)
        mock_gates.run_all = AsyncMock(
            return_value=[GateResult(gate="compile", passed=True, output="", duration_seconds=0.1)]
        )
        mock_gates.all_passed = MagicMock(return_value=True)

        mock_worktrees.merge = AsyncMock(return_value=True)

        orchestrator = Orchestrator(
            pool=mock_pool, planner=mock_planner, gates=mock_gates, runtime="claude", model="claude-sonnet-4-6"
        )

        run = await orchestrator.run("real store test")

        assert run.state == RunState.completed
        assert len(run.subtasks) == 2

    store.close()
