"""Tests for Orchestrator state machine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.agents.pool import AgentPool
from horse_fish.models import (
    AgentSlot,
    AgentState,
    Run,
    RunState,
    Subtask,
    SubtaskResult,
    SubtaskState,
)
from horse_fish.orchestrator.engine import Orchestrator
from horse_fish.planner.decompose import Planner
from horse_fish.validation.gates import GateResult, ValidationGates


@pytest.fixture
def mock_pool():
    pool = MagicMock(spec=AgentPool)
    pool.spawn = AsyncMock()
    pool.send_task = AsyncMock()
    pool.check_status = AsyncMock()
    pool.collect_result = AsyncMock()
    pool._get_slot = MagicMock()
    pool._worktrees = MagicMock()
    pool._worktrees.merge = AsyncMock(return_value=True)
    return pool


@pytest.fixture
def mock_planner():
    planner = MagicMock(spec=Planner)
    planner.decompose = AsyncMock()
    return planner


@pytest.fixture
def mock_gates():
    gates = MagicMock(spec=ValidationGates)
    gates.run_all = AsyncMock()
    gates.all_passed = MagicMock(return_value=True)
    return gates


@pytest.fixture
def orchestrator(mock_pool, mock_planner, mock_gates):
    return Orchestrator(pool=mock_pool, planner=mock_planner, gates=mock_gates, runtime="claude", max_agents=2)


@pytest.mark.asyncio
async def test_plan_success(orchestrator, mock_planner):
    """Test _plan success → state becomes executing with subtasks populated."""
    run = Run.create("test task")
    mock_planner.decompose.return_value = [
        Subtask(id="1", description="Subtask 1"),
        Subtask(id="2", description="Subtask 2"),
    ]

    result = await orchestrator._plan(run)

    assert result.state == RunState.executing
    assert len(result.subtasks) == 2
    assert result.subtasks[0].description == "Subtask 1"
    assert result.subtasks[1].description == "Subtask 2"


@pytest.mark.asyncio
async def test_plan_failure(orchestrator, mock_planner):
    """Test _plan failure → state becomes failed."""
    run = Run.create("test task")
    mock_planner.decompose.side_effect = Exception("LLM error")

    result = await orchestrator._plan(run)

    assert result.state == RunState.failed


@pytest.mark.asyncio
async def test_plan_empty_subtasks(orchestrator, mock_planner):
    """Test _plan empty subtasks → state becomes failed."""
    run = Run.create("test task")
    mock_planner.decompose.return_value = []

    result = await orchestrator._plan(run)

    assert result.state == RunState.failed


@pytest.mark.asyncio
async def test_execute_dispatches_and_polls(orchestrator, mock_pool):
    """Test _execute dispatches subtasks and polls for completion."""
    subtask1 = Subtask(id="1", description="Subtask 1", state=SubtaskState.pending)
    subtask2 = Subtask(id="2", description="Subtask 2", state=SubtaskState.pending)
    run = Run.create("test task")
    run.state = RunState.executing
    run.subtasks = [subtask1, subtask2]

    slot1 = AgentSlot(id="agent-1", name="agent-1", runtime="claude", model="model", capability="builder")
    slot2 = AgentSlot(id="agent-2", name="agent-2", runtime="claude", model="model", capability="builder")

    mock_pool.spawn.side_effect = [slot1, slot2]
    mock_pool.check_status.side_effect = [
        AgentState.busy,
        AgentState.busy,
        AgentState.dead,
        AgentState.dead,
    ]
    mock_pool.collect_result.side_effect = [
        SubtaskResult(subtask_id="1", success=True, output="output1", diff="diff1", duration_seconds=10),
        SubtaskResult(subtask_id="2", success=True, output="output2", diff="diff2", duration_seconds=10),
    ]

    result = await orchestrator._execute(run)

    assert result.state == RunState.reviewing
    assert mock_pool.spawn.call_count == 2
    assert result.subtasks[0].state == SubtaskState.done
    assert result.subtasks[1].state == SubtaskState.done


@pytest.mark.asyncio
async def test_execute_respects_dag_deps(orchestrator, mock_pool):
    """Test _execute respects DAG deps (blocked subtasks wait)."""
    subtask1 = Subtask(id="1", description="Subtask 1", state=SubtaskState.pending)
    subtask2 = Subtask(id="2", description="Subtask 2", deps=["Subtask 1"], state=SubtaskState.pending)
    run = Run.create("test task")
    run.state = RunState.executing
    run.subtasks = [subtask1, subtask2]

    slot1 = AgentSlot(id="agent-1", name="agent-1", runtime="claude", model="model", capability="builder")
    slot2 = AgentSlot(id="agent-2", name="agent-2", runtime="claude", model="model", capability="builder")

    mock_pool.spawn.side_effect = [slot1, slot2]
    mock_pool.check_status.side_effect = [
        AgentState.busy,
        AgentState.dead,
        AgentState.dead,
    ]
    mock_pool.collect_result.side_effect = [
        SubtaskResult(subtask_id="1", success=True, output="output1", diff="diff1", duration_seconds=10),
        SubtaskResult(subtask_id="2", success=True, output="output2", diff="diff2", duration_seconds=10),
    ]

    result = await orchestrator._execute(run)

    # First spawn should be for subtask1 (no deps)
    assert mock_pool.spawn.call_count == 2
    assert result.subtasks[0].state == SubtaskState.done
    assert result.subtasks[1].state == SubtaskState.done


@pytest.mark.asyncio
async def test_execute_spawn_failure(orchestrator, mock_pool):
    """Test _execute handles agent spawn failure."""
    subtask = Subtask(id="1", description="Subtask 1", state=SubtaskState.pending)
    run = Run.create("test task")
    run.state = RunState.executing
    run.subtasks = [subtask]

    mock_pool.spawn.side_effect = Exception("tmux error")

    result = await orchestrator._execute(run)

    assert result.state == RunState.failed
    assert result.subtasks[0].state == SubtaskState.failed


@pytest.mark.asyncio
async def test_execute_deadlock_detection(orchestrator, mock_pool):
    """Test _execute deadlock detection."""
    subtask = Subtask(id="1", description="Subtask 1", deps=["Nonexistent"], state=SubtaskState.pending)
    run = Run.create("test task")
    run.state = RunState.executing
    run.subtasks = [subtask]

    result = await orchestrator._execute(run)

    assert result.state == RunState.failed


@pytest.mark.asyncio
async def test_review_all_gates_pass(orchestrator, mock_pool, mock_gates):
    """Test _review all gates pass → state becomes merging."""
    subtask = Subtask(id="1", description="Subtask 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("test task")
    run.state = RunState.reviewing
    run.subtasks = [subtask]

    slot = AgentSlot(
        id="agent-1",
        name="agent-1",
        runtime="claude",
        model="model",
        capability="builder",
        worktree_path="/worktree",
    )
    mock_pool._get_slot.return_value = slot
    mock_gates.run_all.return_value = [
        GateResult(gate="compile", passed=True, output="ok", duration_seconds=1),
        GateResult(gate="pytest", passed=True, output="ok", duration_seconds=2),
    ]

    result = await orchestrator._review(run)

    assert result.state == RunState.merging


@pytest.mark.asyncio
async def test_review_gate_failure(orchestrator, mock_pool, mock_gates):
    """Test _review gate failure → state becomes failed."""
    subtask = Subtask(id="1", description="Subtask 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("test task")
    run.state = RunState.reviewing
    run.subtasks = [subtask]

    slot = AgentSlot(
        id="agent-1",
        name="agent-1",
        runtime="claude",
        model="model",
        capability="builder",
        worktree_path="/worktree",
    )
    mock_pool._get_slot.return_value = slot
    mock_gates.run_all.return_value = [
        GateResult(gate="compile", passed=False, output="error", duration_seconds=1),
    ]
    mock_gates.all_passed.return_value = False

    result = await orchestrator._review(run)

    assert result.state == RunState.failed
    assert result.subtasks[0].state == SubtaskState.failed


@pytest.mark.asyncio
async def test_merge_success(orchestrator, mock_pool):
    """Test _merge success → state becomes completed."""
    subtask = Subtask(id="1", description="Subtask 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("test task")
    run.state = RunState.merging
    run.subtasks = [subtask]

    slot = AgentSlot(id="agent-1", name="agent-1", runtime="claude", model="model", capability="builder")
    mock_pool._get_slot.return_value = slot
    mock_pool._worktrees.merge.return_value = True

    result = await orchestrator._merge(run)

    assert result.state == RunState.completed


@pytest.mark.asyncio
async def test_merge_conflict(orchestrator, mock_pool):
    """Test _merge conflict → state becomes failed."""
    subtask = Subtask(id="1", description="Subtask 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("test task")
    run.state = RunState.merging
    run.subtasks = [subtask]

    slot = AgentSlot(id="agent-1", name="agent-1", runtime="claude", model="model", capability="builder")
    mock_pool._get_slot.return_value = slot
    mock_pool._worktrees.merge.return_value = False

    result = await orchestrator._merge(run)

    assert result.state == RunState.failed
    assert result.subtasks[0].state == SubtaskState.failed


@pytest.mark.asyncio
async def test_run_full_lifecycle(orchestrator, mock_pool, mock_planner, mock_gates):
    """Test run() drives full lifecycle (plan → execute → review → merge → completed)."""
    subtasks = [
        Subtask(id="1", description="Subtask 1", state=SubtaskState.pending),
        Subtask(id="2", description="Subtask 2", state=SubtaskState.pending),
    ]
    mock_planner.decompose.return_value = subtasks

    slot1 = AgentSlot(id="agent-1", name="agent-1", runtime="claude", model="model", capability="builder")
    slot2 = AgentSlot(id="agent-2", name="agent-2", runtime="claude", model="model", capability="builder")
    mock_pool.spawn.side_effect = [slot1, slot2]
    mock_pool.check_status.side_effect = [
        AgentState.busy,
        AgentState.busy,
        AgentState.dead,
        AgentState.dead,
    ]
    mock_pool.collect_result.side_effect = [
        SubtaskResult(subtask_id="1", success=True, output="output1", diff="diff1", duration_seconds=10),
        SubtaskResult(subtask_id="2", success=True, output="output2", diff="diff2", duration_seconds=10),
    ]

    slot = AgentSlot(
        id="agent-1",
        name="agent-1",
        runtime="claude",
        model="model",
        capability="builder",
        worktree_path="/worktree",
    )
    mock_pool._get_slot.return_value = slot
    mock_gates.run_all.return_value = [
        GateResult(gate="compile", passed=True, output="ok", duration_seconds=1),
    ]

    result = await orchestrator.run("test task")

    assert result.state == RunState.completed
    assert result.completed_at is not None


def test_deps_met():
    """Test _deps_met static method."""
    run = Run.create("test")
    subtask1 = Subtask(id="1", description="Task 1", state=SubtaskState.done)
    subtask2 = Subtask(id="2", description="Task 2", deps=["Task 1"], state=SubtaskState.pending)
    run.subtasks = [subtask1, subtask2]

    assert Orchestrator._deps_met(run, subtask1)
    assert Orchestrator._deps_met(run, subtask2)
    # Task3 depends on Task2 which is not done
    task3 = Subtask(id="3", description="Task 3", deps=["Task 2"], state=SubtaskState.pending)
    assert not Orchestrator._deps_met(run, task3)
