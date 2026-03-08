# Smart Planner + Learning System Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace the over-decomposing planner with a complexity-aware SmartPlanner, and add a LessonStore that feeds patterns back into future planning.

**Architecture:** SmartPlanner wraps the existing Planner. It classifies task complexity (SOLO/TRIO/SQUAD), skips decomposition for simple tasks, strips ceremony subtasks, and injects lessons from past runs. LessonStore is a new SQLite-backed module that extracts patterns (over-decomposition, stalls, no-diff) from completed runs.

**Tech Stack:** Python 3.12+, Pydantic, SQLite, pytest, pytest-asyncio

**Design doc:** `docs/plans/2026-03-08-smart-planner-learning-design.md`

---

### Task 1: Add TaskComplexity enum and Run model enhancements

**Files:**
- Modify: `src/horse_fish/models.py`
- Test: `tests/test_models.py`

**Step 1: Write the failing test**

Add to `tests/test_models.py`:

```python
from horse_fish.models import TaskComplexity


def test_task_complexity_values():
    assert TaskComplexity.solo == "SOLO"
    assert TaskComplexity.trio == "TRIO"
    assert TaskComplexity.squad == "SQUAD"


def test_run_has_complexity_field():
    run = Run.create("test task")
    assert run.complexity is None


def test_run_has_lessons_field():
    run = Run.create("test task")
    assert run.lessons == []


def test_run_complexity_can_be_set():
    run = Run.create("test task")
    run.complexity = TaskComplexity.solo
    assert run.complexity == TaskComplexity.solo
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py::test_task_complexity_values -v`
Expected: FAIL with `ImportError: cannot import name 'TaskComplexity'`

**Step 3: Write minimal implementation**

In `src/horse_fish/models.py`, add after `RunState`:

```python
class TaskComplexity(StrEnum):
    solo = "SOLO"
    trio = "TRIO"
    squad = "SQUAD"
```

Add fields to `Run`:

```python
class Run(BaseModel):
    id: str
    task: str
    state: RunState = RunState.planning
    complexity: TaskComplexity | None = None
    subtasks: list[Subtask] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @classmethod
    def create(cls, task: str) -> Run:
        return cls(id=str(uuid.uuid4()), task=task)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/horse_fish/models.py tests/test_models.py
git commit -m "feat: add TaskComplexity enum and Run.complexity/lessons fields"
```

---

### Task 2: Add lessons table to SQLite schema

**Files:**
- Modify: `src/horse_fish/store/db.py`
- Test: `tests/test_store.py`

**Step 1: Write the failing test**

Add to `tests/test_store.py`:

```python
def test_lessons_table_exists(store):
    """Lessons table should exist after migration."""
    result = store.fetchone(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='lessons'"
    )
    assert result is not None


def test_lessons_insert_and_query(store):
    """Should be able to insert and query lessons."""
    store.execute(
        "INSERT INTO lessons (id, run_id, category, pattern, content, task_signature, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("lesson-1", "run-1", "planner", "over_decomposed",
         "Task was split into 3 subtasks but only touched 1 file", "add version", "2026-03-08T00:00:00"),
    )
    row = store.fetchone("SELECT * FROM lessons WHERE id = ?", ("lesson-1",))
    assert row is not None
    assert row["category"] == "planner"
    assert row["pattern"] == "over_decomposed"


def test_lessons_query_by_category(store):
    """Should query lessons by category."""
    store.execute(
        "INSERT INTO lessons (id, run_id, category, pattern, content, task_signature, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("lesson-1", "run-1", "planner", "over_decomposed", "content", "sig", "2026-03-08T00:00:00"),
    )
    store.execute(
        "INSERT INTO lessons (id, run_id, category, pattern, content, task_signature, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("lesson-2", "run-1", "dispatch", "agent_stalled", "content", "sig", "2026-03-08T00:00:00"),
    )
    rows = store.fetchall("SELECT * FROM lessons WHERE category = ?", ("planner",))
    assert len(rows) == 1
    assert rows[0]["id"] == "lesson-1"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_store.py::test_lessons_table_exists -v`
Expected: FAIL with `AssertionError: assert None is not None`

**Step 3: Write minimal implementation**

In `src/horse_fish/store/db.py`, add migration 2 to the `MIGRATIONS` list:

```python
(
    2,
    """
    CREATE TABLE IF NOT EXISTS lessons (
        id TEXT PRIMARY KEY,
        run_id TEXT REFERENCES runs(id),
        category TEXT NOT NULL,
        pattern TEXT NOT NULL,
        content TEXT NOT NULL,
        task_signature TEXT,
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_lessons_category ON lessons(category);
    CREATE INDEX IF NOT EXISTS idx_lessons_pattern ON lessons(pattern);
    """,
),
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_store.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/horse_fish/store/db.py tests/test_store.py
git commit -m "feat: add lessons table to SQLite schema (migration 2)"
```

