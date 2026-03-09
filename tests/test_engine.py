"""Tests for the Orchestrator engine retry deadlock fix."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.models import AgentSlot, AgentState, Run, RunState, Subtask, SubtaskResult, SubtaskState
from horse_fish.orchestrator.engine import Orchestrator
from horse_fish.store.db import Store


@pytest.fixture
def mock_pool():
    """Mock AgentPool."""
    pool = AsyncMock()
    pool._get_slot = MagicMock()
    pool._worktrees = AsyncMock()
    return pool


@pytest.fixture
def mock_planner():
    """Mock Planner."""
    planner = AsyncMock()
    return planner


@pytest.fixture
def mock_gates():
    """Mock ValidationGates."""
    gates = AsyncMock()
    gates.all_passed = MagicMock(return_value=True)
    return gates


@pytest.fixture
def orchestrator(mock_pool, mock_planner, mock_gates):
    """Create an Orchestrator instance with mocked dependencies."""
    return Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        model="claude-sonnet-4.6",
        max_agents=2,  # Small limit to test concurrency
        stall_timeout_seconds=30,
    )


async def mock_sleep(seconds):
    """Mock sleep that returns immediately."""
    return None


@pytest.mark.asyncio
async def test_check_stalls_returns_retried_plus_failed_count(mock_pool, mock_planner, mock_gates):
    """Test _check_stalls returns count of both retried and failed subtasks."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        stall_timeout_seconds=30,
    )

    # Create one subtask that will be retried (has retries left)
    subtask_retry = Subtask(id="subtask-retry", description="Retry me", max_retries=2, retry_count=0)
    subtask_retry.state = SubtaskState.running
    subtask_retry.agent = "agent-1"
    subtask_retry.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)  # Stalled

    # Create one subtask that will fail (no retries left)
    subtask_fail = Subtask(id="subtask-fail", description="Fail me", max_retries=0, retry_count=0)
    subtask_fail.state = SubtaskState.running
    subtask_fail.agent = "agent-2"
    subtask_fail.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)  # Stalled

    run = Run.create("Test run")
    run.subtasks = [subtask_retry, subtask_fail]
    run.state = RunState.executing

    agent_map = {"subtask-retry": "agent-1", "subtask-fail": "agent-2"}
    mock_pool.release = AsyncMock()

    count = await orchestrator._check_stalls(run, agent_map)

    # Should return 2 (1 retried + 1 failed)
    assert count == 2
    assert subtask_retry.state == SubtaskState.pending
    assert subtask_retry.retry_count == 1
    assert subtask_fail.state == SubtaskState.failed
    assert mock_pool.release.call_count == 2


@pytest.mark.asyncio
async def test_execute_decrements_active_count_on_retry(orchestrator, mock_pool):
    """Test that when a subtask is retried after stall, active_count is correctly decremented."""
    # Create subtasks: one that will stall and retry, one that's pending waiting for capacity
    subtask1 = Subtask(id="subtask-1", description="Task 1")
    subtask1.state = SubtaskState.running
    subtask1.agent = "agent-1"
    subtask1.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)  # Stalled

    subtask2 = Subtask(id="subtask-2", description="Task 2")
    subtask2.state = SubtaskState.pending

    run = Run.create("Test run")
    run.subtasks = [subtask1, subtask2]
    run.state = RunState.executing

    # Mock pool to spawn only one agent initially (at concurrency limit)
    slot1 = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    slot2 = AgentSlot(
        id="agent-2",
        name="hf-subtask-2",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    # First spawn for subtask1 (already done), second spawn for retry of subtask1, third for subtask2
    mock_pool.spawn.side_effect = [slot1, slot2]
    mock_pool.send_task = AsyncMock()
    mock_pool.release = AsyncMock()

    # After retry, the retried subtask completes
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="subtask-1",
            success=True,
            output="Done",
            diff="commit",
            duration_seconds=10.0,
        )
    )

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    # Both subtasks should complete successfully
    assert result.state == RunState.reviewing
    assert subtask1.state == SubtaskState.done
    assert subtask2.state == SubtaskState.done


