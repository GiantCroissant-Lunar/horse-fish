"""Tests for partial success mode in the Orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.models import AgentSlot, AgentState, Subtask, SubtaskResult, SubtaskState, Task, TaskState
from horse_fish.orchestrator.engine import Orchestrator


@pytest.fixture
def mock_pool():
    """Mock AgentPool."""
    pool = AsyncMock()
    pool._get_slot = MagicMock()
    pool._worktrees = AsyncMock()
    pool.runtime_observation_summary = MagicMock(
        return_value={
            "total_count": 0,
            "tool_count": 0,
            "prompt_count": 0,
            "first_observed_at": None,
            "last_observed_at": None,
            "subtasks_with_runtime_observations": 0,
            "subtask_ids": [],
            "subtask_breakdown": [],
            "runtimes": {},
            "observation_names": {},
            "recent_observations": [],
        }
    )
    return pool


@pytest.fixture
def mock_planner():
    """Mock Planner."""
    return AsyncMock()


@pytest.fixture
def mock_gates():
    """Mock ValidationGates."""
    gates = AsyncMock()
    gates.all_passed = MagicMock(return_value=True)
    return gates


async def mock_sleep(seconds):
    """Mock sleep that returns immediately."""
    return None


def _make_orchestrator(mock_pool, mock_planner, mock_gates, *, allow_partial_success: bool = False):
    return Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        model="claude-sonnet-4.6",
        max_agents=4,
        stall_timeout_seconds=30,
        allow_partial_success=allow_partial_success,
    )


def test_partial_success_state_in_enum():
    """TaskState.partial_success exists and has the expected value."""
    assert TaskState.partial_success == "partial_success"
    assert TaskState.partial_success in list(TaskState)


@pytest.mark.asyncio
async def test_partial_success_mode_succeeds_with_some_failures(mock_pool, mock_planner, mock_gates):
    """When allow_partial_success=True and some subtasks succeed, run state is partial_success."""
    orch = _make_orchestrator(mock_pool, mock_planner, mock_gates, allow_partial_success=True)

    # One subtask done, one failed
    s1 = Subtask(id="s1", description="Task 1")
    s1.state = SubtaskState.done
    s1.agent = "agent-1"
    s1.result = SubtaskResult(subtask_id="s1", success=True, output="ok", diff="commit1", duration_seconds=5.0)

    s2 = Subtask(id="s2", description="Task 2", max_retries=0)
    s2.state = SubtaskState.running
    s2.agent = "agent-2"
    s2.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)  # Stalled, will fail

    run = Task.create("test task")
    run.subtasks = [s1, s2]
    run.state = TaskState.executing

    mock_pool.release = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.busy)
    mock_pool.check_heartbeat = AsyncMock(return_value=False)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(subtask_id="s2", success=False, output="", diff="", duration_seconds=0)
    )

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orch._execute(run)

    # Should transition to reviewing (partial success), not failed
    assert result.state == TaskState.reviewing
    assert s1.state == SubtaskState.done
    assert s2.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_partial_success_disabled_fails_on_any_failure(mock_pool, mock_planner, mock_gates):
    """When allow_partial_success=False (default), any failure results in run failed."""
    orch = _make_orchestrator(mock_pool, mock_planner, mock_gates, allow_partial_success=False)

    s1 = Subtask(id="s1", description="Task 1")
    s1.state = SubtaskState.done
    s1.agent = "agent-1"
    s1.result = SubtaskResult(subtask_id="s1", success=True, output="ok", diff="commit1", duration_seconds=5.0)

    s2 = Subtask(id="s2", description="Task 2", max_retries=0)
    s2.state = SubtaskState.running
    s2.agent = "agent-2"
    s2.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)

    run = Task.create("test task")
    run.subtasks = [s1, s2]
    run.state = TaskState.executing

    mock_pool.release = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.busy)
    mock_pool.check_heartbeat = AsyncMock(return_value=False)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(subtask_id="s2", success=False, output="", diff="", duration_seconds=0)
    )

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orch._execute(run)

    assert result.state == TaskState.failed


@pytest.mark.asyncio
async def test_partial_success_all_failed_still_fails(mock_pool, mock_planner, mock_gates):
    """Even with partial success enabled, if ALL subtasks fail, run state is failed."""
    orch = _make_orchestrator(mock_pool, mock_planner, mock_gates, allow_partial_success=True)

    s1 = Subtask(id="s1", description="Task 1", max_retries=0)
    s1.state = SubtaskState.running
    s1.agent = "agent-1"
    s1.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)

    s2 = Subtask(id="s2", description="Task 2", max_retries=0)
    s2.state = SubtaskState.running
    s2.agent = "agent-2"
    s2.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)

    run = Task.create("test task")
    run.subtasks = [s1, s2]
    run.state = TaskState.executing

    mock_pool.release = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.busy)
    mock_pool.check_heartbeat = AsyncMock(return_value=False)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(subtask_id="x", success=False, output="", diff="", duration_seconds=0)
    )

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orch._execute(run)

    # All failed — even with partial success, should be failed
    assert result.state == TaskState.failed
    assert s1.state == SubtaskState.failed
    assert s2.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_partial_success_review_continues_with_passed_subtasks(mock_pool, mock_planner, mock_gates):
    """In review, if partial success enabled and at least one subtask passed gates, continue to merging."""
    from horse_fish.validation.gates import GateResult

    orch = _make_orchestrator(mock_pool, mock_planner, mock_gates, allow_partial_success=True)

    # s1 passes gates, s2 fails gates (retries exhausted)
    s1 = Subtask(id="s1", description="Task 1")
    s1.state = SubtaskState.done
    s1.agent = "agent-1"

    s2 = Subtask(id="s2", description="Task 2")
    s2.state = SubtaskState.done
    s2.agent = "agent-2"
    s2.gate_retry_count = 1
    s2.max_gate_retries = 1  # Exhausted

    run = Task.create("test task")
    run.subtasks = [s1, s2]
    run.state = TaskState.reviewing

    slot1 = AgentSlot(
        id="agent-1",
        name="hf-s1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/wt-1",
    )
    slot2 = AgentSlot(
        id="agent-2",
        name="hf-s2",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/wt-2",
    )

    def get_slot(agent_id):
        return {"agent-1": slot1, "agent-2": slot2}[agent_id]

    mock_pool._get_slot = MagicMock(side_effect=get_slot)

    # s1 passes, s2 fails gates
    call_count = 0

    async def run_all_side_effect(path):
        nonlocal call_count
        call_count += 1
        if path == "/tmp/wt-1":
            return []  # passes
        return [GateResult(gate="pytest", passed=False, output="1 failed", duration_seconds=1.0)]

    mock_gates.auto_fix_and_commit = AsyncMock(
        return_value=GateResult(gate="auto-fix", passed=True, output="ok", duration_seconds=0.1)
    )
    mock_gates.run_all = AsyncMock(side_effect=run_all_side_effect)

    def all_passed_side_effect(results):
        return len(results) == 0

    mock_gates.all_passed = MagicMock(side_effect=all_passed_side_effect)

    result = await orch._review(run)

    # Should proceed to merging, not fail
    assert result.state == TaskState.merging
    assert s1.state == SubtaskState.done
    assert s2.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_partial_success_merge_skips_failed_subtasks(mock_pool, mock_planner, mock_gates):
    """In merge, failed subtasks are skipped when partial success is enabled."""
    orch = _make_orchestrator(mock_pool, mock_planner, mock_gates, allow_partial_success=True)

    s1 = Subtask(id="s1", description="Task 1")
    s1.state = SubtaskState.done
    s1.agent = "agent-1"

    s2 = Subtask(id="s2", description="Task 2")
    s2.state = SubtaskState.failed  # Already failed
    s2.agent = "agent-2"

    run = Task.create("test task")
    run.subtasks = [s1, s2]
    run.state = TaskState.merging

    slot1 = AgentSlot(
        id="agent-1",
        name="hf-s1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/wt-1",
        branch="feat-s1",
    )
    mock_pool._get_slot = MagicMock(return_value=slot1)
    mock_pool._worktrees.merge = AsyncMock(return_value=(True, []))

    result = await orch._merge(run)

    # Should be partial_success since s2 is failed
    assert result.state == TaskState.partial_success
    # Only s1 should have been merged (s2 is failed, skipped by the loop condition)
    mock_pool._worktrees.merge.assert_called_once_with("hf-s1")