---

### Task 3: Create LessonStore with lesson extraction

**Files:**
- Create: `src/horse_fish/memory/lessons.py`
- Test: `tests/test_lessons.py`

**Step 1: Write the failing test**

Create `tests/test_lessons.py`:

```python
"""Tests for LessonStore — pattern extraction and querying."""

from __future__ import annotations

import pytest

from horse_fish.memory.lessons import Lesson, LessonStore
from horse_fish.models import Run, RunState, Subtask, SubtaskResult, SubtaskState
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
    run = Run.create("Add version string to __init__.py")
    run.state = RunState.completed
    run.subtasks = [
        Subtask(id="s1", description="Add version", state=SubtaskState.done,
                result=SubtaskResult(subtask_id="s1", success=True, output="ok",
                                     diff="diff --git a/src/__init__.py", duration_seconds=10)),
        Subtask(id="s2", description="Write test for version", state=SubtaskState.done,
                result=SubtaskResult(subtask_id="s2", success=True, output="ok",
                                     diff="diff --git a/src/__init__.py", duration_seconds=5)),
        Subtask(id="s3", description="Commit changes", state=SubtaskState.done,
                result=SubtaskResult(subtask_id="s3", success=True, output="ok",
                                     diff="", duration_seconds=2)),
    ]

    lessons = lesson_store.extract_lessons(run)
    planner_lessons = [l for l in lessons if l.category == "planner" and l.pattern == "over_decomposed"]
    assert len(planner_lessons) >= 1
    assert "3 subtasks" in planner_lessons[0].content


def test_no_over_decomposition_for_single_subtask(lesson_store):
    """Single subtask run should not flag over-decomposition."""
    run = Run.create("Simple task")
    run.state = RunState.completed
    run.subtasks = [
        Subtask(id="s1", description="Do it", state=SubtaskState.done,
                result=SubtaskResult(subtask_id="s1", success=True, output="ok",
                                     diff="diff --git a/src/foo.py", duration_seconds=10)),
    ]

    lessons = lesson_store.extract_lessons(run)
    planner_lessons = [l for l in lessons if l.pattern == "over_decomposed"]
    assert len(planner_lessons) == 0


# --- Stall detection ---

def test_extract_stall_lesson(lesson_store):
    """Detects subtasks that were retried due to stalls."""
    run = Run.create("Some task")
    run.state = RunState.completed
    run.subtasks = [
        Subtask(id="s1", description="Build feature", state=SubtaskState.done,
                retry_count=2,
                result=SubtaskResult(subtask_id="s1", success=True, output="ok",
                                     diff="some diff", duration_seconds=120,
                                     agent_runtime="pi")),
    ]

    lessons = lesson_store.extract_lessons(run)
    stall_lessons = [l for l in lessons if l.pattern == "agent_stalled"]
    assert len(stall_lessons) >= 1
    assert "pi" in stall_lessons[0].content


# --- No-diff detection ---

def test_extract_no_diff_lesson(lesson_store):
    """Detects when agent reports success but produces no diff."""
    run = Run.create("Some task")
    run.state = RunState.completed
    run.subtasks = [
        Subtask(id="s1", description="Build feature", state=SubtaskState.done,
                result=SubtaskResult(subtask_id="s1", success=True, output="looks good",
                                     diff="", duration_seconds=30,
                                     agent_runtime="pi")),
    ]

    lessons = lesson_store.extract_lessons(run)
    no_diff = [l for l in lessons if l.pattern == "no_diff"]
    assert len(no_diff) >= 1


# --- Store and retrieve ---

def test_store_lesson(lesson_store, store):
    """Lessons should be persisted to SQLite."""
    lesson = Lesson(
        id="l1", run_id="r1", category="planner", pattern="over_decomposed",
        content="Over-decomposed", task_signature="add version",
    )
    lesson_store.store_lesson(lesson)

    row = store.fetchone("SELECT * FROM lessons WHERE id = ?", ("l1",))
    assert row is not None
    assert row["pattern"] == "over_decomposed"


def test_get_lessons_for_task(lesson_store):
    """Should retrieve lessons relevant to a task."""
    lesson_store.store_lesson(Lesson(
        id="l1", run_id="r1", category="planner", pattern="over_decomposed",
        content="Task 'add version' was over-decomposed", task_signature="add version",
    ))
    lesson_store.store_lesson(Lesson(
        id="l2", run_id="r2", category="dispatch", pattern="agent_stalled",
        content="Pi stalled on test task", task_signature="write tests",
    ))

    # Query for planner lessons
    lessons = lesson_store.get_lessons_for_task("add version string", category="planner")
    assert len(lessons) >= 1
    assert lessons[0].pattern == "over_decomposed"


def test_get_lessons_empty(lesson_store):
    """Should return empty list when no lessons exist."""
    lessons = lesson_store.get_lessons_for_task("anything")
    assert lessons == []
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_lessons.py::test_lesson_creation -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'horse_fish.memory.lessons'`

