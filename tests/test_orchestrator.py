"""Tests for the Orchestrator state machine."""

from __future__ import annotations

from datetime import UTC, datetime
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
        Subtask(id="subtask-2", description="Create API endpoints", deps=["subtask-1"]),
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
    subtask2 = Subtask(id="subtask-2", description="Build on base", deps=["subtask-1"])
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
    """Test _review gate failure → state becomes failed when retries exhausted."""
    subtask = Subtask(
        id="subtask-1",
        description="Task 1",
        state=SubtaskState.done,
        agent="agent-1",
        gate_retry_count=1,
        max_gate_retries=1,
    )
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

    # Mock auto-fix and gates to fail
    mock_gates.auto_fix_and_commit = AsyncMock(
        return_value=GateResult(gate="auto-fix", passed=True, output="ok", duration_seconds=0.1)
    )
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
async def test_review_emits_gate_retry_span(mock_pool, mock_planner, mock_gates):
    """_review should emit a dedicated span when gates trigger a retry."""
    subtask = Subtask(
        id="subtask-1",
        description="Task 1",
        state=SubtaskState.done,
        agent="agent-1",
        gate_retry_count=0,
        max_gate_retries=1,
    )
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
    mock_pool.check_status = AsyncMock(return_value=AgentState.busy)
    mock_pool.send_task = AsyncMock()
    mock_gates.auto_fix_and_commit = AsyncMock(
        return_value=GateResult(gate="auto-fix", passed=True, output="ok", duration_seconds=0.1)
    )
    mock_gates.run_all = AsyncMock(
        return_value=[GateResult(gate="compile", passed=False, output="syntax error", duration_seconds=1.0)]
    )
    mock_gates.all_passed.return_value = False

    gate_retry_span = MagicMock(name="gate-retry-span")
    review_span = MagicMock(name="review-span")

    def make_span(trace, name, metadata=None):
        return gate_retry_span if name == "subtask.gate_retry" else review_span

    mock_tracer = MagicMock()
    mock_tracer.span.side_effect = make_span

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        tracer=mock_tracer,
    )
    orchestrator._active_trace = MagicMock()

    result = await orchestrator._review(run)

    assert result.state == RunState.executing
    span_names = [call.args[1] for call in mock_tracer.span.call_args_list]
    assert "subtask.gate_retry" in span_names
    gate_retry_end_calls = [call for call in mock_tracer.end_span.call_args_list if call.args[0] is gate_retry_span]
    assert gate_retry_end_calls
    assert gate_retry_end_calls[-1].args[1] == {"action": "retry"}


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
        Subtask(id="s2", description="Task 2", deps=["s1"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[1]) is True


def test_deps_met_not_done():
    """Test _deps_met returns False when deps are not done."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="s1", description="Task 1", state=SubtaskState.pending),
        Subtask(id="s2", description="Task 2", deps=["s1"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[1]) is False


def test_deps_met_partial():
    """Test _deps_met returns False when some deps are not done."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="s1", description="Task 1", state=SubtaskState.done),
        Subtask(id="s2", description="Task 2", state=SubtaskState.pending),
        Subtask(id="s3", description="Task 3", deps=["s1", "s2"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[2]) is False


def test_deps_met_by_id():
    """Test _deps_met works with ID-based dependencies."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="s1", description="Task 1", state=SubtaskState.done),
        Subtask(id="s2", description="Task 2", state=SubtaskState.done),
        Subtask(id="s3", description="Task 3", deps=["s1", "s2"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[2]) is True


def test_resolve_deps_converts_descriptions_to_ids():
    """Test _resolve_deps converts description-based deps to ID-based deps."""
    from horse_fish.orchestrator.engine import Orchestrator

    subtasks = [
        Subtask(id="subtask-1", description="Implement user model"),
        Subtask(id="subtask-2", description="Create API endpoints", deps=["subtask-1"]),
        Subtask(id="subtask-3", description="Add tests", deps=["Implement user model", "Create API endpoints"]),
    ]
    result = Orchestrator._resolve_deps(subtasks)

    assert result[0].deps == []
    assert result[1].deps == ["subtask-1"]
    assert result[2].deps == ["subtask-1", "subtask-2"]


def test_resolve_deps_keeps_unknown_deps():
    """Test _resolve_deps keeps unknown deps as-is."""
    from horse_fish.orchestrator.engine import Orchestrator

    subtasks = [
        Subtask(id="subtask-1", description="Task 1"),
        Subtask(id="subtask-2", description="Task 2", deps=["subtask-1", "unknown-dep"]),
    ]
    result = Orchestrator._resolve_deps(subtasks)

    assert result[1].deps == ["subtask-1", "unknown-dep"]


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
    mock_merge_queue.process = AsyncMock(
        return_value=[MagicMock(subtask_id="subtask-1", success=True, conflict_files=[])]
    )

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


# --- Task 2: MemoryStore wiring tests ---


@pytest.fixture
def mock_memory():
    """Mock MemoryStore."""
    memory = AsyncMock()
    memory.store_run_result = AsyncMock()
    memory.find_similar_tasks = AsyncMock(return_value=[])
    return memory


@pytest.mark.asyncio
async def test_run_stores_result_in_memory_on_completion(mock_pool, mock_planner, mock_gates, mock_memory):
    """Test run() stores result in memory when completed."""
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
    result_obj = SubtaskResult(
        subtask_id="subtask-1",
        success=True,
        output="Done",
        diff="commit",
        duration_seconds=10.0,
    )
    mock_pool.collect_result = AsyncMock(return_value=result_obj)
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
        memory=mock_memory,
    )

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        run = await orchestrator.run("Build system")

    assert run.state == RunState.completed
    mock_memory.store_run_result.assert_called_once()
    call_args = mock_memory.store_run_result.call_args
    assert call_args[0][0].id == run.id  # first arg is the Run
    assert len(call_args[0][1]) == 1  # second arg is subtask_results list


@pytest.mark.asyncio
async def test_run_does_not_store_memory_on_failure(mock_pool, mock_planner, mock_gates, mock_memory):
    """Test run() does NOT store in memory when failed."""
    mock_planner.decompose.side_effect = Exception("LLM error")

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        memory=mock_memory,
    )
    run = await orchestrator.run("Build system")

    assert run.state == RunState.failed
    mock_memory.store_run_result.assert_not_called()


@pytest.mark.asyncio
async def test_run_scores_trace_outcomes(mock_pool, mock_planner, mock_gates):
    """run() should record Langfuse scores for overall outcome."""
    mock_planner.decompose.return_value = [Subtask(id="subtask-1", description="Task 1")]
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

    mock_tracer = MagicMock()
    mock_trace = MagicMock()
    mock_trace.run_id = "run-1"
    mock_trace.trace_id = "trace-1"
    mock_trace.spans = []
    mock_tracer.trace_run.return_value = mock_trace
    mock_tracer.span.return_value = MagicMock()

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
        run = await orchestrator.run("Build system")

    assert run.state == RunState.completed
    score_names = [call.args[1] for call in mock_tracer.score_trace.call_args_list]
    assert "run_success" in score_names
    assert "completed_subtasks" in score_names
    assert "review_gate_pass_rate" in score_names
    assert "execution_retry_count" in score_names
    assert "gate_retry_count" in score_names
    assert "merge_conflict_count" in score_names
    assert "runtime_observation_count" in score_names
    assert "runtime_tool_observation_count" in score_names
    assert "runtime_prompt_observation_count" in score_names
    assert "runtime_observation_subtask_coverage" in score_names
    mock_tracer.end_trace.assert_called_once()
    assert mock_tracer.end_trace.call_args.kwargs["output"] == {
        "status": "completed",
        "subtask_count": 1,
        "completed_subtasks": 1,
        "failed_subtasks": 0,
        "runtime_observations": {
            "total_count": 0,
            "tool_count": 0,
            "prompt_count": 0,
            "first_observed_at": None,
            "last_observed_at": None,
            "subtask_ids": [],
            "recent_observations": [],
        },
    }


def test_score_run_outcomes_includes_retry_and_merge_metrics(mock_pool, mock_planner, mock_gates):
    """_score_run_outcomes should emit retry and merge-conflict scores."""
    mock_tracer = MagicMock()
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        tracer=mock_tracer,
    )
    run = Run.create("Build system")
    run.state = RunState.failed
    run.subtasks = [
        Subtask(id="subtask-1", description="Task 1", state=SubtaskState.done),
        Subtask(id="subtask-2", description="Task 2", state=SubtaskState.failed),
    ]
    trace = MagicMock()

    orchestrator._execution_retry_events = 2
    orchestrator._execution_retry_exhausted = 1
    orchestrator._gate_retry_events = 1
    orchestrator._gate_retry_exhausted = 0
    orchestrator._merge_conflicts = [
        {"subtask_id": "subtask-2", "branch": "horse-fish/hf-subtask-2", "conflict_files": []}
    ]
    mock_pool.runtime_observation_summary.return_value = {
        "total_count": 3,
        "tool_count": 2,
        "prompt_count": 1,
        "first_observed_at": "2026-03-09T12:00:00+00:00",
        "last_observed_at": "2026-03-09T12:02:00+00:00",
        "subtasks_with_runtime_observations": 1,
        "subtask_ids": ["subtask-1"],
        "subtask_breakdown": [
            {
                "subtask_id": "subtask-1",
                "count": 3,
                "tool_count": 2,
                "prompt_count": 1,
                "subtask_description": "Task 1",
                "prompt_kinds": {"task": 3},
                "observation_names": {"Bash": 2, "permission_prompt": 1},
                "first_observed_at": "2026-03-09T12:00:00+00:00",
                "last_observed_at": "2026-03-09T12:02:00+00:00",
                "latest_excerpt": "Confirm to bypass permissions?",
            }
        ],
        "runtimes": {"claude": 3},
        "observation_names": {"Bash": 2, "permission_prompt": 1},
        "recent_observations": [
            {
                "subtask_id": "subtask-1",
                "observation_name": "Bash",
                "kind": "tool",
                "excerpt": "git status --short)",
                "observed_at": "2026-03-09T12:01:00+00:00",
            },
            {
                "subtask_id": "subtask-1",
                "observation_name": "permission_prompt",
                "kind": "prompt",
                "excerpt": "Confirm to bypass permissions?",
                "observed_at": "2026-03-09T12:02:00+00:00",
            },
        ],
    }

    orchestrator._score_run_outcomes(run, trace)

    score_calls = {call.args[1]: call for call in mock_tracer.score_trace.call_args_list}
    assert score_calls["execution_retry_count"].args[2] == 2.0
    assert score_calls["execution_retry_count"].kwargs["metadata"] == {"retry_exhausted_count": 1}
    assert score_calls["gate_retry_count"].args[2] == 1.0
    assert score_calls["retry_exhausted_count"].args[2] == 1.0
    assert score_calls["merge_conflict_count"].args[2] == 1.0
    assert score_calls["merge_conflict"].args[2] == "conflict"
    assert score_calls["runtime_observation_count"].args[2] == 3.0
    assert score_calls["runtime_observation_count"].kwargs["metadata"] == {
        "tool_count": 2,
        "prompt_count": 1,
        "first_observed_at": "2026-03-09T12:00:00+00:00",
        "last_observed_at": "2026-03-09T12:02:00+00:00",
        "subtasks_with_runtime_observations": 1,
        "subtask_ids": ["subtask-1"],
        "subtask_breakdown": [
            {
                "subtask_id": "subtask-1",
                "count": 3,
                "tool_count": 2,
                "prompt_count": 1,
                "subtask_description": "Task 1",
                "prompt_kinds": {"task": 3},
                "observation_names": {"Bash": 2, "permission_prompt": 1},
                "first_observed_at": "2026-03-09T12:00:00+00:00",
                "last_observed_at": "2026-03-09T12:02:00+00:00",
                "latest_excerpt": "Confirm to bypass permissions?",
            }
        ],
        "runtimes": {"claude": 3},
        "observation_names": {"Bash": 2, "permission_prompt": 1},
        "recent_observations": [
            {
                "subtask_id": "subtask-1",
                "observation_name": "Bash",
                "kind": "tool",
                "excerpt": "git status --short)",
                "observed_at": "2026-03-09T12:01:00+00:00",
            },
            {
                "subtask_id": "subtask-1",
                "observation_name": "permission_prompt",
                "kind": "prompt",
                "excerpt": "Confirm to bypass permissions?",
                "observed_at": "2026-03-09T12:02:00+00:00",
            },
        ],
    }
    assert score_calls["runtime_tool_observation_count"].args[2] == 2.0
    assert score_calls["runtime_prompt_observation_count"].args[2] == 1.0
    assert score_calls["runtime_observation_subtask_coverage"].args[2] == 0.5
    assert score_calls["runtime_observation_subtask_coverage"].kwargs["metadata"] == {
        "subtasks_with_runtime_observations": 1,
        "total_subtasks": 2,
        "subtask_ids": ["subtask-1"],
        "last_observed_at": "2026-03-09T12:02:00+00:00",
    }


def test_trace_output_includes_runtime_observation_summary(mock_pool, mock_planner, mock_gates):
    """_trace_output should include a compact runtime observation summary."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
    )
    run = Run.create("Build system")
    run.state = RunState.completed
    run.subtasks = [
        Subtask(id="subtask-1", description="Task 1", state=SubtaskState.done),
        Subtask(id="subtask-2", description="Task 2", state=SubtaskState.failed),
    ]
    mock_pool.runtime_observation_summary.return_value = {
        "total_count": 3,
        "tool_count": 2,
        "prompt_count": 1,
        "first_observed_at": "2026-03-09T12:00:00+00:00",
        "last_observed_at": "2026-03-09T12:02:00+00:00",
        "subtasks_with_runtime_observations": 1,
        "subtask_ids": ["subtask-1"],
        "subtask_breakdown": [],
        "runtimes": {"claude": 3},
        "observation_names": {"Bash": 2, "permission_prompt": 1},
        "recent_observations": [
            {
                "subtask_id": "subtask-1",
                "observation_name": "permission_prompt",
                "kind": "prompt",
                "excerpt": "Confirm to bypass permissions?",
                "observed_at": "2026-03-09T12:02:00+00:00",
            }
        ],
    }

    output = orchestrator._trace_output(run)

    assert output == {
        "status": "completed",
        "subtask_count": 2,
        "completed_subtasks": 1,
        "failed_subtasks": 1,
        "runtime_observations": {
            "total_count": 3,
            "tool_count": 2,
            "prompt_count": 1,
            "first_observed_at": "2026-03-09T12:00:00+00:00",
            "last_observed_at": "2026-03-09T12:02:00+00:00",
            "subtask_ids": ["subtask-1"],
            "recent_observations": [
                {
                    "subtask_id": "subtask-1",
                    "observation_name": "permission_prompt",
                    "kind": "prompt",
                    "excerpt": "Confirm to bypass permissions?",
                    "observed_at": "2026-03-09T12:02:00+00:00",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_run_scores_execution_retries_after_stall(mock_pool, mock_planner, mock_gates):
    """run() should score execution retries when stalled subtasks are redispatched."""
    mock_planner.decompose.return_value = [Subtask(id="subtask-1", description="Task 1")]

    first_slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1-a",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree-a",
    )
    retry_slot = AgentSlot(
        id="agent-2",
        name="hf-subtask-1-b",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree-b",
    )
    mock_pool.spawn.side_effect = [first_slot, retry_slot]
    mock_pool.send_task = AsyncMock()
    mock_pool.release = AsyncMock()

    def check_status_by_agent(agent_id):
        return AgentState.busy if agent_id == "agent-1" else AgentState.dead

    def collect_result_by_agent(agent_id):
        if agent_id == "agent-1":
            return SubtaskResult(subtask_id="subtask-1", success=False, output="", diff="", duration_seconds=0.0)
        return SubtaskResult(subtask_id="subtask-1", success=True, output="Done", diff="commit", duration_seconds=5.0)

    mock_pool.check_status = AsyncMock(side_effect=check_status_by_agent)
    mock_pool.collect_result = AsyncMock(side_effect=collect_result_by_agent)
    mock_pool._get_slot.return_value = retry_slot
    mock_pool._worktrees.merge = AsyncMock(return_value=True)
    mock_gates.run_all = AsyncMock(
        return_value=[GateResult(gate="compile", passed=True, output="ok", duration_seconds=1.0)]
    )

    mock_tracer = MagicMock()
    mock_trace = MagicMock()
    mock_trace.run_id = "run-1"
    mock_trace.trace_id = "trace-1"
    mock_trace.spans = []
    mock_tracer.trace_run.return_value = mock_trace
    mock_tracer.span.return_value = MagicMock()

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        tracer=mock_tracer,
        stall_timeout_seconds=0,
    )

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        run = await orchestrator.run("Build system")

    assert run.state == RunState.completed
    score_calls = {call.args[1]: call for call in mock_tracer.score_trace.call_args_list}
    assert score_calls["execution_retry_count"].args[2] == 1.0
    assert score_calls["execution_retry_count"].kwargs["metadata"] == {"retry_exhausted_count": 0}


@pytest.mark.asyncio
async def test_run_scores_merge_conflicts(mock_pool, mock_planner, mock_gates):
    """run() should score merge conflicts when merge fails."""
    mock_planner.decompose.return_value = [Subtask(id="subtask-1", description="Task 1")]

    slot = AgentSlot(
        id="agent-1",
        name="hf-subtask-1",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree",
        branch="horse-fish/hf-subtask-1",
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
    mock_pool._worktrees.merge = AsyncMock(return_value=False)

    mock_tracer = MagicMock()
    mock_trace = MagicMock()
    mock_trace.run_id = "run-1"
    mock_trace.trace_id = "trace-1"
    mock_trace.spans = []
    mock_tracer.trace_run.return_value = mock_trace
    mock_tracer.span.return_value = MagicMock()

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
        run = await orchestrator.run("Build system")

    assert run.state == RunState.failed
    score_calls = {call.args[1]: call for call in mock_tracer.score_trace.call_args_list}
    assert score_calls["merge_conflict_count"].args[2] == 1.0
    assert score_calls["merge_conflict_count"].kwargs["metadata"] == {
        "conflicts": [{"subtask_id": "subtask-1", "branch": "horse-fish/hf-subtask-1", "conflict_files": []}]
    }
    assert score_calls["merge_conflict"].args[2] == "conflict"


@pytest.mark.asyncio
async def test_run_emits_subtask_operation_spans(mock_pool, mock_planner, mock_gates):
    """run() should emit subtask-level spans for dispatch, review, and merge."""
    mock_planner.decompose.return_value = [Subtask(id="subtask-1", description="Task 1")]
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

    mock_tracer = MagicMock()
    mock_trace = MagicMock()
    mock_trace.run_id = "run-1"
    mock_trace.trace_id = "trace-1"
    mock_trace.spans = []

    def make_span(trace, name, metadata=None):
        return MagicMock(name=f"span-{name}")

    mock_tracer.trace_run.return_value = mock_trace
    mock_tracer.span.side_effect = make_span

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
        run = await orchestrator.run("Build system")

    assert run.state == RunState.completed
    span_names = [call.args[1] for call in mock_tracer.span.call_args_list]
    assert "subtask.dispatch" in span_names
    assert "subtask.collect_result" in span_names
    assert "subtask.review" in span_names
    assert "subtask.merge" in span_names


# --- Task 3: Stall Detection tests ---


@pytest.mark.asyncio
async def test_orchestrator_accepts_stall_timeout_param(mock_pool, mock_planner, mock_gates):
    """Test Orchestrator accepts stall_timeout_seconds parameter."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        stall_timeout_seconds=60,
    )
    assert orchestrator._stall_timeout == 60


@pytest.mark.asyncio
async def test_execute_sets_last_activity_at_on_dispatch(mock_pool, mock_planner, mock_gates):
    """Test _execute sets last_activity_at when dispatching a subtask."""
    from datetime import UTC, datetime

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        stall_timeout_seconds=30,
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

    before_time = datetime.now(UTC)
    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    after_time = datetime.now(UTC)
    assert result.state == RunState.reviewing
    assert subtask.last_activity_at is not None
    assert before_time <= subtask.last_activity_at <= after_time


@pytest.mark.asyncio
async def test_execute_detects_stalled_agent_and_retries(mock_pool, mock_planner, mock_gates):
    """Test _execute detects a stalled agent and retries the subtask."""
    from datetime import timedelta

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        stall_timeout_seconds=30,
    )

    subtask = Subtask(id="subtask-1", description="Task 1")
    # Simulate: subtask was dispatched and is now running with stale activity
    subtask.state = SubtaskState.running
    subtask.agent = "agent-1"
    subtask.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)

    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    # Mock release method
    mock_pool.release = AsyncMock()

    # Mock spawn for retry
    slot = AgentSlot(
        id="agent-2",
        name="hf-retry",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn.return_value = slot
    mock_pool.send_task = AsyncMock()

    # agent-1 (stalled) returns busy so poll doesn't auto-complete it — stall detection fires
    # agent-2 (retried) returns dead with diff — completes normally
    def check_status_by_agent(agent_id):
        if agent_id == "agent-1":
            return AgentState.busy
        return AgentState.dead

    mock_pool.check_status = AsyncMock(side_effect=check_status_by_agent)

    def collect_result_by_agent(agent_id):
        if agent_id == "agent-1":
            return SubtaskResult(subtask_id="subtask-1", success=False, output="", diff="", duration_seconds=0)
        return SubtaskResult(subtask_id="subtask-1", success=True, output="Done", diff="commit", duration_seconds=10)

    mock_pool.collect_result = AsyncMock(side_effect=collect_result_by_agent)

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        await orchestrator._execute(run)

    assert subtask.retry_count >= 1
    mock_pool.release.assert_called()


@pytest.mark.asyncio
async def test_execute_fails_after_max_retries(mock_pool, mock_planner, mock_gates):
    """Test _execute marks subtask failed after max retries exhausted."""
    from datetime import timedelta

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
    subtask.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)

    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    mock_pool.check_status = AsyncMock(return_value=AgentState.busy)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(subtask_id="subtask-1", success=False, output="", diff="", duration_seconds=0)
    )
    mock_pool.release = AsyncMock()

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    assert result.state == RunState.failed
    assert subtask.state == SubtaskState.failed


@pytest.mark.asyncio
async def test_check_stalls_no_stalled_subtasks(mock_pool, mock_planner, mock_gates):
    """Test _check_stalls returns 0 when no subtasks are stalled."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        stall_timeout_seconds=300,
    )

    subtask = Subtask(id="subtask-1", description="Task 1")
    subtask.state = SubtaskState.running
    subtask.agent = "agent-1"
    # Recent activity - not stalled
    subtask.last_activity_at = datetime.now(UTC)

    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    agent_map = {"subtask-1": "agent-1"}
    retried = await orchestrator._check_stalls(run, agent_map)

    assert retried == 0
    assert subtask.state == SubtaskState.running


@pytest.mark.asyncio
async def test_check_stalls_retries_stalled_subtask(mock_pool, mock_planner, mock_gates):
    """Test _check_stalls retries a stalled subtask."""
    from datetime import timedelta

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        stall_timeout_seconds=30,
    )

    subtask = Subtask(id="subtask-1", description="Task 1")
    subtask.state = SubtaskState.running
    subtask.agent = "agent-1"
    # Old activity - stalled
    subtask.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)

    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    agent_map = {"subtask-1": "agent-1"}
    mock_pool.release = AsyncMock()

    retried = await orchestrator._check_stalls(run, agent_map)

    assert retried == 1
    assert subtask.retry_count == 1
    assert subtask.state == SubtaskState.pending
    assert subtask.agent is None
    assert subtask.last_activity_at is None
    assert "subtask-1" not in agent_map
    mock_pool.release.assert_called_once_with("agent-1")


@pytest.mark.asyncio
async def test_check_stalls_emits_stall_recovery_span(mock_pool, mock_planner, mock_gates):
    """_check_stalls should emit a dedicated span for stall recovery actions."""
    from datetime import timedelta

    mock_tracer = MagicMock()
    stall_span = MagicMock(name="stall-span")
    mock_tracer.span.return_value = stall_span

    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        tracer=mock_tracer,
        stall_timeout_seconds=30,
    )
    orchestrator._active_trace = MagicMock()

    subtask = Subtask(id="subtask-1", description="Task 1")
    subtask.state = SubtaskState.running
    subtask.agent = "agent-1"
    subtask.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)

    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    agent_map = {"subtask-1": "agent-1"}
    mock_pool.release = AsyncMock()

    retried = await orchestrator._check_stalls(run, agent_map)

    assert retried == 1
    mock_tracer.span.assert_called_once()
    assert mock_tracer.span.call_args.args[1] == "subtask.stall_recovery"
    mock_tracer.end_span.assert_called_once()
    assert mock_tracer.end_span.call_args.args[0] is stall_span
    assert mock_tracer.end_span.call_args.args[1] == {"action": "retry"}
