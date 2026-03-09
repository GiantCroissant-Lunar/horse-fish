"""Tests for SmartPlanner — complexity classification and ceremony stripping."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.memory.lessons import Lesson, LessonStore
from horse_fish.models import Subtask, TaskComplexity
from horse_fish.planner.smart import SmartPlanner

# --- Helpers ---


def make_mock_planner():
    planner = MagicMock()
    planner.decompose = AsyncMock()
    planner._build_command = MagicMock(return_value=["claude", "--print", "-m", "model", "prompt"])
    planner._run_cli = AsyncMock()
    planner._tracer = None
    planner.runtime = "claude"
    planner.model = "claude-sonnet-4-6"
    return planner


def make_mock_lesson_store():
    store = MagicMock(spec=LessonStore)
    store.get_lessons_for_task = MagicMock(return_value=[])
    return store


# --- _parse_complexity ---


def test_parse_complexity_solo():
    assert SmartPlanner._parse_complexity("SOLO") == TaskComplexity.solo


def test_parse_complexity_trio():
    assert SmartPlanner._parse_complexity("TRIO") == TaskComplexity.trio


def test_parse_complexity_squad():
    assert SmartPlanner._parse_complexity("SQUAD") == TaskComplexity.squad


def test_parse_complexity_case_insensitive():
    assert SmartPlanner._parse_complexity("solo") == TaskComplexity.solo
    assert SmartPlanner._parse_complexity("  TRIO  ") == TaskComplexity.trio


def test_parse_complexity_embedded_in_text():
    assert SmartPlanner._parse_complexity("The answer is TRIO for this task") == TaskComplexity.trio


def test_parse_complexity_defaults_solo_on_garbage():
    assert SmartPlanner._parse_complexity("unknown stuff") == TaskComplexity.solo


def test_parse_complexity_empty_defaults_solo():
    assert SmartPlanner._parse_complexity("") == TaskComplexity.solo


# --- _strip_ceremony ---


def test_strip_ceremony_removes_commit_subtask():
    subtasks = [
        Subtask.create("Implement the feature"),
        Subtask.create("Commit all changes"),
    ]
    result = SmartPlanner._strip_ceremony(subtasks)
    assert len(result) == 1
    assert result[0].description == "Implement the feature"


def test_strip_ceremony_removes_various_patterns():
    ceremony_descriptions = [
        "Run tests",
        "Commit all changes",
        "Format code",
        "Run linter",
        "Push changes",
        "Create pr for review",
    ]
    subtasks = [Subtask.create(d) for d in ceremony_descriptions]
    result = SmartPlanner._strip_ceremony(subtasks)
    assert result == []


def test_strip_ceremony_keeps_real_work():
    subtasks = [
        Subtask.create("Add user authentication endpoint"),
        Subtask.create("Create database migration for users table"),
    ]
    result = SmartPlanner._strip_ceremony(subtasks)
    assert len(result) == 2


def test_strip_ceremony_keeps_subtasks_with_test_in_middle():
    """Subtasks mentioning 'test' mid-description should be kept."""
    subtasks = [
        Subtask.create("Add integration test infrastructure"),
        Subtask.create("Implement the authentication module"),
    ]
    result = SmartPlanner._strip_ceremony(subtasks)
    assert len(result) == 2


# --- Classification ---


@pytest.mark.asyncio
async def test_classify_solo():
    """SOLO classification should produce single subtask without decomposition."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="SOLO")
    smart = SmartPlanner(planner)

    subtasks, complexity = await smart.decompose("Add __version__ to __init__.py")

    assert complexity == TaskComplexity.solo
    assert len(subtasks) == 1
    assert subtasks[0].description == "Add __version__ to __init__.py"
    planner.decompose.assert_not_awaited()


@pytest.mark.asyncio
async def test_classify_trio_calls_decompose():
    """TRIO classification should call inner planner."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(
        return_value=[
            Subtask.create("Add model"),
            Subtask.create("Add route"),
        ]
    )
    smart = SmartPlanner(planner)

    subtasks, complexity = await smart.decompose("Add user endpoint")

    assert complexity == TaskComplexity.trio
    assert len(subtasks) == 2
    planner.decompose.assert_awaited_once()


@pytest.mark.asyncio
async def test_classify_squad():
    """SQUAD classification allows up to 8 subtasks."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="SQUAD")
    planner.decompose = AsyncMock(return_value=[Subtask.create(f"Task {i}") for i in range(6)])
    smart = SmartPlanner(planner)

    subtasks, complexity = await smart.decompose("Refactor auth system")

    assert complexity == TaskComplexity.squad
    assert len(subtasks) == 6