@pytest.mark.asyncio
async def test_execute_decrements_active_count_on_exhausted_retry(orchestrator, mock_pool):
    """Test that when a subtask fails after exhausting retries, active_count is decremented."""
    # Create subtask with no retries left that will stall and fail
    subtask1 = Subtask(id="subtask-1", description="Task 1", max_retries=0)
    subtask1.state = SubtaskState.running
    subtask1.agent = "agent-1"
    subtask1.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)  # Stalled

    # Another subtask waiting for capacity
    subtask2 = Subtask(id="subtask-2", description="Task 2")
    subtask2.state = SubtaskState.pending

    run = Run.create("Test run")
    run.subtasks = [subtask1, subtask2]
    run.state = RunState.executing

    # Mock pool
    slot = AgentSlot(
        id="agent-2",
        name="hf-subtask-2",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn.return_value = slot
    mock_pool.send_task = AsyncMock()
    mock_pool.release = AsyncMock()

    # subtask2 completes successfully
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="subtask-2",
            success=True,
            output="Done",
            diff="commit",
            duration_seconds=10.0,
        )
    )

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    # subtask1 should be failed, subtask2 should be done
    # Run should be failed because subtask1 failed
    assert result.state == RunState.failed
    assert subtask1.state == SubtaskState.failed
    assert subtask2.state == SubtaskState.done


@pytest.mark.asyncio
async def test_execute_no_deadlock_at_concurrency_limit_with_retry(orchestrator, mock_pool):
    """Test that retry at concurrency limit doesn't cause deadlock - new tasks can be dispatched."""
    # Simulate being at concurrency limit with a stalled task
    subtask1 = Subtask(id="subtask-1", description="Task 1")
    subtask1.state = SubtaskState.running
    subtask1.agent = "agent-1"
    subtask1.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)  # Stalled

    subtask2 = Subtask(id="subtask-2", description="Task 2")
    subtask2.state = SubtaskState.pending

    run = Run.create("Test run")
    run.subtasks = [subtask1, subtask2]
    run.state = RunState.executing

    # Mock pool - can spawn 2 agents total
    slot1 = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    slot2 = AgentSlot(
        id="agent-2",
        name="hf-subtask-2",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn.side_effect = [slot1, slot2]
    mock_pool.send_task = AsyncMock()
    mock_pool.release = AsyncMock()

    # Both subtasks complete after retry
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="subtask-1",
            success=True,
            output="Done",
            diff="commit",
            duration_seconds=10.0,
        )
    )

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    # Should NOT deadlock - both tasks should complete
    assert result.state == RunState.reviewing
    assert subtask1.state == SubtaskState.done
    assert subtask2.state == SubtaskState.done


@pytest.mark.asyncio
async def test_check_stalls_removes_failed_from_agent_map(mock_pool, mock_planner, mock_gates):
    """Test _check_stalls removes failed subtasks from agent_map."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        stall_timeout_seconds=30,
    )

    subtask = Subtask(id="subtask-1", description="Task 1", max_retries=0)
    subtask.state = SubtaskState.running
    subtask.agent = "agent-1"
    subtask.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)  # Stalled

    run = Run.create("Test run")
    run.subtasks = [subtask]
    run.state = RunState.executing

    agent_map = {"subtask-1": "agent-1"}
    mock_pool.release = AsyncMock()

    await orchestrator._check_stalls(run, agent_map)

    # Failed subtask should be removed from agent_map
    assert "subtask-1" not in agent_map
    assert subtask.state == SubtaskState.failed


@pytest.fixture
def tmp_store(tmp_path: Path) -> Store:
    """Create a temporary Store for testing."""
    store = Store(tmp_path / "test.db")
    store.migrate()
    return store


@pytest.mark.asyncio
async def test_orchestrator_persists_run_initial_state(tmp_path: Path, mock_pool, mock_planner, mock_gates, tmp_store):
    """Test that orchestrator persists run initial state to SQLite."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        store=tmp_store,
    )

    # Mock planner to return one subtask
    subtask = Subtask.create("do something")
    mock_planner.decompose = AsyncMock(return_value=[subtask])

    # Mock pool to complete the subtask immediately
    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn = AsyncMock(return_value=slot)
    mock_pool.send_task = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id=subtask.id,
            success=True,
            output="Done",
            diff="commit",
            duration_seconds=10.0,
        )
    )
    mock_pool._get_slot = MagicMock(return_value=slot)
    mock_gates.run_all = AsyncMock(return_value=[])

    # Mock worktree merge
    mock_pool._worktrees = AsyncMock()
    mock_pool._worktrees.merge = AsyncMock(return_value=True)

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator.run("test task")

    # Check run was persisted
    row = tmp_store.fetchone("SELECT * FROM runs WHERE id = ?", (result.id,))
    assert row is not None
    assert row["task"] == "test task"
    assert row["state"] == "completed"

    tmp_store.close()