**Step 3: Write minimal implementation**

Create `src/horse_fish/memory/lessons.py`:

```python
"""Lesson store — structured pattern extraction from completed runs."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from horse_fish.models import Run, SubtaskState
from horse_fish.store.db import Store


class Lesson(BaseModel):
    """A structured lesson learned from a completed run."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    run_id: str = ""
    category: str = ""  # planner, dispatch, merge, agent
    pattern: str = ""  # over_decomposed, agent_stalled, no_diff, merge_conflict
    content: str = ""
    task_signature: str = ""
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


_DIFF_FILE_RE = re.compile(r"diff --git a/(\S+)")


class LessonStore:
    """Extracts and stores structured lessons from completed runs."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def extract_lessons(self, run: Run) -> list[Lesson]:
        """Analyze a completed run and extract structured lessons."""
        lessons: list[Lesson] = []

        # Over-decomposition: many subtasks, few files touched
        if len(run.subtasks) > 1:
            files_touched: set[str] = set()
            for s in run.subtasks:
                if s.result and s.result.diff:
                    files_touched.update(_DIFF_FILE_RE.findall(s.result.diff))
            if len(files_touched) <= 2 and len(run.subtasks) > 2:
                lessons.append(Lesson(
                    run_id=run.id,
                    category="planner",
                    pattern="over_decomposed",
                    content=(
                        f"Task '{run.task}' was split into {len(run.subtasks)} subtasks "
                        f"but only touched {len(files_touched)} file(s). Should be SOLO."
                    ),
                    task_signature=self._normalize(run.task),
                ))

        # Stall detection: subtasks that were retried
        for s in run.subtasks:
            if s.retry_count > 0 and s.result:
                runtime = s.result.agent_runtime or "unknown"
                lessons.append(Lesson(
                    run_id=run.id,
                    category="dispatch",
                    pattern="agent_stalled",
                    content=(
                        f"Subtask '{s.description}' stalled {s.retry_count} time(s) "
                        f"with {runtime}."
                    ),
                    task_signature=self._normalize(s.description),
                ))

        # No-diff: agent reported success but produced no file changes
        for s in run.subtasks:
            if s.result and s.result.success and not s.result.diff:
                runtime = s.result.agent_runtime or "unknown"
                lessons.append(Lesson(
                    run_id=run.id,
                    category="agent",
                    pattern="no_diff",
                    content=(
                        f"Agent {runtime} produced no diff for '{s.description}'."
                    ),
                    task_signature=self._normalize(s.description),
                ))

        return lessons

    def store_lesson(self, lesson: Lesson) -> None:
        """Persist a lesson to SQLite."""
        self._store.execute(
            "INSERT OR IGNORE INTO lessons (id, run_id, category, pattern, content, task_signature, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (lesson.id, lesson.run_id, lesson.category, lesson.pattern,
             lesson.content, lesson.task_signature, lesson.created_at),
        )

    def get_lessons_for_task(
        self, task: str, category: str | None = None, limit: int = 5
    ) -> list[Lesson]:
        """Retrieve lessons relevant to a task description."""
        sig = self._normalize(task)
        if category:
            rows = self._store.fetchall(
                "SELECT * FROM lessons WHERE category = ? ORDER BY created_at DESC LIMIT ?",
                (category, limit),
            )
        else:
            rows = self._store.fetchall(
                "SELECT * FROM lessons ORDER BY created_at DESC LIMIT ?",
                (limit,),
            )

        lessons = []
        for row in rows:
            # Simple relevance: check if task_signature words overlap
            row_sig = row.get("task_signature", "")
            if not sig or not row_sig or self._overlap(sig, row_sig):
                lessons.append(Lesson(
                    id=row["id"],
                    run_id=row["run_id"],
                    category=row["category"],
                    pattern=row["pattern"],
                    content=row["content"],
                    task_signature=row_sig,
                    created_at=row["created_at"],
                ))
        return lessons[:limit]

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize task description for matching."""
        return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()

    @staticmethod
    def _overlap(sig_a: str, sig_b: str) -> bool:
        """Check if two normalized signatures share meaningful words."""
        stop = {"a", "an", "the", "to", "for", "in", "on", "of", "and", "or", "is", "it"}
        words_a = {w for w in sig_a.split() if w not in stop and len(w) > 2}
        words_b = {w for w in sig_b.split() if w not in stop and len(w) > 2}
        if not words_a or not words_b:
            return True  # can't filter, include it
        return bool(words_a & words_b)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_lessons.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/horse_fish/memory/lessons.py tests/test_lessons.py
git commit -m "feat: add LessonStore with pattern extraction (over-decomp, stalls, no-diff)"
```

