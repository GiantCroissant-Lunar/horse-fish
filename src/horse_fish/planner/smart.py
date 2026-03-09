"""SmartPlanner — complexity-aware task decomposition."""

from __future__ import annotations

import logging
import re
from typing import Any

from horse_fish.memory.lessons import LessonStore
from horse_fish.models import Subtask, TaskComplexity
from horse_fish.observability.prompts import resolve_text_prompt
from horse_fish.planner.decompose import Planner

logger = logging.getLogger(__name__)

_CLASSIFY_PROMPT = """\
Estimate the complexity of this coding task. Default to SOLO unless clearly wrong.

- SOLO: One feature, one bug fix, one refactor. Even if it touches 2-3 files in the same component. \
A single agent handles everything. Most tasks are SOLO.
- TRIO: Truly independent changes across 2-3 separate components that benefit from parallel work. \
NOT just "multiple files" — only if the changes have zero coupling.
- SQUAD: 5+ independent components, large-scale refactor across the entire codebase.

When in doubt, choose SOLO. Over-decomposition wastes more time than under-decomposition.

{lessons}

Task: {task}
Context: {context}

Reply with ONLY one word: SOLO, TRIO, or SQUAD.
"""

CLASSIFY_PROMPT_NAME = "smart-planner-classify"

_CEREMONY_PATTERNS = re.compile(
    r"^(commit|run tests|write tests for|commit all|format code|run linter|"
    r"push changes|create pr|open pull request|verify everything|"
    r"lint source|stage changes)",
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
        cognee_memory: Any | None = None,
    ) -> None:
        self._planner = planner
        self._lessons = lesson_store
        self._cognee = cognee_memory

    async def decompose(self, task: str, context: str = "") -> tuple[list[Subtask], TaskComplexity]:
        """Classify task complexity, then decompose if needed.

        Returns:
            Tuple of (subtasks, complexity).
        """
        # 1. Query lessons
        lessons_text = self._get_lessons(task)

        # 2. Query Cognee for semantic context from past runs
        cognee_context = await self._get_cognee_context(task)
        if cognee_context:
            past_work = f"Past similar work:\n{cognee_context}"
            context = f"{context}\n\n{past_work}" if context else past_work

        # 3. Classify
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
        tracer = getattr(self._planner, "_tracer", None)
        resolved_prompt = resolve_text_prompt(
            tracer,
            CLASSIFY_PROMPT_NAME,
            _CLASSIFY_PROMPT,
            task=task,
            context=context or "No additional context.",
            lessons=f"Lessons from past runs:\n{lessons}" if lessons else "",
        )
        prompt = resolved_prompt.compiled
        generation = (
            tracer.generation(
                None,
                "smart_planner.classify",
                input={"task": task, "context": context, "lessons": lessons, "prompt": prompt},
                metadata={
                    "runtime": self._planner.runtime,
                    "model": self._planner.model,
                    "has_lessons": bool(lessons),
                    "prompt_name": resolved_prompt.name,
                    "prompt_source": resolved_prompt.source,
                    "prompt_version": resolved_prompt.version,
                },
                model=self._planner.model,
                model_parameters={"runtime": self._planner.runtime},
                prompt=resolved_prompt.prompt_client,
            )
            if tracer
            else None
        )
        try:
            cmd = self._planner._build_command(prompt)
            raw = await self._planner._run_cli(cmd)
            complexity = self._parse_complexity(raw.strip())
            if tracer and generation:
                tracer.end_span(
                    generation,
                    {"raw_output": raw.strip(), "complexity": complexity.value},
                    metadata={
                        "runtime": self._planner.runtime,
                        "model": self._planner.model,
                        "prompt_name": resolved_prompt.name,
                        "prompt_source": resolved_prompt.source,
                        "prompt_version": resolved_prompt.version,
                    },
                )
            return complexity
        except Exception as exc:
            if tracer and generation:
                tracer.end_span(
                    generation,
                    {"error": str(exc)},
                    metadata={
                        "runtime": self._planner.runtime,
                        "model": self._planner.model,
                        "prompt_name": resolved_prompt.name,
                        "prompt_source": resolved_prompt.source,
                    },
                    level="ERROR",
                    status_message=str(exc),
                )
            logger.warning("Classification failed, defaulting to SOLO: %s", exc)
            return TaskComplexity.solo

    @staticmethod
    def _parse_complexity(raw: str) -> TaskComplexity:
        """Parse LLM response into TaskComplexity."""
        text = raw.strip().upper()
        for complexity in TaskComplexity:
            if complexity.value in text:
                return complexity
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
            return "\n".join(f"- [{les.pattern}] {les.content}" for les in lessons)
        except Exception as exc:
            logger.warning("Failed to query lessons: %s", exc)
            return ""

    async def _get_cognee_context(self, task: str) -> str:
        """Retrieve relevant context from Cognee knowledge graph."""
        if not self._cognee:
            return ""
        try:
            hits = await self._cognee.find_similar_tasks(task)
            if not hits:
                return ""
            return "\n".join(f"- {hit.content}" for hit in hits[:3])
        except Exception as exc:
            logger.warning("Failed to query Cognee: %s", exc)
            return ""
