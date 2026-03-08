"""Orchestrator state machine: plan → execute → review → merge → learn."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from horse_fish.agents.pool import AgentPool
from horse_fish.dispatch.selector import AgentSelector
from horse_fish.memory.store import MemoryStore
from horse_fish.merge.queue import MergeQueue
from horse_fish.models import AgentState, Run, RunState, SubtaskState
from horse_fish.observability.traces import Tracer
from horse_fish.planner.decompose import Planner
from horse_fish.validation.gates import ValidationGates

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 10
SUBTASK_TIMEOUT_SECONDS = 600  # 10 minutes


class OrchestratorError(Exception):
    """Raised when the orchestrator encounters an unrecoverable error."""


class Orchestrator:
    """Drives a Run through its lifecycle: plan → execute → review → merge."""

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
        concurrency_limits: dict[RunState, int] | None = None,
    ) -> None:
        self._pool = pool
        self._planner = planner
        self._gates = gates
        self._runtime = runtime
        self._model = model or ""
        self._max_agents = max_agents
        self._selector = selector
        self._merge_queue = merge_queue
        self._tracer = tracer
        self._memory = memory
        self._concurrency_limits = concurrency_limits or {}

        self._handlers: dict[RunState, _Handler] = {
            RunState.planning: self._plan,
            RunState.executing: self._execute,
            RunState.reviewing: self._review,
            RunState.merging: self._merge,
        }

    async def run(self, task: str) -> Run:
        """Create a Run and drive it through the state machine until terminal."""
        run = Run.create(task)
        logger.info("Starting run %s for task: %s", run.id, task)

        trace = self._tracer.trace_run(run.id, task) if self._tracer else None

        while run.state not in (RunState.completed, RunState.failed):
            handler = self._handlers.get(run.state)
            if handler is None:
                raise OrchestratorError(f"No handler for state {run.state}")

            span = self._tracer.span(trace, run.state.value) if self._tracer and trace else None
            run = await handler(run)
            if self._tracer and span:
                self._tracer.end_span(span, {"state": run.state.value})

            logger.info("Run %s transitioned to %s", run.id, run.state)

        run.completed_at = datetime.now(UTC)

        if self._tracer and trace:
            self._tracer.end_trace(trace, run.state.value)

        if run.state == RunState.completed:
            await self._learn(run)

        return run

    async def _learn(self, run: Run) -> None:
        """Store completed run results in memory for future learning."""
        if not self._memory:
            return
        subtask_results = [s.result for s in run.subtasks if s.result]
        try:
            await self._memory.store_run_result(run, subtask_results)
        except Exception as exc:
            logger.warning("Failed to store run in memory: %s", exc)

    def _stamp_provenance(self, result: SubtaskResult, run: Run, agent_id: str) -> None:
        """Stamp provenance metadata on a SubtaskResult."""
        try:
            slot = self._pool._get_slot(agent_id)
            result.agent_id = slot.id
            result.agent_runtime = slot.runtime
            result.agent_model = slot.model
        except Exception:
            result.agent_id = agent_id
        result.run_id = run.id
        result.completed_at = datetime.now(UTC)

    async def _plan(self, run: Run) -> Run:
        """Decompose the task into subtasks via the Planner."""
        try:
            subtasks = await self._planner.decompose(run.task)
        except Exception as exc:
            logger.error("Planning failed: %s", exc)
            run.state = RunState.failed
            return run

        if not subtasks:
            logger.error("Planner returned no subtasks")
            run.state = RunState.failed
            return run

        run.subtasks = subtasks
        run.state = RunState.executing
        return run

    async def _execute(self, run: Run) -> Run:
        """Dispatch subtasks to agents and poll until all complete or fail."""
        agent_map: dict[str, str] = {}  # subtask_id → agent_id
        active_count = 0

        while True:
            # Dispatch ready subtasks (deps met, not yet running)
            for subtask in run.subtasks:
                if subtask.state != SubtaskState.pending:
                    continue
                max_concurrent = self._concurrency_limits.get(RunState.executing, self._max_agents)
                if active_count >= max_concurrent:
                    break
                if not self._deps_met(run, subtask):
                    continue

                try:
                    # Use selector if available, otherwise spawn directly
                    if self._selector:
                        available_agents = [a for a in self._pool.list_agents() if a.state == AgentState.idle]
                        selected = self._selector.select(subtask, available_agents)
                        if selected is None:
                            # Selector returned None — skip this subtask for now
                            continue
                        slot = selected
                    else:
                        slot = await self._pool.spawn(
                            name=f"hf-{subtask.id[:8]}",
                            runtime=self._runtime,
                            model=self._model,
                            capability="builder",
                        )
                    await self._pool.send_task(slot.id, subtask.description)
                    subtask.state = SubtaskState.running
                    subtask.agent = slot.id
                    agent_map[subtask.id] = slot.id
                    active_count += 1
                except Exception as exc:
                    logger.error("Failed to dispatch subtask %s: %s", subtask.id, exc)
                    subtask.state = SubtaskState.failed

            # Check if all subtasks are terminal
            if all(s.state in (SubtaskState.done, SubtaskState.failed) for s in run.subtasks):
                break

            # If nothing is running and nothing can be dispatched, we're stuck
            running = [s for s in run.subtasks if s.state == SubtaskState.running]
            if not running and not any(
                s.state == SubtaskState.pending and self._deps_met(run, s) for s in run.subtasks
            ):
                logger.error("No subtasks running and none can be dispatched — deadlock")
                run.state = RunState.failed
                return run

            # Poll running subtasks
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

            for subtask in running:
                agent_id = agent_map.get(subtask.id)
                if not agent_id:
                    continue

                status = await self._pool.check_status(agent_id)
                if status.value == "dead":
                    # Agent died — check if it produced output
                    result = await self._pool.collect_result(agent_id)
                    subtask.result = result
                    self._stamp_provenance(result, run, agent_id)
                    subtask.state = SubtaskState.done if result.success else SubtaskState.failed
                    active_count -= 1
                    continue

                # Check for new commits in worktree (primary completion signal)
                try:
                    result = await self._pool.collect_result(agent_id)
                    if result.diff:
                        subtask.result = result
                        self._stamp_provenance(result, run, agent_id)
                        subtask.state = SubtaskState.done
                        active_count -= 1
                except Exception:
                    pass

        # Any failures?
        if any(s.state == SubtaskState.failed for s in run.subtasks):
            run.state = RunState.failed
            return run

        run.state = RunState.reviewing
        return run

    async def _review(self, run: Run) -> Run:
        """Run validation gates on each completed subtask's worktree."""
        all_passed = True
        for subtask in run.subtasks:
            if subtask.state != SubtaskState.done or not subtask.agent:
                continue

            try:
                slot = self._pool._get_slot(subtask.agent)
                if not slot.worktree_path:
                    continue
                results = await self._gates.run_all(slot.worktree_path)
                if not self._gates.all_passed(results):
                    subtask.state = SubtaskState.failed
                    all_passed = False
                    gate_output = "; ".join(f"{r.gate}: {r.output}" for r in results if not r.passed)
                    logger.warning("Subtask %s failed gates: %s", subtask.id, gate_output)
            except Exception as exc:
                logger.error("Review failed for subtask %s: %s", subtask.id, exc)
                subtask.state = SubtaskState.failed
                all_passed = False

        run.state = RunState.merging if all_passed else RunState.failed
        return run

    async def _merge(self, run: Run) -> Run:
        """Merge each subtask's worktree branch into main."""
        if self._merge_queue:
            # Use merge queue: enqueue all subtasks, then process
            for subtask in run.subtasks:
                if subtask.state != SubtaskState.done or not subtask.agent:
                    continue
                slot = self._pool._get_slot(subtask.agent)
                await self._merge_queue.enqueue(subtask.id, slot.name, slot.branch)

            results = await self._merge_queue.process()
            for result in results:
                if not result.success:
                    logger.error("Merge conflict for subtask %s", result.subtask_id)
                    run.state = RunState.failed
                    return run
        else:
            # Fallback: direct merge without queue
            for subtask in run.subtasks:
                if subtask.state != SubtaskState.done or not subtask.agent:
                    continue

                try:
                    slot = self._pool._get_slot(subtask.agent)
                    success = await self._pool._worktrees.merge(slot.name)
                    if not success:
                        logger.error("Merge conflict for subtask %s", subtask.id)
                        subtask.state = SubtaskState.failed
                        run.state = RunState.failed
                        return run
                except Exception as exc:
                    logger.error("Merge failed for subtask %s: %s", subtask.id, exc)
                    subtask.state = SubtaskState.failed
                    run.state = RunState.failed
                    return run

        run.state = RunState.completed
        return run

    @staticmethod
    def _deps_met(run: Run, subtask) -> bool:
        """Check if all dependencies of a subtask are done."""
        if not subtask.deps:
            return True
        done_descriptions = {s.description for s in run.subtasks if s.state == SubtaskState.done}
        return all(dep in done_descriptions for dep in subtask.deps)


_Handler = type(Orchestrator._plan)  # just for type alias readability