@pytest.mark.asyncio
async def test_orchestrator_persists_subtask_state(tmp_path: Path, mock_pool, mock_planner, mock_gates, tmp_store):
    """Test that orchestrator persists subtask state changes to SQLite."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        store=tmp_store,
    )

    # Mock planner to return one subtask
    subtask = Subtask.create("do something")
    mock_planner.decompose = AsyncMock(return_value=[subtask])

    # Mock pool to complete the subtask immediately
    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn = AsyncMock(return_value=slot)
    mock_pool.send_task = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id=subtask.id,
            success=True,
            output="Done",
            diff="commit",
            duration_seconds=10.0,
        )
    )
    mock_pool._get_slot = MagicMock(return_value=slot)
    mock_gates.run_all = AsyncMock(return_value=[])

    # Mock worktree merge
    mock_pool._worktrees = AsyncMock()
    mock_pool._worktrees.merge = AsyncMock(return_value=True)

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        await orchestrator.run("test task")

    # Check subtask was persisted
    row = tmp_store.fetchone("SELECT * FROM subtasks WHERE id = ?", (subtask.id,))
    assert row is not None
    assert row["description"] == "do something"
    assert row["state"] == "done"
    assert row["agent_id"] == "agent-1"

    tmp_store.close()


@pytest.mark.asyncio
async def test_orchestrator_without_store_does_not_persist(tmp_path: Path, mock_pool, mock_planner, mock_gates):
    """Test that orchestrator without store does not persist to SQLite."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        store=None,
    )

    # Mock planner to return one subtask
    subtask = Subtask.create("do something")
    mock_planner.decompose = AsyncMock(return_value=[subtask])

    # Mock pool to complete the subtask immediately
    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn = AsyncMock(return_value=slot)
    mock_pool.send_task = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id=subtask.id,
            success=True,
            output="Done",
            diff="commit",
            duration_seconds=10.0,
        )
    )
    mock_pool._get_slot = MagicMock(return_value=slot)
    mock_gates.run_all = AsyncMock(return_value=[])

    # Mock worktree merge
    mock_pool._worktrees = AsyncMock()
    mock_pool._worktrees.merge = AsyncMock(return_value=True)

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        await orchestrator.run("test task")

    # Should not raise even without store
    # (persistence methods should gracefully skip when store is None)


@pytest.mark.asyncio
async def test_review_calls_auto_fix_before_run_all(orchestrator, mock_pool, mock_gates):
    """Test that _review calls auto_fix_and_commit before run_all."""
    from horse_fish.validation.gates import GateResult

    subtask = Subtask.create("do something")
    subtask.state = SubtaskState.done
    subtask.agent = "agent-1"

    run = Run.create("test task")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/test-worktree",
    )
    mock_pool._get_slot.return_value = slot

    # Track call order
    call_order: list[str] = []

    async def track_auto_fix(path):
        call_order.append("auto_fix_and_commit")
        return GateResult(gate="auto-fix", passed=True, output="ok", duration_seconds=0.1)

    async def track_run_all(path):
        call_order.append("run_all")
        return []

    mock_gates.auto_fix_and_commit = AsyncMock(side_effect=track_auto_fix)
    mock_gates.run_all = AsyncMock(side_effect=track_run_all)
    mock_gates.all_passed = MagicMock(return_value=True)

    result = await orchestrator._review(run)

    assert result.state == RunState.merging
    assert call_order == ["auto_fix_and_commit", "run_all"]
    mock_gates.auto_fix_and_commit.assert_called_once_with("/tmp/test-worktree")
    mock_gates.run_all.assert_called_once_with("/tmp/test-worktree")