---

### Task 4: Create SmartPlanner with complexity classification

**Files:**
- Create: `src/horse_fish/planner/smart.py`
- Test: `tests/test_smart_planner.py`

**Step 1: Write the failing test**

Create `tests/test_smart_planner.py`:

```python
"""Tests for SmartPlanner — complexity classification and ceremony stripping."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horse_fish.memory.lessons import LessonStore
from horse_fish.models import Subtask, TaskComplexity
from horse_fish.planner.smart import SmartPlanner


# --- Helpers ---

def make_mock_planner():
    planner = MagicMock()
    planner.decompose = AsyncMock()
    planner._build_command = MagicMock(return_value=["claude", "--print", "-m", "model", "prompt"])
    planner._run_cli = AsyncMock()
    planner.runtime = "claude"
    planner.model = "claude-sonnet-4-6"
    return planner


def make_mock_lesson_store():
    store = MagicMock(spec=LessonStore)
    store.get_lessons_for_task = MagicMock(return_value=[])
    return store


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
    planner.decompose.assert_not_awaited()  # should NOT call inner decompose


@pytest.mark.asyncio
async def test_classify_trio_calls_decompose():
    """TRIO classification should call inner planner with max_subtasks=3."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(side_effect=[
        "TRIO",  # classification
        json.dumps([  # decomposition (called via _planner.decompose)
            {"description": "Add model", "deps": [], "files_hint": ["src/models.py"]},
            {"description": "Add route", "deps": ["Add model"], "files_hint": ["src/routes.py"]},
        ]),
    ])
    planner.decompose = AsyncMock(return_value=[
        Subtask.create("Add model"),
        Subtask.create("Add route"),
    ])
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
    planner.decompose = AsyncMock(return_value=[
        Subtask.create(f"Task {i}") for i in range(6)
    ])
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


# --- Ceremony stripping ---

@pytest.mark.asyncio
async def test_strip_ceremony_commit_subtask():
    """Subtasks that only commit/test/review should be stripped."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(return_value=[
        Subtask.create("Implement feature X"),
        Subtask.create("Write tests for feature X"),
        Subtask.create("Commit all changes"),
    ])
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("Add feature X")

    descriptions = [s.description for s in subtasks]
    assert "Commit all changes" not in descriptions
    assert "Implement feature X" in descriptions


@pytest.mark.asyncio
async def test_strip_ceremony_preserves_real_subtasks():
    """Non-ceremony subtasks should be preserved."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(return_value=[
        Subtask.create("Add database migration"),
        Subtask.create("Update API handler"),
    ])
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("Add user field")

    assert len(subtasks) == 2


# --- Subtask cap ---

@pytest.mark.asyncio
async def test_trio_caps_at_3_subtasks():
    """TRIO should cap subtask count to 3."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(return_value=[
        Subtask.create(f"Step {i}") for i in range(7)
    ])
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("some task")

    assert len(subtasks) <= 3


@pytest.mark.asyncio
async def test_squad_caps_at_8_subtasks():
    """SQUAD should cap subtask count to 8."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="SQUAD")
    planner.decompose = AsyncMock(return_value=[
        Subtask.create(f"Step {i}") for i in range(12)
    ])
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("big refactor")

    assert len(subtasks) <= 8


# --- Fallback ---

@pytest.mark.asyncio
async def test_fallback_when_decompose_returns_empty():
    """If inner planner returns nothing, fallback to single subtask."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(return_value=[])
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("some task")

    assert len(subtasks) == 1
    assert subtasks[0].description == "some task"


@pytest.mark.asyncio
async def test_fallback_when_all_ceremony():
    """If all subtasks are ceremony, fallback to single subtask."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="TRIO")
    planner.decompose = AsyncMock(return_value=[
        Subtask.create("Run tests"),
        Subtask.create("Commit changes"),
        Subtask.create("Format code"),
    ])
    smart = SmartPlanner(planner)

    subtasks, _ = await smart.decompose("some task")

    assert len(subtasks) == 1
    assert subtasks[0].description == "some task"


@pytest.mark.asyncio
async def test_fallback_when_classify_fails():
    """If classification CLI fails, default to SOLO."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(side_effect=Exception("CLI crashed"))
    smart = SmartPlanner(planner)

    subtasks, complexity = await smart.decompose("some task")

    assert complexity == TaskComplexity.solo
    assert len(subtasks) == 1


# --- Lessons injection ---

@pytest.mark.asyncio
async def test_lessons_injected_into_context():
    """Past lessons should influence classification."""
    planner = make_mock_planner()
    planner._run_cli = AsyncMock(return_value="SOLO")

    from horse_fish.memory.lessons import Lesson
    lesson_store = make_mock_lesson_store()
    lesson_store.get_lessons_for_task = MagicMock(return_value=[
        Lesson(id="l1", run_id="r1", category="planner", pattern="over_decomposed",
               content="Similar task was over-decomposed last time"),
    ])
    smart = SmartPlanner(planner, lesson_store=lesson_store)

    await smart.decompose("Add version string")

    # Verify lesson store was consulted
    lesson_store.get_lessons_for_task.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_smart_planner.py::test_classify_solo -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'horse_fish.planner.smart'`

