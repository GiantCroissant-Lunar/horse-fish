"""Lesson store — structured pattern extraction from completed runs."""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from horse_fish.models import Task
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

    def extract_lessons(self, run: Task) -> list[Lesson]:
        """Analyze a completed run and extract structured lessons."""
        lessons: list[Lesson] = []

        # Over-decomposition: many subtasks, few files touched
        if len(run.subtasks) > 1:
            files_touched: set[str] = set()
            for s in run.subtasks:
                if s.result and s.result.diff:
                    files_touched.update(_DIFF_FILE_RE.findall(s.result.diff))
            if len(files_touched) <= 2 and len(run.subtasks) > 2:
                lessons.append(
                    Lesson(
                        run_id=run.id,
                        category="planner",
                        pattern="over_decomposed",
                        content=(
                            f"Task '{run.task}' was split into {len(run.subtasks)} subtasks "
                            f"but only touched {len(files_touched)} file(s). Should be SOLO."
                        ),
                        task_signature=self._normalize(run.task),
                    )
                )

        # Stall detection: subtasks that were retried
        for s in run.subtasks:
            if s.retry_count > 0 and s.result:
                runtime = s.result.agent_runtime or "unknown"
                lessons.append(
                    Lesson(
                        run_id=run.id,
                        category="dispatch",
                        pattern="agent_stalled",
                        content=(f"Subtask '{s.description}' stalled {s.retry_count} time(s) with {runtime}."),
                        task_signature=self._normalize(s.description),
                    )
                )

        # No-diff: agent reported success but produced no file changes
        for s in run.subtasks:
            if s.result and s.result.success and not s.result.diff:
                runtime = s.result.agent_runtime or "unknown"
                lessons.append(
                    Lesson(
                        run_id=run.id,
                        category="agent",
                        pattern="no_diff",
                        content=(f"Agent {runtime} produced no diff for '{s.description}'."),
                        task_signature=self._normalize(s.description),
                    )
                )

        return lessons

    def store_lesson(self, lesson: Lesson) -> None:
        """Persist a lesson to SQLite."""
        self._store.execute(
            "INSERT OR IGNORE INTO lessons (id, run_id, category, pattern, content, task_signature, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                lesson.id,
                lesson.run_id,
                lesson.category,
                lesson.pattern,
                lesson.content,
                lesson.task_signature,
                lesson.created_at,
            ),
        )

    def get_lessons_for_task(self, task: str, category: str | None = None, limit: int = 5) -> list[Lesson]:
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
            row_sig = row.get("task_signature", "")
            if not sig or not row_sig or self._overlap(sig, row_sig):
                lessons.append(
                    Lesson(
                        id=row["id"],
                        run_id=row["run_id"],
                        category=row["category"],
                        pattern=row["pattern"],
                        content=row["content"],
                        task_signature=row_sig,
                        created_at=row["created_at"],
                    )
                )
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
