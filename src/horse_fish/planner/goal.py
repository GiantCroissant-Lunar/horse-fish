"""Goal-oriented planner: HTN decomposition + GOAP goal evaluation."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from horse_fish.planner.decompose import Planner

logger = logging.getLogger(__name__)

_GOAL_CONDITIONS_PROMPT = """\
Given the following high-level goal, generate 2-5 concrete, verifiable success conditions.
Each condition should be specific enough that an automated system can check whether it is met
by examining code, tests, or project state.

Goal: {goal}

Return ONLY a JSON array of strings. Example:
["All unit tests pass", "New endpoint returns 200 OK", "Documentation updated"]
"""

_EVALUATE_GOAL_PROMPT = """\
Evaluate whether the following goal has been achieved based on the completed work.

Goal: {goal}

Success conditions:
{goal_conditions}

Completed work summaries:
{completed_summaries}

Return ONLY a JSON object with this structure:
{{
  "goal_met": true/false,
  "reasoning": "explanation of why the goal is or is not met",
  "next_tasks": [
    {{"description": "what still needs to be done", "deps": []}}
  ]
}}

If goal_met is true, next_tasks should be empty. If false, list concrete tasks to finish the goal.
"""

_DECOMPOSE_GOAL_PROMPT = """\
Decompose the following high-level goal into concrete coding tasks.
Each task should be independently implementable by a single AI coding agent.

Goal: {goal}

Success conditions that must be met:
{goal_conditions}

Return ONLY a JSON array of task objects:
[
  {{
    "description": "clear description of what to implement",
    "deps": ["description of prerequisite task if any"],
    "files_hint": ["src/path/to/file.py"]
  }}
]

Rules:
- Each task must be independently testable
- Use deps to express ordering constraints
- Aim for 2-6 tasks
- Be specific about file paths when possible
"""


@dataclass
class GoalEvaluation:
    """Result of evaluating whether a goal has been achieved."""

    goal_met: bool
    reasoning: str
    next_tasks: list[dict] = field(default_factory=list)


@dataclass
class GoalDecomposition:
    """Result of decomposing a goal into tasks."""

    goal_conditions: list[str]
    task_descriptions: list[dict] = field(default_factory=list)  # [{description, deps, files_hint}]


class GoalPlanner:
    """HTN decomposition + GOAP goal evaluation at the Plan level.

    Wraps the existing Planner to work at a higher abstraction level:
    - Generates verifiable goal conditions (GOAP-style)
    - Decomposes goals into task DAGs (HTN-style)
    - Evaluates whether goals are met given completed work
    """

    def __init__(self, planner: Planner) -> None:
        self._planner = planner

    async def generate_goal_conditions(self, goal: str) -> list[str]:
        """Generate verifiable success conditions for a goal via LLM.

        Falls back to a generic condition if LLM call or parsing fails.
        """
        prompt = _GOAL_CONDITIONS_PROMPT.format(goal=goal)
        try:
            cmd = self._planner._build_command(prompt)
            raw = await self._planner._run_cli(cmd)
            conditions = self._parse_json_array(raw)
            if conditions and all(isinstance(c, str) for c in conditions):
                return conditions
        except Exception:
            logger.warning("Failed to generate goal conditions via LLM, using fallback", exc_info=True)

        logger.info("Using fallback goal condition for: %s", goal[:80])
        return [f"Goal completed: {goal}"]

    async def evaluate_goal(
        self,
        goal: str,
        goal_conditions: list[str],
        completed_task_summaries: list[str],
    ) -> GoalEvaluation:
        """Evaluate whether goal conditions are met given completed work.

        Falls back to goal_met=False if LLM call or parsing fails.
        """
        conditions_text = "\n".join(f"- {c}" for c in goal_conditions)
        summaries_text = "\n".join(f"- {s}" for s in completed_task_summaries)
        prompt = _EVALUATE_GOAL_PROMPT.format(
            goal=goal,
            goal_conditions=conditions_text,
            completed_summaries=summaries_text,
        )
        try:
            cmd = self._planner._build_command(prompt)
            raw = await self._planner._run_cli(cmd)
            obj = self._parse_json_object(raw)
            if obj and "goal_met" in obj:
                return GoalEvaluation(
                    goal_met=bool(obj["goal_met"]),
                    reasoning=obj.get("reasoning", ""),
                    next_tasks=obj.get("next_tasks", []),
                )
        except Exception:
            logger.warning("Failed to evaluate goal via LLM, using fallback", exc_info=True)

        return GoalEvaluation(goal_met=False, reasoning="Could not evaluate goal — LLM call failed")

    async def decompose_goal(self, goal: str) -> GoalDecomposition:
        """Decompose a goal into conditions + task DAG.

        Calls LLM twice: once for goal conditions, once for task decomposition.
        Falls back to a single task wrapping the goal on failure.
        """
        # Step 1: Generate goal conditions
        goal_conditions = await self.generate_goal_conditions(goal)

        # Step 2: Decompose into tasks
        conditions_text = "\n".join(f"- {c}" for c in goal_conditions)
        prompt = _DECOMPOSE_GOAL_PROMPT.format(goal=goal, goal_conditions=conditions_text)
        try:
            cmd = self._planner._build_command(prompt)
            raw = await self._planner._run_cli(cmd)
            tasks = self._parse_json_array_of_objects(raw)
            if tasks:
                return GoalDecomposition(goal_conditions=goal_conditions, task_descriptions=tasks)
        except Exception:
            logger.warning("Failed to decompose goal via LLM, using fallback", exc_info=True)

        # Fallback: single task wrapping the entire goal
        logger.info("Using fallback single-task decomposition for: %s", goal[:80])
        return GoalDecomposition(
            goal_conditions=goal_conditions,
            task_descriptions=[{"description": goal, "deps": [], "files_hint": []}],
        )

    @staticmethod
    def _strip_fences(text: str) -> str:
        """Strip markdown code fences if present."""
        text = text.strip()
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence_match:
            return fence_match.group(1).strip()
        return text

    @staticmethod
    def _parse_json_array(raw: str) -> list | None:
        """Parse a JSON array from raw text, stripping markdown fences."""
        text = GoalPlanner._strip_fences(raw)
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    @staticmethod
    def _parse_json_object(raw: str) -> dict | None:
        """Parse a JSON object from raw text, stripping markdown fences."""
        text = GoalPlanner._strip_fences(raw)
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        return None

    @staticmethod
    def _parse_json_array_of_objects(raw: str) -> list[dict] | None:
        """Parse a JSON array of objects from raw text, stripping markdown fences."""
        text = GoalPlanner._strip_fences(raw)
        try:
            data = json.loads(text)
            if isinstance(data, list) and all(isinstance(item, dict) for item in data):
                return data
        except (json.JSONDecodeError, ValueError):
            pass
        return None
