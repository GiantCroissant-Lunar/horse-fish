"""PlanExecutor — orchestrates HTN+GOAP round loop for Plan execution."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from horse_fish.models import Plan, PlanState, Task, TaskState
from horse_fish.planner.goal import GoalPlanner
from horse_fish.store.db import Store

logger = logging.getLogger(__name__)


class PlanExecutor:
    """Executes a Plan through incremental HTN decomposition rounds."""

    def __init__(
        self,
        store: Store,
        goal_planner: GoalPlanner,
        run_task_fn: Callable[[str], Awaitable[Task]],
        max_rounds: int = 10,
    ) -> None:
        self._store = store
        self._goal_planner = goal_planner
        self._run_task_fn = run_task_fn
        self._max_rounds = max_rounds

    async def execute(self, goal: str) -> Plan:
        """Execute a goal through iterative decomposition rounds.

        1. Create Plan and persist
        2. Initial decomposition via GoalPlanner
        3. Round loop: execute tasks -> evaluate goal -> replan if needed
        """
        # 1. Create Plan
        plan = Plan.create(goal)
        plan.max_rounds = self._max_rounds
        self._persist_plan(plan)

        # 2. Round 0: initial decomposition
        decomposition = await self._goal_planner.decompose_goal(goal)
        plan.goal_conditions = decomposition.goal_conditions
        plan.state = PlanState.executing
        self._persist_plan(plan)

        round_tasks = decomposition.task_descriptions

        # 3. Round loop
        for current_round in range(self._max_rounds):
            plan.round = current_round

            # Execute this round's tasks (respecting deps)
            completed = await self._execute_round(plan, round_tasks)

            # Check for failures
            failed = [t for t in completed if t.state == TaskState.failed]
            if failed:
                plan.state = PlanState.failed
                plan.completed_at = datetime.now(UTC)
                self._persist_plan(plan)
                return plan

            # Evaluate goal (GOAP check)
            plan.state = PlanState.replanning
            self._persist_plan(plan)

            summaries = [f"[{t.state}] {t.task}" for t in plan.tasks]
            evaluation = await self._goal_planner.evaluate_goal(
                goal=plan.goal,
                goal_conditions=plan.goal_conditions,
                completed_task_summaries=summaries,
            )

            if evaluation.goal_met:
                plan.state = PlanState.completed
                plan.completed_at = datetime.now(UTC)
                self._persist_plan(plan)
                return plan

            # Not met — get next tasks
            if not evaluation.next_tasks:
                plan.state = PlanState.failed
                plan.completed_at = datetime.now(UTC)
                self._persist_plan(plan)
                return plan

            round_tasks = evaluation.next_tasks

        # Exceeded max rounds
        plan.state = PlanState.failed
        plan.completed_at = datetime.now(UTC)
        self._persist_plan(plan)
        return plan

    async def _execute_round(self, plan: Plan, round_tasks: list[dict]) -> list[Task]:
        """Execute tasks respecting dependency order.

        Tasks list deps by description string (matching other task descriptions).
        Runs tasks whose deps are all in the completed set. Currently sequential.
        """
        completed_descriptions: set[str] = set()
        pending = list(round_tasks)
        results: list[Task] = []

        while pending:
            ready = [t for t in pending if all(dep in completed_descriptions for dep in t.get("deps", []))]
            if not ready:
                # Deadlock — no task can proceed
                logger.error("Dependency deadlock: %s", [t["description"] for t in pending])
                break

            for task_desc in ready:
                task = await self._run_task_fn(task_desc["description"])
                plan.tasks.append(task)
                results.append(task)
                completed_descriptions.add(task_desc["description"])
                pending.remove(task_desc)

        return results

    def _persist_plan(self, plan: Plan) -> None:
        """Persist plan state to SQLite. Failures are logged but don't crash execution."""
        try:
            self._store.upsert_plan(
                plan_id=plan.id,
                goal=plan.goal,
                state=plan.state.value,
                goal_conditions=plan.goal_conditions,
                round=plan.round,
                created_at=plan.created_at.isoformat(),
                completed_at=plan.completed_at.isoformat() if plan.completed_at else None,
            )
        except Exception:
            logger.warning("Failed to persist plan %s", plan.id, exc_info=True)
