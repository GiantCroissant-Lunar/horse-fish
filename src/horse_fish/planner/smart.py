"""SmartPlanner — wraps Planner with complexity classification and ceremony stripping."""

from __future__ import annotations

import re
import uuid

from horse_fish.memory.lessons import LessonStore
from horse_fish.models import Subtask, TaskComplexity
from horse_fish.planner.decompose import Planner

_COMPLEXITY_CAPS: dict[TaskComplexity, int] = {
    TaskComplexity.SOLO: 1,
    TaskComplexity.TRIO: 3,
    TaskComplexity.SQUAD: 8,
}

_CEREMONY_PATTERNS = re.compile(
    r"\b(commit|tests?|review|format|lint|verify|push|check|validate|stage)\b",
    re.IGNORECASE,
)

_CLASSIFICATION_PROMPT = """\
Classify the complexity of this task. Reply with exactly one word:
- SOLO: single focused change (one file, one function, trivial fix)
- TRIO: small feature needing 2-3 coordinated changes
- SQUAD: large feature requiring 4+ changes across many files

{lessons}

Task: {task}
Context: {context}

Reply SOLO, TRIO, or SQUAD only.
"""


class SmartPlanner:
    """Wraps Planner with complexity classification, ceremony stripping, and subtask capping."""

    def __init__(
        self,
        runtime: str = "claude",
        model: str | None = None,
        lesson_store: LessonStore | None = None,
    ) -> None:
        self._planner = Planner(runtime=runtime, model=model)
        self._lesson_store = lesson_store

    async def decompose(self, task: str, context: str = "") -> tuple[list[Subtask], TaskComplexity]:
        """Decompose task into subtasks with complexity classification.

        Returns:
            Tuple of (subtasks, complexity). Falls back to a single-subtask wrapping the
            full task if decomposition fails, returns empty, or all subtasks are ceremony.
        """
        lessons = await self._get_lessons(task)
        complexity = await self._classify(task, context, lessons)
        cap = _COMPLEXITY_CAPS[complexity]

        try:
            raw_subtasks = await self._planner.decompose(task, context)
        except Exception:
            return [self._wrap_task(task)], TaskComplexity.SOLO

        stripped = self._strip_ceremony(raw_subtasks)

        if not stripped:
            return [self._wrap_task(task)], complexity

        return stripped[:cap], complexity

    async def _classify(self, task: str, context: str, lessons: str) -> TaskComplexity:
        """Send classification prompt to LLM via planner and parse response."""
        prompt = _CLASSIFICATION_PROMPT.format(
            task=task,
            context=context or "No additional context.",
            lessons=f"Relevant lessons:\n{lessons}" if lessons else "",
        )
        try:
            cmd = self._planner._build_command(prompt)
            raw = await self._planner._run_cli(cmd)
            return self._parse_complexity(raw)
        except Exception:
            return TaskComplexity.SOLO

    def _parse_complexity(self, raw: str) -> TaskComplexity:
        """Parse SOLO/TRIO/SQUAD from LLM response, default SOLO if unparseable."""
        text = raw.strip().upper()
        for complexity in (TaskComplexity.SQUAD, TaskComplexity.TRIO, TaskComplexity.SOLO):
            if complexity.value in text:
                return complexity
        return TaskComplexity.SOLO

    def _strip_ceremony(self, subtasks: list[Subtask]) -> list[Subtask]:
        """Remove subtasks matching ceremony patterns (commit, test, review, etc.)."""
        return [s for s in subtasks if not _CEREMONY_PATTERNS.search(s.description)]

    async def _get_lessons(self, task: str) -> str:
        """Query LessonStore for relevant lessons, or return empty string."""
        if self._lesson_store is None:
            return ""
        return await self._lesson_store.get_lessons(task)

    def _wrap_task(self, task: str) -> Subtask:
        """Wrap entire task as a single fallback subtask."""
        return Subtask(id=str(uuid.uuid4()), description=task)
