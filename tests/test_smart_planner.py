"""Tests for SmartPlanner with complexity classification and ceremony stripping."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.models import Subtask, TaskComplexity
from horse_fish.planner.smart import SmartPlanner


def make_subtask(description: str) -> Subtask:
    return Subtask(id=str(uuid.uuid4()), description=description)


# --- _parse_complexity ---


def test_parse_complexity_solo():
    sp = SmartPlanner.__new__(SmartPlanner)
    assert sp._parse_complexity("SOLO") == TaskComplexity.SOLO


def test_parse_complexity_trio():
    sp = SmartPlanner.__new__(SmartPlanner)
    assert sp._parse_complexity("TRIO") == TaskComplexity.TRIO


def test_parse_complexity_squad():
    sp = SmartPlanner.__new__(SmartPlanner)
    assert sp._parse_complexity("SQUAD") == TaskComplexity.SQUAD


def test_parse_complexity_case_insensitive():
    sp = SmartPlanner.__new__(SmartPlanner)
    assert sp._parse_complexity("solo") == TaskComplexity.SOLO
    assert sp._parse_complexity("  TRIO  ") == TaskComplexity.TRIO


def test_parse_complexity_embedded_in_text():
    sp = SmartPlanner.__new__(SmartPlanner)
    assert sp._parse_complexity("The answer is TRIO for this task") == TaskComplexity.TRIO


def test_parse_complexity_defaults_solo_on_garbage():
    sp = SmartPlanner.__new__(SmartPlanner)
    assert sp._parse_complexity("unknown stuff") == TaskComplexity.SOLO


def test_parse_complexity_empty_defaults_solo():
    sp = SmartPlanner.__new__(SmartPlanner)
    assert sp._parse_complexity("") == TaskComplexity.SOLO


# --- _strip_ceremony ---


def test_strip_ceremony_removes_commit_subtask():
    sp = SmartPlanner.__new__(SmartPlanner)
    subtasks = [
        make_subtask("Implement the feature"),
        make_subtask("Commit all changes"),
    ]
    result = sp._strip_ceremony(subtasks)
    assert len(result) == 1
    assert result[0].description == "Implement the feature"


def test_strip_ceremony_removes_various_patterns():
    sp = SmartPlanner.__new__(SmartPlanner)
    ceremony_descriptions = [
        "Run tests",
        "Review the code",
        "Format the codebase",
        "Lint source files",
        "Verify everything works",
        "commit changes to git",
    ]
    subtasks = [make_subtask(d) for d in ceremony_descriptions]
    result = sp._strip_ceremony(subtasks)
    assert result == []


def test_strip_ceremony_keeps_real_work():
    sp = SmartPlanner.__new__(SmartPlanner)
    subtasks = [
        make_subtask("Add user authentication endpoint"),
        make_subtask("Create database migration for users table"),
    ]
    result = sp._strip_ceremony(subtasks)
    assert len(result) == 2


def test_strip_ceremony_mixed():
    sp = SmartPlanner.__new__(SmartPlanner)
    subtasks = [
        make_subtask("Implement login API"),
        make_subtask("Test the login flow"),
        make_subtask("Update user profile page"),
        make_subtask("Commit and push changes"),
    ]
    result = sp._strip_ceremony(subtasks)
    descriptions = [s.description for s in result]
    assert "Implement login API" in descriptions
    assert "Update user profile page" in descriptions
    assert "Test the login flow" not in descriptions
    assert "Commit and push changes" not in descriptions


# --- _get_lessons ---


@pytest.mark.asyncio
async def test_get_lessons_with_store():
    sp = SmartPlanner.__new__(SmartPlanner)
    mock_store = AsyncMock()
    mock_store.get_lessons = AsyncMock(return_value="Previous lesson: keep it simple")
    sp._lesson_store = mock_store
    result = await sp._get_lessons("my task")
    assert result == "Previous lesson: keep it simple"
    mock_store.get_lessons.assert_called_once_with("my task")


@pytest.mark.asyncio
async def test_get_lessons_without_store():
    sp = SmartPlanner.__new__(SmartPlanner)
    sp._lesson_store = None
    result = await sp._get_lessons("my task")
    assert result == ""


# --- _classify ---


@pytest.mark.asyncio
async def test_classify_returns_complexity():
    sp = SmartPlanner.__new__(SmartPlanner)
    mock_planner = MagicMock()
    mock_planner._build_command = MagicMock(return_value=["echo", "TRIO"])
    mock_planner._run_cli = AsyncMock(return_value="TRIO")
    sp._planner = mock_planner
    sp._lesson_store = None

    result = await sp._classify("build a feature", "some context", "")
    assert result == TaskComplexity.TRIO


@pytest.mark.asyncio
async def test_classify_fallback_solo_on_error():
    sp = SmartPlanner.__new__(SmartPlanner)
    mock_planner = MagicMock()
    mock_planner._build_command = MagicMock(return_value=["false"])
    mock_planner._run_cli = AsyncMock(side_effect=Exception("LLM error"))
    sp._planner = mock_planner
    sp._lesson_store = None

    result = await sp._classify("some task", "", "")
    assert result == TaskComplexity.SOLO


# --- decompose ---


@pytest.mark.asyncio
async def test_decompose_returns_subtasks_and_complexity():
    mock_planner = MagicMock()
    subtasks = [make_subtask("Write code"), make_subtask("Update README")]
    mock_planner.decompose = AsyncMock(return_value=subtasks)
    mock_planner._build_command = MagicMock(return_value=["echo", "TRIO"])
    mock_planner._run_cli = AsyncMock(return_value="TRIO")

    sp = SmartPlanner.__new__(SmartPlanner)
    sp._planner = mock_planner
    sp._lesson_store = None

    result_subtasks, complexity = await sp.decompose("build something", "context")
    assert complexity == TaskComplexity.TRIO
    assert len(result_subtasks) == 2


@pytest.mark.asyncio
async def test_decompose_caps_solo_to_1():
    mock_planner = MagicMock()
    subtasks = [make_subtask("Do A"), make_subtask("Do B"), make_subtask("Do C")]
    mock_planner.decompose = AsyncMock(return_value=subtasks)
    mock_planner._build_command = MagicMock(return_value=["echo", "SOLO"])
    mock_planner._run_cli = AsyncMock(return_value="SOLO")

    sp = SmartPlanner.__new__(SmartPlanner)
    sp._planner = mock_planner
    sp._lesson_store = None

    result_subtasks, complexity = await sp.decompose("simple task")
    assert complexity == TaskComplexity.SOLO
    assert len(result_subtasks) == 1


@pytest.mark.asyncio
async def test_decompose_caps_trio_to_3():
    mock_planner = MagicMock()
    subtasks = [make_subtask(f"Do {i}") for i in range(6)]
    mock_planner.decompose = AsyncMock(return_value=subtasks)
    mock_planner._build_command = MagicMock(return_value=["echo", "TRIO"])
    mock_planner._run_cli = AsyncMock(return_value="TRIO")

    sp = SmartPlanner.__new__(SmartPlanner)
    sp._planner = mock_planner
    sp._lesson_store = None

    result_subtasks, _ = await sp.decompose("medium task")
    assert len(result_subtasks) == 3


@pytest.mark.asyncio
async def test_decompose_caps_squad_to_8():
    mock_planner = MagicMock()
    subtasks = [make_subtask(f"Task {i}") for i in range(15)]
    mock_planner.decompose = AsyncMock(return_value=subtasks)
    mock_planner._build_command = MagicMock(return_value=["echo", "SQUAD"])
    mock_planner._run_cli = AsyncMock(return_value="SQUAD")

    sp = SmartPlanner.__new__(SmartPlanner)
    sp._planner = mock_planner
    sp._lesson_store = None

    result_subtasks, _ = await sp.decompose("large task")
    assert len(result_subtasks) == 8


@pytest.mark.asyncio
async def test_decompose_fallback_on_decompose_failure():
    mock_planner = MagicMock()
    mock_planner.decompose = AsyncMock(side_effect=Exception("LLM down"))
    mock_planner._build_command = MagicMock(return_value=["echo", "SOLO"])
    mock_planner._run_cli = AsyncMock(return_value="SOLO")

    sp = SmartPlanner.__new__(SmartPlanner)
    sp._planner = mock_planner
    sp._lesson_store = None

    result_subtasks, complexity = await sp.decompose("a task")
    assert len(result_subtasks) == 1
    assert result_subtasks[0].description == "a task"
    assert complexity == TaskComplexity.SOLO


@pytest.mark.asyncio
async def test_decompose_fallback_on_empty_result():
    mock_planner = MagicMock()
    mock_planner.decompose = AsyncMock(return_value=[])
    mock_planner._build_command = MagicMock(return_value=["echo", "TRIO"])
    mock_planner._run_cli = AsyncMock(return_value="TRIO")

    sp = SmartPlanner.__new__(SmartPlanner)
    sp._planner = mock_planner
    sp._lesson_store = None

    result_subtasks, complexity = await sp.decompose("a task")
    assert len(result_subtasks) == 1
    assert result_subtasks[0].description == "a task"


@pytest.mark.asyncio
async def test_decompose_fallback_when_all_ceremony():
    mock_planner = MagicMock()
    subtasks = [make_subtask("Commit changes"), make_subtask("Run tests"), make_subtask("Lint code")]
    mock_planner.decompose = AsyncMock(return_value=subtasks)
    mock_planner._build_command = MagicMock(return_value=["echo", "TRIO"])
    mock_planner._run_cli = AsyncMock(return_value="TRIO")

    sp = SmartPlanner.__new__(SmartPlanner)
    sp._planner = mock_planner
    sp._lesson_store = None

    result_subtasks, _ = await sp.decompose("do something")
    assert len(result_subtasks) == 1
    assert result_subtasks[0].description == "do something"


@pytest.mark.asyncio
async def test_decompose_strips_ceremony_before_capping():
    mock_planner = MagicMock()
    subtasks = [
        make_subtask("Implement feature A"),
        make_subtask("Implement feature B"),
        make_subtask("Commit all changes"),  # ceremony
        make_subtask("Run tests"),  # ceremony
    ]
    mock_planner.decompose = AsyncMock(return_value=subtasks)
    mock_planner._build_command = MagicMock(return_value=["echo", "TRIO"])
    mock_planner._run_cli = AsyncMock(return_value="TRIO")

    sp = SmartPlanner.__new__(SmartPlanner)
    sp._planner = mock_planner
    sp._lesson_store = None

    result_subtasks, _ = await sp.decompose("do stuff")
    descriptions = [s.description for s in result_subtasks]
    assert "Implement feature A" in descriptions
    assert "Implement feature B" in descriptions
    assert "Commit all changes" not in descriptions
    assert "Run tests" not in descriptions


@pytest.mark.asyncio
async def test_smart_planner_constructor():
    sp = SmartPlanner(runtime="claude")
    assert sp._planner is not None
    assert sp._lesson_store is None


@pytest.mark.asyncio
async def test_smart_planner_constructor_with_lesson_store():
    from horse_fish.memory.lessons import LessonStore

    store = LessonStore()
    sp = SmartPlanner(runtime="claude", lesson_store=store)
    assert sp._lesson_store is store