**Step 3: Write minimal implementation**

Create `src/horse_fish/planner/smart.py`:

```python
"""SmartPlanner — complexity-aware task decomposition."""

from __future__ import annotations

import logging
import re

from horse_fish.memory.lessons import LessonStore
from horse_fish.models import Subtask, TaskComplexity
from horse_fish.planner.decompose import Planner

logger = logging.getLogger(__name__)

_CLASSIFY_PROMPT = """\
Estimate the complexity of this coding task:
- SOLO: Single file or tightly coupled change. One agent handles everything.
- TRIO: 2-4 files across 1-2 components. Needs minimal decomposition.
- SQUAD: 5+ files, multiple components, needs parallel work.

{lessons}

Task: {task}
Context: {context}

Reply with ONLY one word: SOLO, TRIO, or SQUAD.
"""

_CEREMONY_PATTERNS = re.compile(
    r"^(commit|test|review|format|lint|verify|run tests|write tests for|"
    r"commit all|format code|run linter|push changes|create pr|open pull request)",
    re.IGNORECASE,
)

# Caps per complexity tier
_MAX_SUBTASKS = {
    TaskComplexity.solo: 1,
    TaskComplexity.trio: 3,
    TaskComplexity.squad: 8,
}


class SmartPlanner:
    """Wraps Planner with complexity classification and ceremony stripping."""

    def __init__(
        self,
        planner: Planner,
        lesson_store: LessonStore | None = None,
    ) -> None:
        self._planner = planner
        self._lessons = lesson_store

    async def decompose(self, task: str, context: str = "") -> tuple[list[Subtask], TaskComplexity]:
        """Classify task complexity, then decompose if needed.

        Returns:
            Tuple of (subtasks, complexity).
        """
        # 1. Query lessons
        lessons_text = self._get_lessons(task)

        # 2. Classify
        complexity = await self._classify(task, context, lessons_text)

        # 3. SOLO → single subtask, skip decomposition
        if complexity == TaskComplexity.solo:
            return [Subtask.create(task)], complexity

        # 4. Decompose
        try:
            subtasks = await self._planner.decompose(task, context)
        except Exception as exc:
            logger.warning("Decomposition failed, falling back to SOLO: %s", exc)
            return [Subtask.create(task)], TaskComplexity.solo

        # 5. Strip ceremony
        subtasks = self._strip_ceremony(subtasks)

        # 6. Cap
        cap = _MAX_SUBTASKS.get(complexity, 8)
        subtasks = subtasks[:cap]

        # 7. Fallback if empty
        if not subtasks:
            return [Subtask.create(task)], complexity

        return subtasks, complexity

    async def _classify(self, task: str, context: str, lessons: str) -> TaskComplexity:
        """Ask the LLM to classify task complexity."""
        prompt = _CLASSIFY_PROMPT.format(
            task=task,
            context=context or "No additional context.",
            lessons=f"Lessons from past runs:\n{lessons}" if lessons else "",
        )
        try:
            cmd = self._planner._build_command(prompt)
            raw = await self._planner._run_cli(cmd)
            return self._parse_complexity(raw.strip())
        except Exception as exc:
            logger.warning("Classification failed, defaulting to SOLO: %s", exc)
            return TaskComplexity.solo

    @staticmethod
    def _parse_complexity(raw: str) -> TaskComplexity:
        """Parse LLM response into TaskComplexity."""
        text = raw.strip().upper()
        for complexity in TaskComplexity:
            if complexity.value in text:
                return complexity
        # Default to SOLO if unparseable
        return TaskComplexity.solo

    @staticmethod
    def _strip_ceremony(subtasks: list[Subtask]) -> list[Subtask]:
        """Remove subtasks that are purely ceremony (commit, test, review, etc.)."""
        return [s for s in subtasks if not _CEREMONY_PATTERNS.match(s.description.strip())]

    def _get_lessons(self, task: str) -> str:
        """Retrieve relevant lessons for this task."""
        if not self._lessons:
            return ""
        try:
            lessons = self._lessons.get_lessons_for_task(task)
            if not lessons:
                return ""
            return "\n".join(f"- [{l.pattern}] {l.content}" for l in lessons)
        except Exception as exc:
            logger.warning("Failed to query lessons: %s", exc)
            return ""
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_smart_planner.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add src/horse_fish/planner/smart.py tests/test_smart_planner.py
git commit -m "feat: add SmartPlanner with complexity classification and ceremony stripping"
```

