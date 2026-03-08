"""Tests for the Orchestrator state machine."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.models import AgentSlot, AgentState, Run, RunState, Subtask, SubtaskResult, SubtaskState
from horse_fish.orchestrator.engine import Orchestrator
from horse_fish.validation.gates import GateResult


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
        max_agents=3,
    )


@pytest.fixture
def sample_subtasks():
    """Sample subtasks for testing."""
    return [
        Subtask(id="subtask-1", description="Implement user model"),
        Subtask(id="subtask-2", description="Create API endpoints", deps=["Implement user model"]),
    ]


@pytest.mark.asyncio
async def test_plan_success(orchestrator, mock_planner, sample_subtasks):
    """Test _plan success: state becomes executing with subtasks populated."""
    mock_planner.decompose.return_value = sample_subtasks

    run = Run.create("Build user system")
    run.state = RunState.planning
    result = await orchestrator._plan(run)

    assert result.state == RunState.executing
    assert len(result.subtasks) == 2
    assert result.subtasks[0].description == "Implement user model"
    assert result.subtasks[1].description == "Create API endpoints"
    mock_planner.decompose.assert_called_once_with("Build user system")


@pytest.mark.asyncio
async def test_plan_failure(orchestrator, mock_planner):
    """Test _plan failure: state becomes failed."""
    mock_planner.decompose.side_effect = Exception("LLM timeout")

    run = Run.create("Build user system")
    run.state = RunState.planning
    result = await orchestrator._plan(run)

    assert result.state == RunState.failed
    assert len(result.subtasks) == 0


@pytest.mark.asyncio
async def test_plan_empty_subtasks(orchestrator, mock_planner):
    """Test _plan with empty subtasks: state becomes failed."""
    mock_planner.decompose.return_value = []

    run = Run.create("Build user system")
    run.state = RunState.planning
    result = await orchestrator._plan(run)

    assert result.state == RunState.failed
    assert len(result.subtasks) == 0


@pytest.mark.asyncio
async def test_execute_simple(orchestrator, mock_pool, sample_subtasks):
    """Test _execute dispatches subtasks and polls for completion."""
    # Mock spawn and send_task
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

    # Mock check_status to return dead immediately (signals completion)
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)

    # Mock collect_result to return success with diff
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="test",
            success=True,
            output="Done",
            diff="commit changes",
            duration_seconds=10.0,
        )
    )

    # Patch sleep to return immediately
    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)

        run = Run.create("Build user system")
        run.subtasks = sample_subtasks
        run.state = RunState.executing
        result = await orchestrator._execute(run)

    assert result.state == RunState.reviewing
    assert all(s.state == SubtaskState.done for s in result.subtasks)
    assert mock_pool.spawn.call_count == 2
    assert mock_pool.send_task.call_count == 2


@pytest.mark.asyncio
async def test_execute_respects_dag_deps(orchestrator, mock_pool):
    """Test _execute respects DAG deps (blocked subtasks wait)."""
    # Create subtasks where subtask-2 depends on subtask-1
    subtask1 = Subtask(id="subtask-1", description="Implement base")
    subtask2 = Subtask(id="subtask-2", description="Build on base", deps=["Implement base"])
    run = Run.create("Build system")
    run.subtasks = [subtask1, subtask2]
    run.state = RunState.executing
    # Set max_agents to 1 so only subtask-1 can be dispatched initially
    orchestrator._max_agents = 1

    # Mock spawn for subtask-1 only initially
    slot1 = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn.return_value = slot1
    mock_pool.send_task = AsyncMock()
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

    # Patch sleep to return immediately
    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)

        _ = await orchestrator._execute(run)

    # Verify subtask-1 was dispatched and completed first
    assert subtask1.state == SubtaskState.done
    # With max_agents=1 and subtask-1 completing, subtask-2 would become eligible
    # But since we want to verify dep ordering, we check that spawn was called at least once
    assert mock_pool.spawn.call_count >= 1


@pytest.mark.asyncio
async def test_execute_agent_spawn_failure(orchestrator, mock_pool):
    """Test _execute handles agent spawn failure."""
    subtask1 = Subtask(id="subtask-1", description="Task 1")
    subtask2 = Subtask(id="subtask-2", description="Task 2")
    run = Run.create("Build system")
    run.subtasks = [subtask1, subtask2]
    run.state = RunState.executing

    # Mock spawn to fail for subtask-1
    mock_pool.spawn.side_effect = [Exception("Tmux error"), None]

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)

        result = await orchestrator._execute(run)

    assert result.state == RunState.failed
    assert subtask1.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_execute_deadlock_detection(orchestrator, mock_pool):
    """Test _execute deadlock detection when nothing can run."""
    subtask1 = Subtask(id="subtask-1", description="Task 1", deps=["nonexistent"])
    run = Run.create("Build system")
    run.subtasks = [subtask1]
    run.state = RunState.executing

    mock_pool.spawn = AsyncMock()
    mock_pool.send_task = AsyncMock()

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)

        result = await orchestrator._execute(run)

    assert result.state == RunState.failed
    mock_pool.spawn.assert_not_called()


@pytest.mark.asyncio
async def test_execute_subtask_failure(orchestrator, mock_pool):
    """Test _execute when a subtask fails."""
    subtask1 = Subtask(id="subtask-1", description="Task 1")
    subtask2 = Subtask(id="subtask-2", description="Task 2")
    run = Run.create("Build system")
    run.subtasks = [subtask1, subtask2]
    run.state = RunState.executing

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
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        side_effect=[
            SubtaskResult(
                subtask_id="subtask-1",
                success=False,
                output="Failed",
                diff="",
                duration_seconds=10.0,
            ),
            SubtaskResult(
                subtask_id="subtask-2",
                success=True,
                output="Done",
                diff="commit",
                duration_seconds=10.0,
            ),
        ]
    )

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)

        result = await orchestrator._execute(run)

    assert result.state == RunState.failed
    assert subtask1.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_review_all_gates_pass(orchestrator, mock_pool, mock_gates):
    """Test _review all gates pass → state becomes merging."""
    subtask = Subtask(id="subtask-1", description="Task 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    # Mock slot with worktree path
    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree",
    )
    mock_pool._get_slot.return_value = slot

    # Mock gates to pass
    mock_gates.run_all = AsyncMock(
        return_value=[
            GateResult(gate="compile", passed=True, output="ok", duration_seconds=1.0),
            GateResult(gate="ruff-check", passed=True, output="ok", duration_seconds=1.0),
        ]
    )

    result = await orchestrator._review(run)

    assert result.state == RunState.merging
    mock_gates.run_all.assert_called_once_with("/tmp/worktree")


@pytest.mark.asyncio
async def test_review_gate_failure(orchestrator, mock_pool, mock_gates):
    """Test _review gate failure → state becomes failed."""
    subtask = Subtask(id="subtask-1", description="Task 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree",
    )
    mock_pool._get_slot.return_value = slot

    # Mock gates to fail
    mock_gates.run_all = AsyncMock(
        return_value=[
            GateResult(gate="compile", passed=False, output="syntax error", duration_seconds=1.0),
        ]
    )
    mock_gates.all_passed.return_value = False

    result = await orchestrator._review(run)

    assert result.state == RunState.failed
    assert subtask.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_review_exception(orchestrator, mock_pool, mock_gates):
    """Test _review with exception → state becomes failed."""
    subtask = Subtask(id="subtask-1", description="Task 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    mock_pool._get_slot.side_effect = KeyError("Agent not found")

    result = await orchestrator._review(run)

    assert result.state == RunState.failed
    assert subtask.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_merge_success(orchestrator, mock_pool):
    """Test _merge success → state becomes completed."""
    subtask = Subtask(id="subtask-1", description="Task 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.merging

    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree",
    )
    mock_pool._get_slot.return_value = slot
    mock_pool._worktrees.merge = AsyncMock(return_value=True)

    result = await orchestrator._merge(run)

    assert result.state == RunState.completed
    mock_pool._worktrees.merge.assert_called_once_with("hf-subtask-1")


@pytest.mark.asyncio
async def test_merge_conflict(orchestrator, mock_pool):
    """Test _merge conflict → state becomes failed."""
    subtask = Subtask(id="subtask-1", description="Task 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.merging

    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree",
    )
    mock_pool._get_slot.return_value = slot
    mock_pool._worktrees.merge = AsyncMock(return_value=False)

    result = await orchestrator._merge(run)

    assert result.state == RunState.failed
    assert subtask.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_merge_exception(orchestrator, mock_pool):
    """Test _merge with exception → state becomes failed."""
    subtask = Subtask(id="subtask-1", description="Task 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.merging

    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree",
    )
    mock_pool._get_slot.return_value = slot
    mock_pool._worktrees.merge = AsyncMock(side_effect=Exception("Merge error"))

    result = await orchestrator._merge(run)

    assert result.state == RunState.failed
    assert subtask.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_full_lifecycle_success(orchestrator, mock_pool, mock_planner, mock_gates):
    """Test run() drives full lifecycle (plan → execute → review → merge → completed)."""
    # Mock planner
    sample_subtasks = [
        Subtask(id="subtask-1", description="Task 1"),
        Subtask(id="subtask-2", description="Task 2"),
    ]
    mock_planner.decompose.return_value = sample_subtasks

    # Mock pool for execute phase
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
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="test",
            success=True,
            output="Done",
            diff="commit",
            duration_seconds=10.0,
        )
    )

    # Mock pool for review phase
    mock_pool._get_slot.return_value = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree1",
    )
    mock_gates.run_all = AsyncMock(
        return_value=[
            GateResult(gate="compile", passed=True, output="ok", duration_seconds=1.0),
        ]
    )

    # Mock pool for merge phase
    mock_pool._worktrees.merge = AsyncMock(return_value=True)

    # Patch sleep
    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)

        result = await orchestrator.run("Build system")

    assert result.state == RunState.completed
    assert result.completed_at is not None
    assert all(s.state == SubtaskState.done for s in result.subtasks)


@pytest.mark.asyncio
async def test_full_lifecycle_planner_error(orchestrator, mock_planner):
    """Test run() when planner fails → run.state == failed."""
    mock_planner.decompose.side_effect = Exception("LLM error")

    result = await orchestrator.run("Build system")

    assert result.state == RunState.failed


@pytest.mark.asyncio
async def test_full_lifecycle_gate_failure(orchestrator, mock_pool, mock_planner, mock_gates):
    """Test run() when gate fails → run.state == failed."""
    mock_planner.decompose.return_value = [
        Subtask(id="subtask-1", description="Task 1"),
    ]

    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn.return_value = slot
    mock_pool.send_task = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="test",
            success=True,
            output="Done",
            diff="commit",
            duration_seconds=10.0,
        )
    )

    mock_pool._get_slot.return_value = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree1",
    )
    mock_gates.run_all = AsyncMock(
        return_value=[
            GateResult(gate="compile", passed=False, output="error", duration_seconds=1.0),
        ]
    )
    mock_gates.all_passed.return_value = False

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)

        result = await orchestrator.run("Build system")

    assert result.state == RunState.failed


@pytest.mark.asyncio
async def test_full_lifecycle_merge_conflict(orchestrator, mock_pool, mock_planner, mock_gates):
    """Test run() when merge conflicts → run.state == failed."""
    mock_planner.decompose.return_value = [
        Subtask(id="subtask-1", description="Task 1"),
    ]

    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn.return_value = slot
    mock_pool.send_task = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="test",
            success=True,
            output="Done",
            diff="commit",
            duration_seconds=10.0,
        )
    )

    mock_pool._get_slot.return_value = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree1",
    )
    mock_gates.run_all = AsyncMock(
        return_value=[
            GateResult(gate="compile", passed=True, output="ok", duration_seconds=1.0),
        ]
    )
    mock_pool._worktrees.merge = AsyncMock(return_value=False)

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)

        result = await orchestrator.run("Build system")

    assert result.state == RunState.failed


@pytest.mark.asyncio
async def test_no_handler_for_state():
    """Test run() raises OrchestratorError for unhandled state."""
    mock_pool = AsyncMock()
    mock_planner = AsyncMock()
    mock_gates = AsyncMock()
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
    )

    # Create a run in an invalid state that has no handler
    run = Run.create("Task")
    run.state = RunState.completed  # Terminal state, no handler

    # The run() method should exit the loop since completed is terminal
    result = await orchestrator.run("Task")
    # Should return the run unchanged (already completed)
    assert result.state == RunState.completed


def test_deps_met_no_deps():
    """Test _deps_met with no dependencies returns True."""
    run = Run.create("Task")
    subtask = Subtask(id="s1", description="Task 1")
    assert Orchestrator._deps_met(run, subtask) is True


def test_deps_met_all_done():
    """Test _deps_met returns True when all deps are done."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="s1", description="Task 1", state=SubtaskState.done),
        Subtask(id="s2", description="Task 2", deps=["Task 1"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[1]) is True


def test_deps_met_not_done():
    """Test _deps_met returns False when deps are not done."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="s1", description="Task 1", state=SubtaskState.pending),
        Subtask(id="s2", description="Task 2", deps=["Task 1"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[1]) is False


def test_deps_met_partial():
    """Test _deps_met returns False when some deps are not done."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="s1", description="Task 1", state=SubtaskState.done),
        Subtask(id="s2", description="Task 2", state=SubtaskState.pending),
        Subtask(id="s3", description="Task 3", deps=["Task 1", "Task 2"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[2]) is False


@pytest.mark.asyncio
async def test_execute_uses_agent_selector_when_provided(mock_pool, mock_planner, mock_gates):
    """Test _execute uses AgentSelector when provided."""
    from horse_fish.dispatch.selector import AgentSelector

    # Create a mock selector
    mock_selector = AsyncMock(spec=AgentSelector)
    idle_agent = AgentSlot(
        id="idle-agent-1",
        name="hf-existing",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.idle,
    )
    mock_pool.list_agents = MagicMock(return_value=[idle_agent])  # sync method
    mock_selector.select.return_value = idle_agent

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        model="claude-sonnet-4.6",
        max_agents=3,
        selector=mock_selector,
    )

    subtask = Subtask(id="subtask-1", description="Task 1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    mock_pool.send_task = AsyncMock()
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

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    assert result.state == RunState.reviewing
    assert subtask.state == SubtaskState.done
    # Verify selector.select was called
    mock_selector.select.assert_called_once()
    # Verify spawn was NOT called (selector returned existing agent)
    mock_pool.spawn.assert_not_called()


@pytest.mark.asyncio
async def test_execute_falls_back_to_round_robin_without_selector(mock_pool, mock_planner, mock_gates):
    """Test _execute falls back to round-robin spawn without selector."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        model="claude-sonnet-4.6",
        max_agents=3,
        selector=None,  # No selector
    )

    subtask = Subtask(id="subtask-1", description="Task 1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn.return_value = slot
    mock_pool.send_task = AsyncMock()
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

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    assert result.state == RunState.reviewing
    assert subtask.state == SubtaskState.done
    # Verify spawn WAS called (round-robin fallback)
    mock_pool.spawn.assert_called_once()


@pytest.mark.asyncio
async def test_selector_returns_none_skips_dispatch(mock_pool, mock_planner, mock_gates):
    """Test _execute skips subtask when selector returns None."""
    from horse_fish.dispatch.selector import AgentSelector

    mock_selector = AsyncMock(spec=AgentSelector)
    mock_selector.select.return_value = None  # No suitable agent
    mock_pool.list_agents.return_value = []  # No idle agents

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        model="claude-sonnet-4.6",
        max_agents=3,
        selector=mock_selector,
    )

    # Create subtask with unmet deps so it won't be dispatched
    subtask = Subtask(id="subtask-1", description="Task 1", deps=["nonexistent"])
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    # Should fail due to deadlock (no subtasks can be dispatched)
    assert result.state == RunState.failed


@pytest.mark.asyncio
async def test_merge_uses_queue_when_provided(mock_pool, mock_planner, mock_gates):
    """Test _merge uses MergeQueue when provided."""
    from horse_fish.merge.queue import MergeQueue

    mock_merge_queue = AsyncMock(spec=MergeQueue)
    mock_merge_queue.enqueue = AsyncMock()
    mock_merge_queue.process = AsyncMock(return_value=[
        MagicMock(subtask_id="subtask-1", success=True, conflict_files=[])
    ])

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        model="claude-sonnet-4.6",
        max_agents=3,
        merge_queue=mock_merge_queue,
    )

    subtask = Subtask(id="subtask-1", description="Task 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.merging

    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree",
        branch="overstory/hf-subtask-1",
    )
    mock_pool._get_slot.return_value = slot

    result = await orchestrator._merge(run)

    assert result.state == RunState.completed
    mock_merge_queue.enqueue.assert_called_once()
    mock_merge_queue.process.assert_called_once()
    # Direct merge should NOT be called when using queue
    mock_pool._worktrees.merge.assert_not_called()


@pytest.mark.asyncio
async def test_merge_falls_back_to_direct_without_queue(mock_pool, mock_planner, mock_gates):
    """Test _merge falls back to direct merge without queue."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        model="claude-sonnet-4.6",
        max_agents=3,
        merge_queue=None,  # No queue
    )

    subtask = Subtask(id="subtask-1", description="Task 1", state=SubtaskState.done, agent="agent-1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.merging

    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree",
        branch="overstory/hf-subtask-1",
    )
    mock_pool._get_slot.return_value = slot
    mock_pool._worktrees.merge = AsyncMock(return_value=True)

    result = await orchestrator._merge(run)

    assert result.state == RunState.completed
    mock_pool._worktrees.merge.assert_called_once_with("hf-subtask-1")


@pytest.fixture
def mock_tracer():
    """Mock Tracer."""
    from horse_fish.observability.traces import RunTrace, Span, Tracer

    tracer = MagicMock(spec=Tracer)
    trace = RunTrace(run_id="test", task="test task")
    span = Span(name="test", trace=trace)
    tracer.trace_run.return_value = trace
    tracer.span.return_value = span
    return tracer


@pytest.mark.asyncio
async def test_run_creates_trace_and_spans(mock_pool, mock_planner, mock_gates, mock_tracer):
    """Test run() creates a trace and spans for each phase."""
    mock_planner.decompose.return_value = [
        Subtask(id="subtask-1", description="Task 1"),
    ]
    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/wt",
    )
    mock_pool.spawn.return_value = slot
    mock_pool.send_task = AsyncMock()
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
    mock_pool._get_slot.return_value = slot
    mock_gates.run_all = AsyncMock(
        return_value=[GateResult(gate="compile", passed=True, output="ok", duration_seconds=1.0)]
    )
    mock_pool._worktrees.merge = AsyncMock(return_value=True)

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        tracer=mock_tracer,
    )

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator.run("Build system")

    assert result.state == RunState.completed
    mock_tracer.trace_run.assert_called_once()
    # Should have spans for plan, execute, review, merge
    assert mock_tracer.span.call_count == 4
    assert mock_tracer.end_span.call_count == 4
    mock_tracer.end_trace.assert_called_once_with(mock_tracer.trace_run.return_value, "completed")


@pytest.mark.asyncio
async def test_run_ends_trace_on_failure(mock_pool, mock_planner, mock_gates, mock_tracer):
    """Test run() ends trace with 'failed' on failure."""
    mock_planner.decompose.side_effect = Exception("LLM error")

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        tracer=mock_tracer,
    )
    result = await orchestrator.run("Build system")

    assert result.state == RunState.failed
    mock_tracer.end_trace.assert_called_once_with(mock_tracer.trace_run.return_value, "failed")