@pytest.mark.asyncio
async def test_classify_unknown_defaults_to_solo():
    """If classifier returns garbage, default to SOLO."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="I think this needs 5 subtasks...")
    smart = SmartPlanner(planner)

    subtasks, complexity = await smart.decompose("some task")

    assert complexity == TaskComplexity.solo
    assert len(subtasks) == 1


@pytest.mark.asyncio
async def test_classify_emits_generation_trace():
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner._tracer = MagicMock()
    planner._tracer.generation.return_value = MagicMock()
    planner._tracer.get_prompt.return_value = None
    planner.decompose = AsyncMock(return_value=[Subtask.create("Add model")])
    smart = SmartPlanner(planner)

    subtasks, complexity = await smart.decompose("Add user endpoint")

    assert complexity == TaskComplexity.trio
    assert len(subtasks) == 1
    planner._tracer.generation.assert_called_once()
    planner._tracer.end_span.assert_called_once()


@pytest.mark.asyncio
async def test_classify_traces_generation_error():
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(side_effect=Exception("classifier crashed"))
    planner._tracer = MagicMock()
    planner._tracer.generation.return_value = MagicMock()
    planner._tracer.get_prompt.return_value = None
    smart = SmartPlanner(planner)

    subtasks, complexity = await smart.decompose("some task")

    assert complexity == TaskComplexity.solo
    assert len(subtasks) == 1
    planner._tracer.end_span.assert_called_once()
    assert planner._tracer.end_span.call_args.kwargs["level"] == "ERROR"


@pytest.mark.asyncio
async def test_classify_uses_langfuse_prompt_when_available():
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner._tracer = MagicMock()
    planner._tracer.generation.return_value = MagicMock()
    prompt_client = MagicMock()
    prompt_client.compile.return_value = "Managed classify prompt"
    prompt_client.version = 5
    prompt_client.labels = ["production"]
    planner._tracer.get_prompt.return_value = prompt_client
    planner.decompose = AsyncMock(return_value=[Subtask.create("Add model")])
    smart = SmartPlanner(planner)

    await smart.decompose("Add user endpoint")

    prompt_client.compile.assert_called_once()
    assert planner._tracer.generation.call_args.kwargs["prompt"] is prompt_client


# --- Ceremony stripping in decompose ---


@pytest.mark.asyncio
async def test_strip_ceremony_in_decompose():
    """Ceremony subtasks should be stripped during decompose."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(
        return_value=[
            Subtask.create("Implement feature X"),
            Subtask.create("Write tests for feature X"),
            Subtask.create("Commit all changes"),
        ]
    )
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("Add feature X")

    descriptions = [s.description for s in subtasks]
    assert "Commit all changes" not in descriptions
    assert "Implement feature X" in descriptions


# --- Subtask caps ---


@pytest.mark.asyncio
async def test_trio_caps_at_3_subtasks():
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(return_value=[Subtask.create(f"Step {i}") for i in range(7)])
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("some task")

    assert len(subtasks) <= 3


@pytest.mark.asyncio
async def test_squad_caps_at_8_subtasks():
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="SQUAD")
    planner.decompose = AsyncMock(return_value=[Subtask.create(f"Step {i}") for i in range(12)])
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("big refactor")

    assert len(subtasks) <= 8


# --- Fallbacks ---


@pytest.mark.asyncio
async def test_fallback_when_decompose_returns_empty():
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(return_value=[])
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("some task")

    assert len(subtasks) == 1
    assert subtasks[0].description == "some task"


@pytest.mark.asyncio
async def test_fallback_when_all_ceremony():
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(
        return_value=[
            Subtask.create("Run tests"),
            Subtask.create("Commit changes"),
            Subtask.create("Format code"),
        ]
    )
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("some task")

    assert len(subtasks) == 1
    assert subtasks[0].description == "some task"


@pytest.mark.asyncio
async def test_fallback_when_classify_fails():
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(side_effect=Exception("CLI crashed"))
    smart = SmartPlanner(planner)

    subtasks, complexity = await smart.decompose("some task")

    assert complexity == TaskComplexity.solo
    assert len(subtasks) == 1


@pytest.mark.asyncio
async def test_fallback_when_decompose_fails():
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(side_effect=Exception("LLM down"))
    smart = SmartPlanner(planner)

    subtasks, complexity = await smart.decompose("some task")

    assert complexity == TaskComplexity.solo
    assert len(subtasks) == 1
    assert subtasks[0].description == "some task"


# --- Lessons injection ---


@pytest.mark.asyncio
async def test_lessons_injected():
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="SOLO")

    lesson_store = make_mock_lesson_store()
    lesson_store.get_lessons_for_task = MagicMock(
        return_value=[
            Lesson(
                id="l1",
                run_id="r1",
                category="planner",
                pattern="over_decomposed",
                content="Similar task was over-decomposed last time",
            ),
        ]
    )
    smart = SmartPlanner(planner, lesson_store=lesson_store)

    await smart.decompose("Add version string")

    lesson_store.get_lessons_for_task.assert_called_once()


# --- Classify prompt bias ---


def test_classify_prompt_contains_solo_bias():
    """Classify prompt should instruct to default to SOLO."""
    from horse_fish.planner.smart import _CLASSIFY_PROMPT

    assert "Default to SOLO" in _CLASSIFY_PROMPT
    assert "Most tasks are SOLO" in _CLASSIFY_PROMPT
    assert "When in doubt, choose SOLO" in _CLASSIFY_PROMPT