---

### Task 5: Update planner __init__.py exports

**Files:**
- Modify: `src/horse_fish/planner/__init__.py`
- Modify: `src/horse_fish/memory/__init__.py`

**Step 1: Update planner exports**

In `src/horse_fish/planner/__init__.py`:

```python
"""Planner module — LLM-driven task decomposition into subtask DAGs."""

from horse_fish.planner.decompose import Planner, PlannerError
from horse_fish.planner.smart import SmartPlanner

__all__ = ["Planner", "PlannerError", "SmartPlanner"]
```

**Step 2: Update memory exports**

In `src/horse_fish/memory/__init__.py`:

```python
"""Memory module for cross-session learning."""

from horse_fish.memory.lessons import Lesson, LessonStore
from horse_fish.memory.store import MemoryHit, MemoryStore

__all__ = ["MemoryStore", "MemoryHit", "LessonStore", "Lesson"]
```

**Step 3: Run existing tests to verify no breakage**

Run: `pytest tests/test_planner.py tests/test_memory.py -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add src/horse_fish/planner/__init__.py src/horse_fish/memory/__init__.py
git commit -m "feat: export SmartPlanner and LessonStore from package __init__"
```

---

### Task 6: Wire SmartPlanner and LessonStore into Orchestrator

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py`
- Modify: `tests/test_orchestrator.py`

**Step 1: Write the failing test**

Add to `tests/test_orchestrator.py`:

```python
from horse_fish.models import TaskComplexity
from horse_fish.memory.lessons import LessonStore


@pytest.fixture
def mock_lesson_store():
    store = MagicMock(spec=LessonStore)
    store.extract_lessons = MagicMock(return_value=[])
    store.store_lesson = MagicMock()
    store.get_lessons_for_task = MagicMock(return_value=[])
    return store


@pytest.mark.asyncio
async def test_plan_uses_smart_planner(mock_pool, mock_gates, mock_lesson_store):
    """Orchestrator should use SmartPlanner when lesson_store is provided."""
    mock_planner = AsyncMock()
    mock_planner.decompose = AsyncMock(return_value=[
        Subtask(id="s1", description="Do task"),
    ])
    mock_planner._build_command = MagicMock(return_value=["cmd"])
    mock_planner._run_cli = AsyncMock(return_value="SOLO")
    mock_planner.runtime = "claude"
    mock_planner.model = "test"

    orch = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        lesson_store=mock_lesson_store,
    )

    # Mock pool to end execution immediately
    mock_pool.check_status = AsyncMock(return_value=MagicMock(value="dead"))
    mock_pool.collect_result = AsyncMock(return_value=SubtaskResult(
        subtask_id="s1", success=True, output="ok", diff="some diff", duration_seconds=5,
    ))
    mock_pool.spawn = AsyncMock(return_value=AgentSlot(
        id="a1", name="test", runtime="claude", model="test", capability="builder",
    ))
    mock_pool._get_slot = MagicMock(return_value=AgentSlot(
        id="a1", name="test", runtime="claude", model="test", capability="builder",
        worktree_path="/tmp/wt", branch="test-branch",
    ))
    mock_pool.send_task = AsyncMock()
    mock_pool.list_agents = MagicMock(return_value=[])
    mock_gates.run_all = AsyncMock(return_value=[])
    mock_gates.all_passed = MagicMock(return_value=True)
    mock_pool._worktrees = AsyncMock()
    mock_pool._worktrees.merge = AsyncMock(return_value=True)

    run = await orch.run("Simple task")

    assert run.state == RunState.completed
    assert run.complexity == TaskComplexity.solo


