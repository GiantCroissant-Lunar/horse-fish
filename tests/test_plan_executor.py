"""Tests for PlanExecutor — HTN+GOAP round loop."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from horse_fish.models import PlanState, Task, TaskState
from horse_fish.orchestrator.plan_executor import PlanExecutor
from horse_fish.planner.goal import GoalDecomposition, GoalEvaluation
from horse_fish.store.db import Store


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    store.migrate()
    return store


def _make_task(desc: str, state: TaskState = TaskState.completed) -> Task:
    t = Task.create(task=desc)
    t.state = state
    return t


@pytest.mark.asyncio
async def test_single_task_plan(tmp_path: Path) -> None:
    """Goal decomposes to 1 task, goal met after first round -> completed."""
    store = make_store(tmp_path)
    goal_planner = AsyncMock()
    goal_planner.decompose_goal.return_value = GoalDecomposition(
        goal_conditions=["tests pass"],
        task_descriptions=[{"description": "write tests", "deps": []}],
    )
    goal_planner.evaluate_goal.return_value = GoalEvaluation(goal_met=True, reasoning="all done")

    run_task_fn = AsyncMock(side_effect=lambda desc: _make_task(desc))

    executor = PlanExecutor(store=store, goal_planner=goal_planner, run_task_fn=run_task_fn)
    plan = await executor.execute("add tests")

    assert plan.state == PlanState.completed
    assert len(plan.tasks) == 1
    assert plan.tasks[0].task == "write tests"
    assert plan.completed_at is not None
    run_task_fn.assert_awaited_once_with("write tests")
    store.close()


@pytest.mark.asyncio
async def test_multi_round(tmp_path: Path) -> None:
    """First round: 1 task, goal not met -> second round: 1 more task, goal met."""
    store = make_store(tmp_path)
    goal_planner = AsyncMock()
    goal_planner.decompose_goal.return_value = GoalDecomposition(
        goal_conditions=["feature complete", "tests pass"],
        task_descriptions=[{"description": "implement feature", "deps": []}],
    )

    # First eval: not met, second eval: met
    goal_planner.evaluate_goal.side_effect = [
        GoalEvaluation(
            goal_met=False,
            reasoning="need tests",
            next_tasks=[{"description": "add tests", "deps": []}],
        ),
        GoalEvaluation(goal_met=True, reasoning="all done"),
    ]

    run_task_fn = AsyncMock(side_effect=lambda desc: _make_task(desc))

    executor = PlanExecutor(store=store, goal_planner=goal_planner, run_task_fn=run_task_fn)
    plan = await executor.execute("build feature with tests")

    assert plan.state == PlanState.completed
    assert len(plan.tasks) == 2
    assert plan.tasks[0].task == "implement feature"
    assert plan.tasks[1].task == "add tests"
    assert plan.round == 1
    store.close()


@pytest.mark.asyncio
async def test_max_rounds_exceeded(tmp_path: Path) -> None:
    """Goal never met -> failed after max_rounds."""
    store = make_store(tmp_path)
    goal_planner = AsyncMock()
    goal_planner.decompose_goal.return_value = GoalDecomposition(
        goal_conditions=["impossible"],
        task_descriptions=[{"description": "try something", "deps": []}],
    )
    goal_planner.evaluate_goal.return_value = GoalEvaluation(
        goal_met=False,
        reasoning="still not done",
        next_tasks=[{"description": "try again", "deps": []}],
    )

    run_task_fn = AsyncMock(side_effect=lambda desc: _make_task(desc))

    executor = PlanExecutor(store=store, goal_planner=goal_planner, run_task_fn=run_task_fn, max_rounds=2)
    plan = await executor.execute("impossible goal")

    assert plan.state == PlanState.failed
    assert plan.completed_at is not None
    # Should have executed 2 rounds worth of tasks
    assert len(plan.tasks) == 2
    store.close()


@pytest.mark.asyncio
async def test_task_failure_fails_plan(tmp_path: Path) -> None:
    """Task returns failed state -> plan fails immediately."""
    store = make_store(tmp_path)
    goal_planner = AsyncMock()
    goal_planner.decompose_goal.return_value = GoalDecomposition(
        goal_conditions=["works"],
        task_descriptions=[{"description": "broken task", "deps": []}],
    )

    run_task_fn = AsyncMock(side_effect=lambda desc: _make_task(desc, TaskState.failed))

    executor = PlanExecutor(store=store, goal_planner=goal_planner, run_task_fn=run_task_fn)
    plan = await executor.execute("do something")

    assert plan.state == PlanState.failed
    assert len(plan.tasks) == 1
    assert plan.tasks[0].state == TaskState.failed
    assert plan.completed_at is not None
    # evaluate_goal should NOT have been called since task failed
    goal_planner.evaluate_goal.assert_not_awaited()
    store.close()


@pytest.mark.asyncio
async def test_task_deps_respected(tmp_path: Path) -> None:
    """Two tasks with dependency, verify execution order."""
    store = make_store(tmp_path)
    goal_planner = AsyncMock()
    goal_planner.decompose_goal.return_value = GoalDecomposition(
        goal_conditions=["done"],
        task_descriptions=[
            {"description": "task B", "deps": ["task A"]},
            {"description": "task A", "deps": []},
        ],
    )
    goal_planner.evaluate_goal.return_value = GoalEvaluation(goal_met=True, reasoning="done")

    execution_order: list[str] = []

    async def track_order(desc: str) -> Task:
        execution_order.append(desc)
        return _make_task(desc)

    run_task_fn = AsyncMock(side_effect=track_order)

    executor = PlanExecutor(store=store, goal_planner=goal_planner, run_task_fn=run_task_fn)
    plan = await executor.execute("ordered work")

    assert plan.state == PlanState.completed
    assert execution_order == ["task A", "task B"]
    assert len(plan.tasks) == 2
    store.close()


@pytest.mark.asyncio
async def test_no_next_tasks_fails_plan(tmp_path: Path) -> None:
    """Goal not met but planner returns empty next_tasks -> failed."""
    store = make_store(tmp_path)
    goal_planner = AsyncMock()
    goal_planner.decompose_goal.return_value = GoalDecomposition(
        goal_conditions=["done"],
        task_descriptions=[{"description": "initial task", "deps": []}],
    )
    goal_planner.evaluate_goal.return_value = GoalEvaluation(
        goal_met=False,
        reasoning="not done but no ideas",
        next_tasks=[],
    )

    run_task_fn = AsyncMock(side_effect=lambda desc: _make_task(desc))

    executor = PlanExecutor(store=store, goal_planner=goal_planner, run_task_fn=run_task_fn)
    plan = await executor.execute("stuck goal")

    assert plan.state == PlanState.failed
    assert len(plan.tasks) == 1
    assert plan.completed_at is not None
    store.close()
