"""Integration test: Plan -> GoalPlanner -> PlanExecutor -> mock orchestrator."""

from unittest.mock import MagicMock

import pytest

from horse_fish.models import PlanState, Task, TaskState
from horse_fish.orchestrator.plan_executor import PlanExecutor
from horse_fish.planner.goal import GoalPlanner


@pytest.fixture
def store(tmp_path):
    from horse_fish.store.db import Store

    s = Store(tmp_path / "test.db")
    s.migrate()
    yield s
    s.close()


@pytest.mark.asyncio
async def test_full_plan_lifecycle(store):
    """Plan goes through: planning -> executing -> replanning -> executing -> completed."""
    planner = MagicMock()
    planner.runtime = "claude"
    planner.model = "test"
    planner._tracer = None

    goal_planner = GoalPlanner(planner)

    call_count = {"run": 0}

    async def mock_run_task(desc):
        call_count["run"] += 1
        t = Task.create(desc)
        t.state = TaskState.completed
        return t

    # Mock LLM calls in sequence
    responses = iter(
        [
            # decompose_goal: generate_goal_conditions
            '["Feature works", "Tests pass"]',
            # decompose_goal: decompose tasks
            '[{"description": "Implement feature", "deps": []}]',
            # evaluate_goal round 0: not met
            '{"goal_met": false, "reasoning": "Need tests", "next_tasks": [{"description": "Add tests", "deps": []}]}',
            # evaluate_goal round 1: met
            '{"goal_met": true, "reasoning": "All done", "next_tasks": []}',
        ]
    )

    async def mock_run_cli(cmd, timeout=120.0):
        return next(responses)

    planner._run_cli = mock_run_cli
    planner._build_command = lambda prompt: ["echo", "test"]

    executor = PlanExecutor(
        store=store,
        goal_planner=goal_planner,
        run_task_fn=mock_run_task,
    )

    plan = await executor.execute("Build feature X with tests")
    assert plan.state == PlanState.completed
    assert len(plan.tasks) == 2
    assert call_count["run"] == 2

    # Verify persisted in SQLite
    db_plan = store.fetch_plan(plan.id)
    assert db_plan is not None
    assert db_plan["state"] == "completed"


@pytest.mark.asyncio
async def test_single_task_immediate_completion(store):
    """Simple goal: one task, immediately met."""
    planner = MagicMock()
    planner.runtime = "claude"
    planner.model = "test"
    planner._tracer = None

    goal_planner = GoalPlanner(planner)

    async def mock_run_task(desc):
        t = Task.create(desc)
        t.state = TaskState.completed
        return t

    responses = iter(
        [
            '["Bug is fixed"]',
            '[{"description": "Fix the bug", "deps": []}]',
            '{"goal_met": true, "reasoning": "Fixed", "next_tasks": []}',
        ]
    )

    async def mock_run_cli(cmd, timeout=120.0):
        return next(responses)

    planner._run_cli = mock_run_cli
    planner._build_command = lambda prompt: ["echo", "test"]

    executor = PlanExecutor(store=store, goal_planner=goal_planner, run_task_fn=mock_run_task)
    plan = await executor.execute("Fix the bug")

    assert plan.state == PlanState.completed
    assert len(plan.tasks) == 1
    assert plan.round == 0


@pytest.mark.asyncio
async def test_plan_fails_on_task_failure(store):
    """Plan fails when a task fails."""
    planner = MagicMock()
    planner.runtime = "claude"
    planner.model = "test"
    planner._tracer = None

    goal_planner = GoalPlanner(planner)

    async def mock_run_task(desc):
        t = Task.create(desc)
        t.state = TaskState.failed
        return t

    responses = iter(
        [
            '["Done"]',
            '[{"description": "Do thing", "deps": []}]',
        ]
    )

    async def mock_run_cli(cmd, timeout=120.0):
        return next(responses)

    planner._run_cli = mock_run_cli
    planner._build_command = lambda prompt: ["echo", "test"]

    executor = PlanExecutor(store=store, goal_planner=goal_planner, run_task_fn=mock_run_task)
    plan = await executor.execute("Do thing")

    assert plan.state == PlanState.failed
    assert len(plan.tasks) == 1

    # Verify persisted
    db_plan = store.fetch_plan(plan.id)
    assert db_plan is not None
    assert db_plan["state"] == "failed"