@pytest.mark.asyncio
async def test_review_retries_on_gate_failure(orchestrator, mock_pool, mock_gates):
    """Test that _review sends fix prompt and returns to executing when gates fail and retries remain."""
    from horse_fish.validation.gates import GateResult

    subtask = Subtask.create("do something")
    subtask.state = SubtaskState.done
    subtask.agent = "agent-1"
    subtask.gate_retry_count = 0
    subtask.max_gate_retries = 1

    run = Run.create("test task")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/test-worktree",
        branch="feat-test",
    )
    mock_pool._get_slot.return_value = slot

    # Gates fail
    mock_gates.auto_fix_and_commit = AsyncMock(
        return_value=GateResult(gate="auto-fix", passed=True, output="ok", duration_seconds=0.1)
    )
    failed_gate = GateResult(gate="ruff-check", passed=False, output="F401 unused import", duration_seconds=0.1)
    mock_gates.run_all = AsyncMock(return_value=[failed_gate])
    mock_gates.all_passed = MagicMock(return_value=False)

    # Agent is still alive
    mock_pool.check_status = AsyncMock(return_value=AgentState.busy)
    mock_pool.send_task = AsyncMock()

    result = await orchestrator._review(run)

    # Should transition back to executing, not failed
    assert result.state == RunState.executing
    assert subtask.state == SubtaskState.running
    assert subtask.gate_retry_count == 1
    # Should have sent fix prompt to agent
    mock_pool.send_task.assert_called_once()
    call_args = mock_pool.send_task.call_args
    assert "F401 unused import" in call_args[0][1]  # prompt contains gate output


@pytest.mark.asyncio
async def test_review_fails_when_gate_retries_exhausted(orchestrator, mock_pool, mock_gates):
    """Test that _review fails the run when gate retries are exhausted."""
    from horse_fish.validation.gates import GateResult

    subtask = Subtask.create("do something")
    subtask.state = SubtaskState.done
    subtask.agent = "agent-1"
    subtask.gate_retry_count = 1
    subtask.max_gate_retries = 1  # Already at max

    run = Run.create("test task")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/test-worktree",
    )
    mock_pool._get_slot.return_value = slot

    mock_gates.auto_fix_and_commit = AsyncMock(
        return_value=GateResult(gate="auto-fix", passed=True, output="ok", duration_seconds=0.1)
    )
    failed_gate = GateResult(gate="pytest", passed=False, output="2 failed", duration_seconds=1.0)
    mock_gates.run_all = AsyncMock(return_value=[failed_gate])
    mock_gates.all_passed = MagicMock(return_value=False)

    result = await orchestrator._review(run)

    # Should fail — no retries left
    assert result.state == RunState.failed
    assert subtask.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_review_skips_retry_when_agent_dead(orchestrator, mock_pool, mock_gates):
    """Test that _review doesn't retry when agent tmux session is dead."""
    from horse_fish.validation.gates import GateResult

    subtask = Subtask.create("do something")
    subtask.state = SubtaskState.done
    subtask.agent = "agent-1"
    subtask.gate_retry_count = 0
    subtask.max_gate_retries = 1

    run = Run.create("test task")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/test-worktree",
    )
    mock_pool._get_slot.return_value = slot

    mock_gates.auto_fix_and_commit = AsyncMock(
        return_value=GateResult(gate="auto-fix", passed=True, output="ok", duration_seconds=0.1)
    )
    failed_gate = GateResult(gate="pytest", passed=False, output="1 failed", duration_seconds=1.0)
    mock_gates.run_all = AsyncMock(return_value=[failed_gate])
    mock_gates.all_passed = MagicMock(return_value=False)

    # Agent is dead
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)

    result = await orchestrator._review(run)

    # Should fail — can't retry with dead agent
    assert result.state == RunState.failed
    assert subtask.state == SubtaskState.failed


def test_subtask_has_gate_retry_fields():
    """Test Subtask has gate_retry_count and max_gate_retries fields."""
    subtask = Subtask.create("test")
    assert subtask.gate_retry_count == 0
    assert subtask.max_gate_retries == 1
