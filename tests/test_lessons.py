"""Tests for LessonStore — pattern extraction and querying."""

from __future__ import annotations

import pytest

from horse_fish.memory.lessons import Lesson, LessonStore
from horse_fish.models import Subtask, SubtaskResult, SubtaskState, Task, TaskState
from horse_fish.store.db import Store


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "test.db")
    db.migrate()
    return db


@pytest.fixture
def lesson_store(store):
    return LessonStore(store)


# --- Lesson model ---


def test_lesson_creation():
    lesson = Lesson(
        id="l1",
        run_id="r1",
        category="planner",
        pattern="over_decomposed",
        content="Task split into 3 but touched 1 file",
        task_signature="add version string",
    )
    assert lesson.category == "planner"
    assert lesson.pattern == "over_decomposed"


# --- Over-decomposition detection ---


def test_extract_over_decomposition(lesson_store):
    """Detects when subtask count >> files touched."""
    run = Task.create("Add version string to __init__.py")
    run.state = TaskState.completed
    run.subtasks = [
        Subtask(
            id="s1",
            description="Add version",
            state=SubtaskState.done,
            result=SubtaskResult(
                subtask_id="s1", success=True, output="ok", diff="diff --git a/src/__init__.py", duration_seconds=10
            ),
        ),
        Subtask(
            id="s2",
            description="Write test for version",
            state=SubtaskState.done,
            result=SubtaskResult(
                subtask_id="s2", success=True, output="ok", diff="diff --git a/src/__init__.py", duration_seconds=5
            ),
        ),
        Subtask(
            id="s3",
            description="Commit changes",
            state=SubtaskState.done,
            result=SubtaskResult(subtask_id="s3", success=True, output="ok", diff="", duration_seconds=2),
        ),
    ]

    lessons = lesson_store.extract_lessons(run)
    planner_lessons = [
        lesson for lesson in lessons if lesson.category == "planner" and lesson.pattern == "over_decomposed"
    ]
    assert len(planner_lessons) >= 1
    assert "3 subtasks" in planner_lessons[0].content


def test_no_over_decomposition_for_single_subtask(lesson_store):
    """Single subtask run should not flag over-decomposition."""
    run = Task.create("Simple task")
    run.state = TaskState.completed
    run.subtasks = [
        Subtask(
            id="s1",
            description="Do it",
            state=SubtaskState.done,
            result=SubtaskResult(
                subtask_id="s1", success=True, output="ok", diff="diff --git a/src/foo.py", duration_seconds=10
            ),
        ),
    ]

    lessons = lesson_store.extract_lessons(run)
    planner_lessons = [lesson for lesson in lessons if lesson.pattern == "over_decomposed"]
    assert len(planner_lessons) == 0


# --- Stall detection ---


def test_extract_stall_lesson(lesson_store):
    """Detects subtasks that were retried due to stalls."""
    run = Task.create("Some task")
    run.state = TaskState.completed
    run.subtasks = [
        Subtask(
            id="s1",
            description="Build feature",
            state=SubtaskState.done,
            retry_count=2,
            result=SubtaskResult(
                subtask_id="s1", success=True, output="ok", diff="some diff", duration_seconds=120, agent_runtime="pi"
            ),
        ),
    ]

    lessons = lesson_store.extract_lessons(run)
    stall_lessons = [lesson for lesson in lessons if lesson.pattern == "agent_stalled"]
    assert len(stall_lessons) >= 1
    assert "pi" in stall_lessons[0].content


# --- No-diff detection ---


def test_extract_no_diff_lesson(lesson_store):
    """Detects when agent reports success but produces no diff."""
    run = Task.create("Some task")
    run.state = TaskState.completed
    run.subtasks = [
        Subtask(
            id="s1",
            description="Build feature",
            state=SubtaskState.done,
            result=SubtaskResult(
                subtask_id="s1", success=True, output="looks good", diff="", duration_seconds=30, agent_runtime="pi"
            ),
        ),
    ]

    lessons = lesson_store.extract_lessons(run)
    no_diff = [lesson for lesson in lessons if lesson.pattern == "no_diff"]
    assert len(no_diff) >= 1


# --- Store and retrieve ---


def test_store_lesson(lesson_store, store):
    """Lessons should be persisted to SQLite."""
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, ?)",
        ("r1", "test", "completed", "2026-03-08T00:00:00"),
    )
    lesson = Lesson(
        id="l1",
        run_id="r1",
        category="planner",
        pattern="over_decomposed",
        content="Over-decomposed",
        task_signature="add version",
    )
    lesson_store.store_lesson(lesson)

    row = store.fetchone("SELECT * FROM lessons WHERE id = ?", ("l1",))
    assert row is not None
    assert row["pattern"] == "over_decomposed"


def test_get_lessons_for_task(lesson_store, store):
    """Should retrieve lessons relevant to a task."""
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, ?)",
        ("r1", "test1", "completed", "2026-03-08T00:00:00"),
    )
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, ?)",
        ("r2", "test2", "completed", "2026-03-08T00:00:00"),
    )
    lesson_store.store_lesson(
        Lesson(
            id="l1",
            run_id="r1",
            category="planner",
            pattern="over_decomposed",
            content="Task 'add version' was over-decomposed",
            task_signature="add version",
        )
    )
    lesson_store.store_lesson(
        Lesson(
            id="l2",
            run_id="r2",
            category="dispatch",
            pattern="agent_stalled",
            content="Pi stalled on test task",
            task_signature="write tests",
        )
    )

    # Query for planner lessons
    lessons = lesson_store.get_lessons_for_task("add version string", category="planner")
    assert len(lessons) >= 1
    assert lessons[0].pattern == "over_decomposed"


def test_get_lessons_empty(lesson_store):
    """Should return empty list when no lessons exist."""
    lessons = lesson_store.get_lessons_for_task("anything")
    assert lessons == []