@pytest.mark.asyncio
async def test_learn_extracts_lessons(mock_pool, mock_planner, mock_gates, mock_lesson_store):
    """_learn should extract and store lessons."""
    orch = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
        lesson_store=mock_lesson_store,
    )

    from horse_fish.memory.lessons import Lesson
    mock_lesson_store.extract_lessons = MagicMock(return_value=[
        Lesson(id="l1", category="planner", pattern="over_decomposed", content="test"),
    ])

    run = Run.create("test task")
    run.state = RunState.completed
    run.subtasks = [Subtask(id="s1", description="task", state=SubtaskState.done)]

    await orch._learn(run)

    mock_lesson_store.extract_lessons.assert_called_once_with(run)
    mock_lesson_store.store_lesson.assert_called_once()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_orchestrator.py::test_plan_uses_smart_planner -v`
Expected: FAIL with `TypeError: Orchestrator.__init__() got an unexpected keyword argument 'lesson_store'`

**Step 3: Write minimal implementation**

Modify `src/horse_fish/orchestrator/engine.py`:

1. Add import:
```python
from horse_fish.memory.lessons import LessonStore
from horse_fish.models import AgentState, Run, RunState, Subtask, SubtaskResult, SubtaskState, TaskComplexity
from horse_fish.planner.smart import SmartPlanner
```

2. Add `lesson_store` parameter to `__init__`:
```python
def __init__(
    self,
    pool: AgentPool,
    planner: Planner,
    gates: ValidationGates,
    runtime: str = "claude",
    model: str | None = None,
    max_agents: int = 3,
    selector: AgentSelector | None = None,
    merge_queue: MergeQueue | None = None,
    tracer: Tracer | None = None,
    memory: MemoryStore | None = None,
    lesson_store: LessonStore | None = None,
    stall_timeout_seconds: int = STALL_TIMEOUT_SECONDS,
    concurrency_limits: dict[RunState, int] | None = None,
) -> None:
    # ... existing assignments ...
    self._lesson_store = lesson_store
    self._smart_planner = SmartPlanner(planner, lesson_store=lesson_store) if lesson_store else None
```

3. Update `_plan`:
```python
async def _plan(self, run: Run) -> Run:
    """Decompose the task into subtasks via the Planner."""
    try:
        if self._smart_planner:
            subtasks, complexity = await self._smart_planner.decompose(run.task)
            run.complexity = complexity
        else:
            subtasks = await self._planner.decompose(run.task)
    except Exception as exc:
        logger.error("Planning failed: %s", exc)
        run.state = RunState.failed
        return run

    if not subtasks:
        logger.error("Planner returned no subtasks")
        run.state = RunState.failed
        return run

    subtasks = self._resolve_deps(subtasks)
    run.subtasks = subtasks
    run.state = RunState.executing
    return run
```

4. Update `_learn`:
```python
async def _learn(self, run: Run) -> None:
    """Store completed run results in memory for future learning."""
    if self._memory:
        subtask_results = [s.result for s in run.subtasks if s.result]
        try:
            await self._memory.store_run_result(run, subtask_results)
        except Exception as exc:
            logger.warning("Failed to store run in memory: %s", exc)

    if self._lesson_store:
        try:
            lessons = self._lesson_store.extract_lessons(run)
            for lesson in lessons:
                self._lesson_store.store_lesson(lesson)
            run.lessons = [l.id for l in lessons]
        except Exception as exc:
            logger.warning("Failed to extract lessons: %s", exc)
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_orchestrator.py -v`
Expected: All PASS

**Step 5: Run full test suite**

Run: `pytest -v`
Expected: All existing tests still PASS

**Step 6: Commit**

```bash
git add src/horse_fish/orchestrator/engine.py tests/test_orchestrator.py
git commit -m "feat: wire SmartPlanner and LessonStore into orchestrator"
```

---

### Task 7: Wire into CLI

**Files:**
- Modify: `src/horse_fish/cli.py`
- Test: `tests/test_cli.py`

**Step 1: Update _init_components**

Modify `src/horse_fish/cli.py` to create LessonStore and pass to Orchestrator:

```python
from horse_fish.memory.lessons import LessonStore

def _init_components(runtime: str, model: str | None, max_agents: int):
    """Initialize all components needed for orchestration."""
    repo_root = str(Path.cwd())
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    store = Store(DB_PATH)
    store.migrate()
    tmux = TmuxManager()
    worktrees = WorktreeManager(repo_root)
    claude_md = Path.cwd() / "CLAUDE.md"
    project_context = claude_md.read_text() if claude_md.exists() else None
    pool = AgentPool(store, tmux, worktrees, project_context=project_context)
    planner = Planner(runtime=runtime, model=model)
    gates = ValidationGates()
    memory = MemoryStore()
    lesson_store = LessonStore(store)
    orchestrator = Orchestrator(
        pool=pool,
        planner=planner,
        gates=gates,
        runtime=runtime,
        model=model or "",
        max_agents=max_agents,
        memory=memory,
        lesson_store=lesson_store,
    )
    return orchestrator, store, pool
