"""Tests for GoalPlanner — HTN decomposition + GOAP evaluation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.planner.goal import GoalDecomposition, GoalEvaluation, GoalPlanner


@pytest.fixture
def mock_planner():
    """Create a mock Planner instance."""
    planner = MagicMock()
    planner._build_command = MagicMock(return_value=["echo", "test"])
    planner._run_cli = AsyncMock(return_value="[]")
    return planner


@pytest.fixture
def goal_planner(mock_planner):
    return GoalPlanner(mock_planner)


@pytest.mark.asyncio
async def test_generate_goal_conditions(goal_planner, mock_planner):
    """Mock LLM returns valid JSON array, verify parsed correctly."""
    conditions = ["All tests pass", "New endpoint responds 200", "Docs updated"]
    mock_planner._run_cli = AsyncMock(return_value=json.dumps(conditions))

    result = await goal_planner.generate_goal_conditions("Add a REST endpoint for users")

    assert result == conditions
    assert len(result) == 3
    mock_planner._build_command.assert_called_once()
    mock_planner._run_cli.assert_called_once()


@pytest.mark.asyncio
async def test_generate_goal_conditions_fallback(goal_planner, mock_planner):
    """Mock LLM returns garbage, verify fallback condition."""
    mock_planner._run_cli = AsyncMock(return_value="this is not json at all!!!")

    result = await goal_planner.generate_goal_conditions("Add a REST endpoint for users")

    assert len(result) == 1
    assert "Add a REST endpoint for users" in result[0]


@pytest.mark.asyncio
async def test_evaluate_goal_met(goal_planner, mock_planner):
    """Mock returns goal_met=true, verify GoalEvaluation."""
    response = json.dumps(
        {
            "goal_met": True,
            "reasoning": "All conditions satisfied",
            "next_tasks": [],
        }
    )
    mock_planner._run_cli = AsyncMock(return_value=response)

    result = await goal_planner.evaluate_goal(
        goal="Add user endpoint",
        goal_conditions=["Endpoint exists", "Tests pass"],
        completed_task_summaries=["Created /api/users endpoint", "Added unit tests"],
    )

    assert isinstance(result, GoalEvaluation)
    assert result.goal_met is True
    assert result.reasoning == "All conditions satisfied"
    assert result.next_tasks == []


@pytest.mark.asyncio
async def test_evaluate_goal_not_met_with_next_tasks(goal_planner, mock_planner):
    """Mock returns goal_met=false with next_tasks."""
    response = json.dumps(
        {
            "goal_met": False,
            "reasoning": "Missing documentation",
            "next_tasks": [
                {"description": "Write API docs for /api/users", "deps": []},
                {"description": "Add integration test", "deps": ["Write API docs for /api/users"]},
            ],
        }
    )
    mock_planner._run_cli = AsyncMock(return_value=response)

    result = await goal_planner.evaluate_goal(
        goal="Add user endpoint",
        goal_conditions=["Endpoint exists", "Tests pass", "Docs written"],
        completed_task_summaries=["Created endpoint", "Added unit tests"],
    )

    assert isinstance(result, GoalEvaluation)
    assert result.goal_met is False
    assert "documentation" in result.reasoning.lower()
    assert len(result.next_tasks) == 2
    assert result.next_tasks[0]["description"] == "Write API docs for /api/users"


@pytest.mark.asyncio
async def test_evaluate_goal_fallback(goal_planner, mock_planner):
    """Mock returns garbage, verify fallback to goal_met=False."""
    mock_planner._run_cli = AsyncMock(return_value="not valid json")

    result = await goal_planner.evaluate_goal(
        goal="Add user endpoint",
        goal_conditions=["Endpoint exists"],
        completed_task_summaries=["Did some work"],
    )

    assert isinstance(result, GoalEvaluation)
    assert result.goal_met is False


@pytest.mark.asyncio
async def test_decompose_goal_first_round(goal_planner, mock_planner):
    """Mock two LLM calls (conditions + tasks), verify GoalDecomposition."""
    conditions = ["Feature works", "Tests pass"]
    tasks = [
        {"description": "Implement feature X", "deps": [], "files_hint": ["src/x.py"]},
        {"description": "Write tests for X", "deps": ["Implement feature X"], "files_hint": ["tests/test_x.py"]},
    ]

    call_count = 0

    async def mock_run_cli(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return json.dumps(conditions)
        return json.dumps(tasks)

    mock_planner._run_cli = AsyncMock(side_effect=mock_run_cli)

    result = await goal_planner.decompose_goal("Build feature X")

    assert isinstance(result, GoalDecomposition)
    assert result.goal_conditions == conditions
    assert len(result.task_descriptions) == 2
    assert result.task_descriptions[0]["description"] == "Implement feature X"
    assert result.task_descriptions[1]["files_hint"] == ["tests/test_x.py"]


@pytest.mark.asyncio
async def test_decompose_goal_fallback(goal_planner, mock_planner):
    """When both LLM calls fail, verify fallback single task."""
    mock_planner._run_cli = AsyncMock(return_value="garbage")

    result = await goal_planner.decompose_goal("Build feature X")

    assert isinstance(result, GoalDecomposition)
    assert len(result.goal_conditions) == 1
    assert len(result.task_descriptions) == 1
    assert "Build feature X" in result.task_descriptions[0]["description"]


def test_parse_json_with_markdown_fences():
    """Verify fence stripping works for all parse helpers."""
    fenced = '```json\n["a", "b", "c"]\n```'
    result = GoalPlanner._parse_json_array(fenced)
    assert result == ["a", "b", "c"]

    fenced_obj = '```json\n{"key": "value"}\n```'
    result = GoalPlanner._parse_json_object(fenced_obj)
    assert result == {"key": "value"}

    fenced_arr_obj = '```json\n[{"x": 1}, {"x": 2}]\n```'
    result = GoalPlanner._parse_json_array_of_objects(fenced_arr_obj)
    assert result == [{"x": 1}, {"x": 2}]


def test_parse_json_array_plain():
    """Plain JSON without fences."""
    assert GoalPlanner._parse_json_array("[1, 2, 3]") == [1, 2, 3]


def test_parse_json_array_invalid():
    """Invalid JSON returns None."""
    assert GoalPlanner._parse_json_array("not json") is None


def test_parse_json_object_invalid():
    """Invalid JSON returns None."""
    assert GoalPlanner._parse_json_object("not json") is None


def test_parse_json_array_of_objects_invalid():
    """Invalid JSON returns None."""
    assert GoalPlanner._parse_json_array_of_objects("not json") is None


def test_parse_json_array_of_objects_not_objects():
    """Array of non-objects returns None."""
    assert GoalPlanner._parse_json_array_of_objects("[1, 2, 3]") is None
