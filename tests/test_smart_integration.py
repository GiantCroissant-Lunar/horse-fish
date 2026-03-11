"""Integration test — SmartPlanner + LessonStore round-trip."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from horse_fish.memory.lessons import LessonStore
from horse_fish.models import Subtask, SubtaskResult, SubtaskState, Task, TaskComplexity, TaskState
from horse_fish.planner.decompose import Planner
from horse_fish.planner.smart import SmartPlanner
from horse_fish.store.db import Store


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "test.db")
    db.migrate()
    return db


@pytest.fixture
def lesson_store(store):
    return LessonStore(store)


@pytest.fixture
def planner():
    return Planner(runtime="claude", model="test")


@pytest.fixture
def smart_planner(planner, lesson_store):
    return SmartPlanner(planner, lesson_store=lesson_store)


# --- Round-trip: run → extract lessons → feed back into planning ---


@pytest.mark.asyncio
async def test_lesson_round_trip(smart_planner, lesson_store, store):
    """Lessons from a completed run feed back into future planning."""
    # 1. Simulate a completed over-decomposed run
    run = Task.create("Add version string to __init__.py")
    run.state = TaskState.completed
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, ?)",
        (run.id, run.task, run.state.value, run.created_at.isoformat()),
    )
    run.subtasks = [
        Subtask(
            id="s1",
            description="Add version constant",
            state=SubtaskState.done,
            result=SubtaskResult(
                subtask_id="s1",
                success=True,
                output="ok",
                diff="diff --git a/src/__init__.py",
                duration_seconds=10,
            ),
        ),
        Subtask(
            id="s2",
            description="Write test for version",
            state=SubtaskState.done,
            result=SubtaskResult(
                subtask_id="s2",
                success=True,
                output="ok",
                diff="diff --git a/src/__init__.py",
                duration_seconds=5,
            ),
        ),
        Subtask(
            id="s3",
            description="Commit changes",
            state=SubtaskState.done,
            result=SubtaskResult(
                subtask_id="s3",
                success=True,
                output="ok",
                diff="",
                duration_seconds=2,
            ),
        ),
    ]

    # 2. Extract and store lessons
    lessons = lesson_store.extract_lessons(run)
    assert len(lessons) >= 1

    for lesson in lessons:
        lesson_store.store_lesson(lesson)

    # 3. Verify lessons are persisted
    stored = lesson_store.get_lessons_for_task("Add version string")
    assert len(stored) >= 1
    assert any(les.pattern == "over_decomposed" for les in stored)

    # 4. SmartPlanner picks up lessons in _get_lessons
    text = smart_planner._get_lessons("Add version string to init")
    assert "over_decomposed" in text


@pytest.mark.asyncio
async def test_solo_skips_decomposition(smart_planner):
    """SOLO classification skips the planner entirely."""
    with patch.object(smart_planner._planner, "_run_cli", new_callable=AsyncMock, return_value="SOLO"):
        with patch.object(smart_planner._planner, "decompose", new_callable=AsyncMock) as mock_decompose:
            subtasks, complexity = await smart_planner.decompose("Fix typo in README")

    assert complexity == TaskComplexity.solo
    assert len(subtasks) == 1
    assert subtasks[0].description == "Fix typo in README"
    mock_decompose.assert_not_called()


@pytest.mark.asyncio
async def test_ceremony_stripped_in_decompose(smart_planner):
    """Ceremony subtasks are stripped during decomposition."""
    mock_subtasks = [
        Subtask.create("Implement the feature"),
        Subtask.create("Run tests"),
        Subtask.create("Commit all"),
    ]

    with patch.object(smart_planner._planner, "_run_cli", new_callable=AsyncMock, return_value="TRIO"):
        with patch.object(smart_planner._planner, "decompose", new_callable=AsyncMock, return_value=mock_subtasks):
            subtasks, complexity = await smart_planner.decompose("Build a feature")

    assert complexity == TaskComplexity.trio
    descriptions = [s.description for s in subtasks]
    assert "Implement the feature" in descriptions
    assert "Run tests" not in descriptions
    assert "Commit all" not in descriptions


@pytest.mark.asyncio
async def test_orchestrator_wiring(store, lesson_store):
    """Orchestrator creates SmartPlanner when lesson_store is provided."""
    from horse_fish.orchestrator.engine import Orchestrator

    planner = Planner(runtime="claude", model="test")
    pool = AsyncMock()
    gates = AsyncMock()

    orch = Orchestrator(
        pool=pool,
        planner=planner,
        gates=gates,
        runtime="claude",
        model="test",
        max_agents=3,
        lesson_store=lesson_store,
    )

    assert orch._smart_planner is not None
    assert orch._lesson_store is lesson_store