```

**Step 2: Run CLI test to verify no breakage**

Run: `pytest tests/test_cli.py -v`
Expected: All PASS

**Step 3: Commit**

```bash
git add src/horse_fish/cli.py
git commit -m "feat: wire LessonStore into CLI initialization"
```

---

### Task 8: Integration test — SmartPlanner + LessonStore round-trip

**Files:**
- Create: `tests/test_smart_integration.py`

**Step 1: Write integration test**

Create `tests/test_smart_integration.py`:

```python
"""Integration test: SmartPlanner classifies, orchestrator learns, lessons feed back."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.memory.lessons import LessonStore
from horse_fish.models import Run, RunState, Subtask, SubtaskResult, SubtaskState, TaskComplexity
from horse_fish.planner.smart import SmartPlanner
from horse_fish.store.db import Store


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "integration.db")
    db.migrate()
    return db


@pytest.fixture
def lesson_store(store):
    return LessonStore(store)


@pytest.mark.asyncio
async def test_over_decomposition_feeds_back_to_classification(store, lesson_store):
    """Round-trip: over-decomposition detected → lesson stored → next run queries it."""
    # 1. Simulate a completed run that was over-decomposed
    run = Run.create("Add version string")
    run.state = RunState.completed
    run.subtasks = [
        Subtask(id="s1", description="Add version", state=SubtaskState.done,
                result=SubtaskResult(subtask_id="s1", success=True, output="ok",
                                     diff="diff --git a/src/__init__.py", duration_seconds=10)),
        Subtask(id="s2", description="Write test", state=SubtaskState.done,
                result=SubtaskResult(subtask_id="s2", success=True, output="ok",
                                     diff="diff --git a/src/__init__.py", duration_seconds=5)),
        Subtask(id="s3", description="Commit", state=SubtaskState.done,
                result=SubtaskResult(subtask_id="s3", success=True, output="ok",
                                     diff="", duration_seconds=2)),
    ]

    # 2. Extract and store lessons
    lessons = lesson_store.extract_lessons(run)
    assert any(l.pattern == "over_decomposed" for l in lessons)
    for l in lessons:
        lesson_store.store_lesson(l)

    # 3. Query lessons for a similar task
    retrieved = lesson_store.get_lessons_for_task("Add version to package", category="planner")
    assert len(retrieved) >= 1
    assert retrieved[0].pattern == "over_decomposed"

    # 4. SmartPlanner should see these lessons
    mock_planner = MagicMock()
    mock_planner._build_command = MagicMock(return_value=["cmd"])
    mock_planner._run_cli = AsyncMock(return_value="SOLO")
    mock_planner.runtime = "claude"
    mock_planner.model = "test"

    smart = SmartPlanner(mock_planner, lesson_store=lesson_store)
    lessons_text = smart._get_lessons("Add version to package")
    assert "over_decomposed" in lessons_text


@pytest.mark.asyncio
async def test_ceremony_strip_end_to_end():
    """Full flow: classify as TRIO, decompose, strip ceremony, get clean subtasks."""
    mock_planner = MagicMock()
    mock_planner._build_command = MagicMock(return_value=["cmd"])
    mock_planner._run_cli = AsyncMock(return_value="TRIO")
    mock_planner.runtime = "claude"
    mock_planner.model = "test"
    mock_planner.decompose = AsyncMock(return_value=[
        Subtask.create("Add user model to models.py"),
        Subtask.create("Add user API route"),
        Subtask.create("Write tests for user model"),
        Subtask.create("Run linter and format"),
        Subtask.create("Commit all changes"),
    ])

    smart = SmartPlanner(mock_planner)
    subtasks, complexity = await smart.decompose("Add user management")

    assert complexity == TaskComplexity.trio
    descriptions = [s.description for s in subtasks]
    # Ceremony stripped
    assert "Run linter and format" not in descriptions
    assert "Commit all changes" not in descriptions
    # Real work preserved
    assert "Add user model to models.py" in descriptions
    assert "Add user API route" in descriptions
    # Capped at 3
    assert len(subtasks) <= 3
```

**Step 2: Run integration test**

Run: `pytest tests/test_smart_integration.py -v`
Expected: All PASS

**Step 3: Run full test suite**

Run: `pytest -v`
Expected: All PASS

**Step 4: Commit**

```bash
git add tests/test_smart_integration.py
git commit -m "test: add integration tests for SmartPlanner + LessonStore round-trip"
```
