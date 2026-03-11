"""Orchestrator state machine: plan → execute → review → merge → learn."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any

from horse_fish.agents.pool import AgentPool
from horse_fish.dispatch.selector import AgentSelector
from horse_fish.memory.lessons import LessonStore
from horse_fish.memory.store import MemoryStore
from horse_fish.merge.queue import MergeQueue
from horse_fish.models import AgentState, ContextBrief, Subtask, SubtaskResult, SubtaskState, Task, TaskState
from horse_fish.observability.log_context import reset_log_context, set_log_context
from horse_fish.observability.traces import Tracer
from horse_fish.orchestrator.middleware import (
    LogContextMiddleware,
    MemoryMiddleware,
    MiddlewareContext,
    PersistenceMiddleware,
    ScoutContextMiddleware,
    TracingMiddleware,
    compose_chain,
)
from horse_fish.planner.decompose import Planner
from horse_fish.planner.scout import SCOUT_PROMPT_TEMPLATE, parse_scout_output, programmatic_scout
from horse_fish.planner.smart import SmartPlanner
from horse_fish.store.db import Store
from horse_fish.validation.gates import ValidationGates

try:
    from horse_fish.memory.cognee_store import CogneeMemory
except ImportError:
    CogneeMemory = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 10
SUBTASK_TIMEOUT_SECONDS = 600  # 10 minutes
STALL_TIMEOUT_SECONDS = 300  # 5 minutes default


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
        lesson_store: LessonStore | None = None,
        cognee_memory: CogneeMemory | None = None,
        stall_timeout_seconds: int = STALL_TIMEOUT_SECONDS,
        concurrency_limits: dict[TaskState, int] | None = None,
        store: Store | None = None,
        allow_partial_success: bool = False,
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
        self._lesson_store = lesson_store
        self._cognee_memory = cognee_memory
        self._smart_planner = (
            SmartPlanner(planner, lesson_store=lesson_store, cognee_memory=cognee_memory)
            if lesson_store or cognee_memory
            else None
        )
        self._stall_timeout = stall_timeout_seconds
        self._concurrency_limits = concurrency_limits or {}
        self._store = store
        self._allow_partial_success = allow_partial_success
        self._active_trace = None
        self._execution_retry_events = 0
        self._execution_retry_exhausted = 0
        self._gate_retry_events = 0
        self._gate_retry_exhausted = 0
        self._merge_conflicts: list[dict[str, object]] = []
        self._context_brief: ContextBrief | None = None
        self._handlers: dict[TaskState, _Handler] = {
            TaskState.scouting: self._scout,
            TaskState.planning: self._plan,
            TaskState.executing: self._execute,
            TaskState.reviewing: self._review,
            TaskState.merging: self._merge,
        }

        # Build middleware chain
        self._middlewares: list = [
            TracingMiddleware(tracer),
            LogContextMiddleware(),
            PersistenceMiddleware(self._async_persist_run),
            MemoryMiddleware(self._learn),
            ScoutContextMiddleware(lambda: self._context_brief),
        ]

    async def _async_persist_run(self, run: Task) -> None:
        """Async wrapper for _persist_run (middleware expects async)."""
        self._persist_run(run)

    def _subtask_span(
        self,
        name: str,
        subtask: Subtask,
        **metadata,
    ):
        """Create a best-effort span for a specific subtask operation."""
        if not self._tracer or not self._active_trace:
            return None
        base_metadata = {
            "subtask_id": subtask.id,
            "description": subtask.description,
            "agent_id": subtask.agent,
            "retry_count": subtask.retry_count,
            "gate_retry_count": subtask.gate_retry_count,
        }
        base_metadata.update(metadata)
        return self._tracer.span(self._active_trace, name, base_metadata)

    def _reset_trace_metrics(self) -> None:
        """Reset per-run observability counters before a new run starts."""
        self._execution_retry_events = 0
        self._execution_retry_exhausted = 0
        self._gate_retry_events = 0
        self._gate_retry_exhausted = 0
        self._merge_conflicts = []

    def _record_merge_conflict(
        self,
        subtask_id: str,
        *,
        branch: str | None = None,
        conflict_files: list[str] | None = None,
    ) -> None:
        """Store merge conflict details for later Langfuse scoring."""
        self._merge_conflicts.append(
            {
                "subtask_id": subtask_id,
                "branch": branch,
                "conflict_files": conflict_files or [],
            }
        )

    def _score_run_outcomes(self, run: Task, trace) -> None:
        """Emit run-level Langfuse scores after the state machine finishes."""
        if not self._tracer or not trace:
            return
        runtime_summary = self._pool.runtime_observation_summary(run.id)
        subtask_coverage = (
            runtime_summary["subtasks_with_runtime_observations"] / len(run.subtasks) if run.subtasks else 0.0
        )

        self._tracer.score_trace(
            trace,
            "run_success",
            1.0 if run.state == TaskState.completed else 0.0,
            data_type="NUMERIC",
            metadata={"status": run.state.value},
        )
        self._tracer.score_trace(
            trace,
            "completed_subtasks",
            float(sum(1 for s in run.subtasks if s.state == SubtaskState.done)),
            data_type="NUMERIC",
        )
        self._tracer.score_trace(
            trace,
            "failed_subtasks",
            float(sum(1 for s in run.subtasks if s.state == SubtaskState.failed)),
            data_type="NUMERIC",
        )
        self._tracer.score_trace(
            trace,
            "execution_retry_count",
            float(self._execution_retry_events),
            data_type="NUMERIC",
            metadata={"retry_exhausted_count": self._execution_retry_exhausted},
        )
        self._tracer.score_trace(
            trace,
            "gate_retry_count",
            float(self._gate_retry_events),
            data_type="NUMERIC",
            metadata={"retry_exhausted_count": self._gate_retry_exhausted},
        )
        self._tracer.score_trace(
            trace,
            "retry_exhausted_count",
            float(self._execution_retry_exhausted + self._gate_retry_exhausted),
            data_type="NUMERIC",
            metadata={
                "execution_retry_exhausted_count": self._execution_retry_exhausted,
                "gate_retry_exhausted_count": self._gate_retry_exhausted,
            },
        )
        self._tracer.score_trace(
            trace,
            "merge_conflict_count",
            float(len(self._merge_conflicts)),
            data_type="NUMERIC",
            metadata={"conflicts": self._merge_conflicts},
        )
        self._tracer.score_trace(
            trace,
            "merge_conflict",
            "conflict" if self._merge_conflicts else "clean",
            data_type="CATEGORICAL",
            metadata={"count": len(self._merge_conflicts)},
        )
        self._tracer.score_trace(
            trace,
            "runtime_observation_count",
            float(runtime_summary["total_count"]),
            data_type="NUMERIC",
            metadata={
                "tool_count": runtime_summary["tool_count"],
                "prompt_count": runtime_summary["prompt_count"],
                "first_observed_at": runtime_summary["first_observed_at"],
                "last_observed_at": runtime_summary["last_observed_at"],
                "subtasks_with_runtime_observations": runtime_summary["subtasks_with_runtime_observations"],
                "subtask_ids": runtime_summary["subtask_ids"],
                "subtask_breakdown": runtime_summary["subtask_breakdown"],
                "runtimes": runtime_summary["runtimes"],
                "observation_names": runtime_summary["observation_names"],
                "recent_observations": runtime_summary["recent_observations"],
            },
        )
        self._tracer.score_trace(
            trace,
            "runtime_tool_observation_count",
            float(runtime_summary["tool_count"]),
            data_type="NUMERIC",
        )
        self._tracer.score_trace(
            trace,
            "runtime_prompt_observation_count",
            float(runtime_summary["prompt_count"]),
            data_type="NUMERIC",
        )
        self._tracer.score_trace(
            trace,
            "runtime_observation_subtask_coverage",
            subtask_coverage,
            data_type="NUMERIC",
            metadata={
                "subtasks_with_runtime_observations": runtime_summary["subtasks_with_runtime_observations"],
                "total_subtasks": len(run.subtasks),
                "subtask_ids": runtime_summary["subtask_ids"],
                "last_observed_at": runtime_summary["last_observed_at"],
            },
        )

    def _trace_output(self, run: Task) -> dict[str, Any]:
        """Build the final trace output payload."""
        runtime_summary = self._pool.runtime_observation_summary(run.id)
        return {
            "status": run.state.value,
            "subtask_count": len(run.subtasks),
            "completed_subtasks": sum(1 for s in run.subtasks if s.state == SubtaskState.done),
            "failed_subtasks": sum(1 for s in run.subtasks if s.state == SubtaskState.failed),
            "runtime_observations": {
                "total_count": runtime_summary["total_count"],
                "tool_count": runtime_summary["tool_count"],
                "prompt_count": runtime_summary["prompt_count"],
                "first_observed_at": runtime_summary["first_observed_at"],
                "last_observed_at": runtime_summary["last_observed_at"],
                "subtask_ids": runtime_summary["subtask_ids"],
                "recent_observations": runtime_summary["recent_observations"],
            },
        }

    def _persist_run(self, run: Task) -> None:
        """Persist run state to SQLite."""
        if not self._store:
            return
        self._store.upsert_run(
            run_id=run.id,
            task=run.task,
            state=run.state.value,
            complexity=run.complexity.value if run.complexity else None,
            created_at=run.created_at.isoformat() if run.created_at else datetime.now(UTC).isoformat(),
            completed_at=run.completed_at.isoformat() if run.completed_at else None,
        )

    def _persist_subtask(self, subtask: Subtask, run_id: str) -> None:
        """Persist subtask state to SQLite."""
        if not self._store:
            return
        self._store.upsert_subtask(
            subtask_id=subtask.id,
            run_id=run_id,
            description=subtask.description,
            state=subtask.state.value,
            agent_id=subtask.agent,
            deps=json.dumps(subtask.deps) if subtask.deps else None,
            retry_count=subtask.retry_count,
            created_at=datetime.now(UTC).isoformat(),
        )

    async def run(self, task: str) -> Task:
        """Create a Run and drive it through the state machine until terminal."""
        run = Task.create(task)
        self._persist_run(run)
        # Set logging context for the run
        context_tokens = set_log_context(run_id=run.id)
        logger.info("Starting run %s for task: %s", run.id, task)

        trace = (
            self._tracer.trace_run(
                run.id,
                task,
                metadata={
                    "runtime": self._runtime,
                    "model": self._model,
                    "max_agents": self._max_agents,
                },
                tags=[f"runtime:{self._runtime}"] + ([f"model:{self._model}"] if self._model else []),
            )
            if self._tracer
            else None
        )
        self._active_trace = trace
        self._reset_trace_metrics()

        try:
            while run.state not in (TaskState.completed, TaskState.failed, TaskState.partial_success):
                handler = self._handlers.get(run.state)
                if handler is None:
                    raise OrchestratorError(f"No handler for state {run.state}")

                ctx = MiddlewareContext(trace=trace, context_brief=self._context_brief)
                chain = compose_chain(self._middlewares, handler, ctx)
                run = await chain(run)
                # Propagate scout context brief from middleware
                if ctx.context_brief is not None:
                    self._context_brief = ctx.context_brief
                logger.info("Run %s transitioned to %s", run.id, run.state)
        finally:
            run.completed_at = datetime.now(UTC)
            self._persist_run(run)

            # Clear logging context
            reset_log_context(*context_tokens)

            if self._tracer and trace:
                self._score_run_outcomes(run, trace)
                self._tracer.end_trace(
                    trace,
                    run.state.value,
                    output=self._trace_output(run),
                )
            self._active_trace = None

        return run

    async def _learn(self, run: Task) -> None:
        """Store completed run results in memory for future learning."""
        subtask_results = [s.result for s in run.subtasks if s.result]

        # Tier 1: memvid (agent-local, backward compat)
        if self._memory:
            try:
                await self._memory.store_run_result(run, subtask_results)
            except Exception as exc:
                logger.warning("Failed to store run in memvid: %s", exc)

        # Tier 2: Store entries with metadata for later Cognee batch ingestion
        if self._memory:
            try:
                for result in subtask_results:
                    content = f"Subtask {result.subtask_id}: success={result.success}\nOutput: {result.output}"
                    if result.diff:
                        content += f"\nDiff: {result.diff}"
                    self._memory.store_entry(
                        content=content,
                        agent=getattr(result, "agent_runtime", "unknown") or "unknown",
                        run_id=run.id,
                        domain="run_result",
                        tags=["subtask", result.subtask_id],
                    )
            except Exception as exc:
                logger.warning("Failed to store run entries for Cognee ingestion: %s", exc)

        # Tier 3: Auto-organize into Cognee if available
        if self._cognee_memory and self._memory:
            try:
                uningested = self._memory.get_uningested()
                if uningested:
                    count = await self._cognee_memory.batch_ingest(uningested)
                    if count > 0:
                        self._memory.mark_ingested([e.id for e in uningested])
                    logger.info("Auto-organized %d entries into Cognee", count)
            except Exception as exc:
                logger.warning("Auto-organize into Cognee failed: %s", exc)

        # Lessons (deterministic pattern extraction)
        if self._lesson_store:
            try:
                lessons = self._lesson_store.extract_lessons(run)
                for lesson in lessons:
                    self._lesson_store.store_lesson(lesson)
                run.lessons = [lesson.id for lesson in lessons]
            except Exception as exc:
                logger.warning("Failed to extract lessons: %s", exc)

    def _stamp_provenance(self, result: SubtaskResult, run: Task, agent_id: str) -> None:
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

    async def _check_stalls(self, run: Task, agent_map: dict[str, str]) -> int:
        """Check for stalled agents. Returns count of subtasks no longer running (retried + failed)."""
        retried = 0
        failed = 0
        now = datetime.now(UTC)

        for subtask in run.subtasks:
            if subtask.state != SubtaskState.running:
                continue
            if subtask.last_activity_at is None:
                continue

            elapsed = (now - subtask.last_activity_at).total_seconds()
            # Gate-retried subtasks get double the stall timeout since the respawned
            # agent needs time to start up, read the fix prompt, and run gates.
            timeout = self._stall_timeout * 2 if subtask.gate_retry_count > 0 else self._stall_timeout
            if elapsed < timeout:
                continue

            logger.warning("Subtask %s stalled (%.0fs since last activity)", subtask.id, elapsed)

            agent_id = agent_map.get(subtask.id)
            stall_span = self._subtask_span(
                "subtask.stall_recovery",
                subtask,
                elapsed_seconds=elapsed,
                timeout_seconds=timeout,
                stalled_agent_id=agent_id,
            )
            if agent_id:
                try:
                    await self._pool.release(agent_id)
                except Exception:
                    pass

            if subtask.retry_count < subtask.max_retries:
                subtask.retry_count += 1
                self._execution_retry_events += 1
                subtask.state = SubtaskState.pending
                subtask.agent = None
                subtask.last_activity_at = None
                if subtask.id in agent_map:
                    del agent_map[subtask.id]
                retried += 1
                self._persist_subtask(subtask, run.id)
                logger.info("Retrying subtask %s (attempt %d/%d)", subtask.id, subtask.retry_count, subtask.max_retries)
                if stall_span:
                    self._tracer.end_span(
                        stall_span,
                        {"action": "retry"},
                        metadata={
                            "retry_count": subtask.retry_count,
                            "max_retries": subtask.max_retries,
                        },
                        level="WARNING",
                    )
            else:
                self._execution_retry_exhausted += 1
                subtask.state = SubtaskState.failed
                if subtask.id in agent_map:
                    del agent_map[subtask.id]
                failed += 1
                self._persist_subtask(subtask, run.id)
                logger.error("Subtask %s failed after %d retries", subtask.id, subtask.max_retries)
                if stall_span:
                    self._tracer.end_span(
                        stall_span,
                        {"action": "failed"},
                        metadata={
                            "retry_count": subtask.retry_count,
                            "max_retries": subtask.max_retries,
                        },
                        level="ERROR",
                        status_message="stall recovery exhausted retries",
                    )

        return retried + failed

    async def _scout(self, run: Task) -> Task:
        """Gather codebase context by spawning a scout agent.

        Spawns a real agent that explores the codebase and produces a ContextBrief.
        Falls back to programmatic scout if agent fails.
        """
        brief = await self._run_agent_scout(run)
        if brief is None:
            logger.info("Agent scout failed or unavailable, using programmatic fallback")
            try:
                brief = programmatic_scout(run.task)
            except Exception as exc:
                logger.warning("Programmatic scout also failed: %s", exc)
                brief = None

        self._context_brief = brief
        if brief:
            logger.info(
                "Scout phase complete: %d files, %d patterns, %d criteria",
                len(brief.relevant_files),
                len(brief.patterns),
                len(brief.acceptance_criteria),
            )
        run.state = TaskState.planning
        return run

    async def _run_agent_scout(self, run: Task) -> ContextBrief | None:
        """Spawn a scout agent, send the scout prompt, wait for output, parse brief."""
        import shutil
        from pathlib import Path

        repo_root = str(Path.cwd())

        # Read project context for the scout prompt
        project_context = ""
        claude_md = Path(repo_root) / "CLAUDE.md"
        if claude_md.exists():
            try:
                project_context = claude_md.read_text(encoding="utf-8")
            except Exception:
                pass

        prompt = SCOUT_PROMPT_TEMPLATE.format(
            task=run.task,
            project_context=project_context or "No project context file found.",
        )

        # Find first available runtime (prefer configured, then cheap options)
        from horse_fish.agents.runtime import RUNTIME_REGISTRY

        selected_runtime = None
        for rt_name in (self._runtime, "pi", "kimi", "claude"):
            if rt_name in RUNTIME_REGISTRY and shutil.which(rt_name):
                selected_runtime = rt_name
                break

        if not selected_runtime:
            logger.warning("No scout runtime available")
            return None

        scout_slot = None
        try:
            scout_slot = await self._pool.spawn_scout(
                runtime=selected_runtime,
                model=self._model or "",
                repo_root=repo_root,
            )

            # Send scout prompt (raw mode — no task/fix template wrapping)
            await self._pool.send_task(
                scout_slot.id,
                prompt,
                task_id="scout-" + run.id[:8],
                raw=True,
                run_id=run.id,
            )

            # Poll for completion (agent becomes dead or produces JSON output)
            scout_timeout = 120.0
            poll_interval = 5.0
            elapsed = 0.0
            while elapsed < scout_timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval

                status = await self._pool.check_status(scout_slot.id)
                if status == AgentState.dead:
                    break

                # Check if agent has produced JSON output
                output = await self._pool.collect_scout_output(scout_slot.id)
                if output and "{" in output and "}" in output:
                    brief = parse_scout_output(output)
                    if brief:
                        return brief

            # Final attempt to capture output after timeout/death
            output = await self._pool.collect_scout_output(scout_slot.id)
            if output:
                return parse_scout_output(output)

            return None

        except Exception as exc:
            logger.warning("Agent scout failed: %s", exc)
            return None
        finally:
            if scout_slot:
                try:
                    await self._pool.release_scout(scout_slot.id)
                except Exception:
                    pass

    async def _plan(self, run: Task) -> Task:
        """Decompose the task into subtasks via the Planner."""
        brief = self._context_brief
        try:
            if self._smart_planner:
                subtasks, complexity = await self._smart_planner.decompose(run.task, context_brief=brief)
                run.complexity = complexity
            else:
                subtasks = await self._planner.decompose(run.task)
        except Exception as exc:
            logger.error("Planning failed: %s", exc)
            run.state = TaskState.failed
            return run

        if not subtasks:
            logger.error("Planner returned no subtasks")
            run.state = TaskState.failed
            return run

        # Convert description-based deps to ID-based deps
        subtasks = self._resolve_deps(subtasks)

        run.subtasks = subtasks
        # Persist initial subtasks
        for subtask in subtasks:
            self._persist_subtask(subtask, run.id)
        run.state = TaskState.executing
        self._persist_run(run)
        return run

    async def _execute(self, run: Task) -> Task:
        """Dispatch subtasks to agents and poll until all complete or fail."""
        agent_map: dict[str, str] = {}  # subtask_id → agent_id
        active_count = 0

        # Rebuild agent_map for subtasks already running (e.g. after gate-retry)
        for subtask in run.subtasks:
            if subtask.state == SubtaskState.running and subtask.agent:
                agent_map[subtask.id] = subtask.agent
                active_count += 1

        while True:
            # Dispatch ready subtasks (deps met, not yet running)
            for subtask in run.subtasks:
                if subtask.state != SubtaskState.pending:
                    continue
                max_concurrent = self._concurrency_limits.get(TaskState.executing, self._max_agents)
                if active_count >= max_concurrent:
                    break
                if not self._deps_met(run, subtask):
                    continue

                try:
                    dispatch_span = self._subtask_span(
                        "subtask.dispatch",
                        subtask,
                        deps=subtask.deps,
                        files_hint=subtask.files_hint,
                    )
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
                    await self._pool.send_task(
                        slot.id,
                        subtask.description,
                        task_id=subtask.id,
                        run_id=run.id,
                        subtask_description=subtask.description,
                    )
                    subtask.state = SubtaskState.running
                    subtask.agent = slot.id
                    subtask.last_activity_at = datetime.now(UTC)
                    agent_map[subtask.id] = slot.id
                    active_count += 1
                    set_log_context(subtask_id=subtask.id[:8], agent_id=slot.name)
                    self._persist_subtask(subtask, run.id)
                    if dispatch_span:
                        self._tracer.end_span(
                            dispatch_span,
                            {"state": subtask.state.value},
                            metadata={"selected_agent": slot.id, "runtime": slot.runtime, "model": slot.model},
                        )
                except Exception as exc:
                    logger.error("Failed to dispatch subtask %s: %s", subtask.id, exc)
                    subtask.state = SubtaskState.failed
                    self._persist_subtask(subtask, run.id)
                    if self._tracer and dispatch_span:
                        self._tracer.end_span(
                            dispatch_span,
                            {"error": str(exc)},
                            metadata={"state": subtask.state.value},
                            level="ERROR",
                            status_message=str(exc),
                        )

            # Check if all subtasks are terminal
            if all(s.state in (SubtaskState.done, SubtaskState.failed) for s in run.subtasks):
                break

            # If nothing is running and nothing can be dispatched, we're stuck
            running = [s for s in run.subtasks if s.state == SubtaskState.running]
            if not running and not any(
                s.state == SubtaskState.pending and self._deps_met(run, s) for s in run.subtasks
            ):
                logger.error("No subtasks running and none can be dispatched — deadlock")
                run.state = TaskState.failed
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
                    collect_span = self._subtask_span("subtask.collect_result", subtask, status=status.value)
                    result = await self._pool.collect_result(agent_id)
                    subtask.result = result
                    self._stamp_provenance(result, run, agent_id)
                    subtask.state = SubtaskState.done if result.success else SubtaskState.failed
                    self._persist_subtask(subtask, run.id)
                    active_count -= 1
                    if collect_span:
                        self._tracer.end_span(
                            collect_span,
                            {"success": result.success, "has_diff": bool(result.diff)},
                            metadata={"duration_seconds": result.duration_seconds},
                            level="DEFAULT" if result.success else "WARNING",
                        )
                    continue

                # Check heartbeat — update last_activity_at if agent is producing output
                try:
                    heartbeat = await self._pool.check_heartbeat(agent_id)
                    if heartbeat:
                        subtask.last_activity_at = datetime.now(UTC)
                except Exception:
                    pass

                # Check for new commits in worktree (primary completion signal)
                try:
                    collect_span = self._subtask_span("subtask.poll_result", subtask, status=status.value)
                    result = await self._pool.collect_result(agent_id)
                    if result.diff:
                        subtask.result = result
                        self._stamp_provenance(result, run, agent_id)
                        subtask.state = SubtaskState.done
                        subtask.last_activity_at = datetime.now(UTC)
                        self._persist_subtask(subtask, run.id)
                        active_count -= 1
                        if collect_span:
                            self._tracer.end_span(
                                collect_span,
                                {"success": True, "has_diff": True},
                                metadata={"duration_seconds": result.duration_seconds},
                            )
                    elif collect_span:
                        self._tracer.end_span(collect_span, {"success": False, "has_diff": False})
                except Exception:
                    pass

            # Check for stalled agents and retry
            retried_count = await self._check_stalls(run, agent_map)
            active_count -= retried_count

        # Any failures?
        failed = [s for s in run.subtasks if s.state == SubtaskState.failed]
        succeeded = [s for s in run.subtasks if s.state == SubtaskState.done]
        if failed:
            if self._allow_partial_success and succeeded:
                logger.warning(
                    "Partial success: %d/%d subtasks failed, continuing with %d successful",
                    len(failed),
                    len(run.subtasks),
                    len(succeeded),
                )
            else:
                run.state = TaskState.failed
                return run

        run.state = TaskState.reviewing
        return run

    async def _review(self, run: Task) -> Task:
        """Run validation gates on each completed subtask's worktree.

        If gates fail and the agent is alive with retries remaining,
        send fix feedback and return to executing state.
        """
        all_passed = True
        needs_re_execute = False
        reviewed_subtasks = 0
        passed_subtasks = 0
        failed_subtasks = 0
        retried_subtasks = 0

        for subtask in run.subtasks:
            if subtask.state != SubtaskState.done or not subtask.agent:
                continue
            reviewed_subtasks += 1
            set_log_context(subtask_id=subtask.id[:8])

            review_span = self._subtask_span("subtask.review", subtask)
            gate_retry_span = None
            try:
                slot = self._pool._get_slot(subtask.agent)
                if not slot.worktree_path:
                    if review_span:
                        self._tracer.end_span(
                            review_span,
                            {"skipped": True},
                            metadata={"reason": "missing worktree_path"},
                            level="WARNING",
                        )
                    continue

                # Auto-fix lint before running gates
                fix_result = await self._gates.auto_fix_and_commit(slot.worktree_path)
                if not fix_result.passed:
                    logger.warning("Auto-fix failed for subtask %s: %s", subtask.id, fix_result.output)

                results = await self._gates.run_all(slot.worktree_path)
                if self._gates.all_passed(results):
                    passed_subtasks += 1
                    if review_span:
                        self._tracer.end_span(
                            review_span,
                            {"passed": True},
                            metadata={"gate_count": len(results)},
                        )
                    continue

                # Gates failed — try retry
                gate_output = "; ".join(f"{r.gate}: {r.output}" for r in results if not r.passed)
                logger.warning("Subtask %s failed gates: %s", subtask.id, gate_output)
                gate_retry_span = self._subtask_span(
                    "subtask.gate_retry",
                    subtask,
                    gate_output=gate_output,
                )

                if subtask.gate_retry_count < subtask.max_gate_retries:
                    # Check agent is still alive; respawn if dead
                    agent_status = await self._pool.check_status(subtask.agent)
                    respawned = False
                    if agent_status == AgentState.dead:
                        try:
                            logger.info("Respawning dead agent for subtask %s gate retry", subtask.id)
                            await self._pool.respawn(subtask.agent)
                            respawned = True
                        except Exception as exc:
                            logger.error("Failed to respawn agent for subtask %s: %s", subtask.id, exc)
                            subtask.state = SubtaskState.failed
                            self._persist_subtask(subtask, run.id)
                            all_passed = False
                            if review_span:
                                self._tracer.end_span(
                                    review_span,
                                    {"error": str(exc)},
                                    metadata={"passed": False},
                                    level="ERROR",
                                    status_message=str(exc),
                                )
                            if self._tracer and gate_retry_span:
                                self._tracer.end_span(
                                    gate_retry_span,
                                    {"error": str(exc)},
                                    metadata={"respawned": False},
                                    level="ERROR",
                                    status_message=str(exc),
                                )
                            continue

                    await self._pool.send_task(
                        subtask.agent,
                        gate_output,
                        task_id=subtask.id,
                        prompt_kind="fix",
                        run_id=run.id,
                        subtask_description=subtask.description,
                    )
                    subtask.state = SubtaskState.running
                    subtask.gate_retry_count += 1
                    self._gate_retry_events += 1
                    subtask.last_activity_at = datetime.now(UTC)
                    self._persist_subtask(subtask, run.id)
                    needs_re_execute = True
                    retried_subtasks += 1
                    if review_span:
                        self._tracer.end_span(
                            review_span,
                            {"passed": False, "retrying": True},
                            metadata={"gate_output": gate_output},
                            level="WARNING",
                        )
                    if self._tracer and gate_retry_span:
                        self._tracer.end_span(
                            gate_retry_span,
                            {"action": "retry"},
                            metadata={
                                "gate_retry_count": subtask.gate_retry_count,
                                "max_gate_retries": subtask.max_gate_retries,
                                "respawned": respawned,
                            },
                            level="WARNING",
                        )
                    logger.info(
                        "Sent fix prompt to agent for subtask %s (gate retry %d/%d)",
                        subtask.id,
                        subtask.gate_retry_count,
                        subtask.max_gate_retries,
                    )
                    continue

                # No retries left
                self._gate_retry_exhausted += 1
                subtask.state = SubtaskState.failed
                self._persist_subtask(subtask, run.id)
                all_passed = False
                failed_subtasks += 1
                if review_span:
                    self._tracer.end_span(
                        review_span,
                        {"passed": False, "retrying": False},
                        metadata={"gate_output": gate_output},
                        level="ERROR",
                    )
                if self._tracer and gate_retry_span:
                    self._tracer.end_span(
                        gate_retry_span,
                        {"action": "failed"},
                        metadata={
                            "gate_retry_count": subtask.gate_retry_count,
                            "max_gate_retries": subtask.max_gate_retries,
                        },
                        level="ERROR",
                        status_message="gate retry exhausted",
                    )

            except KeyError as exc:
                logger.error("Review failed for subtask %s — agent slot not found: %s", subtask.id, exc)
                subtask.state = SubtaskState.failed
                self._persist_subtask(subtask, run.id)
                all_passed = False
                if self._tracer and review_span:
                    self._tracer.end_span(
                        review_span,
                        {"error": str(exc)},
                        metadata={"passed": False},
                        level="ERROR",
                        status_message=str(exc),
                    )
                if self._tracer and gate_retry_span:
                    self._tracer.end_span(
                        gate_retry_span,
                        {"error": str(exc)},
                        level="ERROR",
                        status_message=str(exc),
                    )
            except Exception as exc:
                logger.error("Review failed for subtask %s: %s", subtask.id, exc, exc_info=True)
                subtask.state = SubtaskState.failed
                self._persist_subtask(subtask, run.id)
                all_passed = False
                failed_subtasks += 1
                if self._tracer and gate_retry_span:
                    self._tracer.end_span(
                        gate_retry_span,
                        {"error": str(exc)},
                        level="ERROR",
                        status_message=str(exc),
                    )
                if self._tracer and review_span:
                    self._tracer.end_span(
                        review_span,
                        {"error": str(exc)},
                        metadata={"passed": False},
                        level="ERROR",
                        status_message=str(exc),
                    )

        if self._tracer and self._active_trace and reviewed_subtasks:
            self._tracer.score_trace(
                self._active_trace,
                "review_gate_pass_rate",
                passed_subtasks / reviewed_subtasks,
                data_type="NUMERIC",
                metadata={
                    "reviewed_subtasks": reviewed_subtasks,
                    "passed_subtasks": passed_subtasks,
                    "failed_subtasks": failed_subtasks,
                    "retried_subtasks": retried_subtasks,
                },
            )
            self._tracer.score_trace(
                self._active_trace,
                "review_status",
                "retry" if needs_re_execute else ("pass" if all_passed else "fail"),
                data_type="CATEGORICAL",
            )

        if needs_re_execute:
            run.state = TaskState.executing
            return run

        if all_passed:
            run.state = TaskState.merging
        elif self._allow_partial_success and passed_subtasks > 0:
            logger.warning(
                "Partial success review: %d passed, %d failed gates — continuing to merge",
                passed_subtasks,
                failed_subtasks,
            )
            run.state = TaskState.merging
        else:
            run.state = TaskState.failed
        return run

    async def _merge(self, run: Task) -> Task:
        """Merge each subtask's worktree branch into main."""
        if self._merge_queue:
            # Use merge queue: enqueue all subtasks, then process
            for subtask in run.subtasks:
                if subtask.state != SubtaskState.done or not subtask.agent:
                    continue
                set_log_context(subtask_id=subtask.id[:8])
                slot = self._pool._get_slot(subtask.agent)
                merge_span = self._subtask_span("subtask.merge_queue", subtask, branch=slot.branch)
                await self._merge_queue.enqueue(subtask.id, slot.name, slot.branch)
                if merge_span:
                    self._tracer.end_span(merge_span, {"enqueued": True}, metadata={"agent_name": slot.name})

            results = await self._merge_queue.process()
            merge_failures = 0
            for result in results:
                if not result.success:
                    logger.error("Merge conflict for subtask %s", result.subtask_id)
                    self._record_merge_conflict(
                        result.subtask_id,
                        branch=result.branch,
                        conflict_files=result.conflict_files,
                    )
                    merge_failures += 1
                    if not self._allow_partial_success:
                        run.state = TaskState.failed
                        return run
            if merge_failures and not self._allow_partial_success:
                run.state = TaskState.failed
                return run
        else:
            # Fallback: direct merge without queue
            for subtask in run.subtasks:
                if subtask.state != SubtaskState.done or not subtask.agent:
                    continue
                set_log_context(subtask_id=subtask.id[:8])

                merge_span = None
                try:
                    slot = self._pool._get_slot(subtask.agent)
                    merge_span = self._subtask_span("subtask.merge", subtask, branch=slot.branch)
                    success, conflict_files = await self._pool._worktrees.merge(slot.name)
                    if not success:
                        logger.error("Merge conflict for subtask %s", subtask.id)
                        self._record_merge_conflict(subtask.id, branch=slot.branch, conflict_files=conflict_files)
                        subtask.state = SubtaskState.failed
                        if not self._allow_partial_success:
                            run.state = TaskState.failed
                            if merge_span:
                                self._tracer.end_span(
                                    merge_span,
                                    {"success": False},
                                    level="ERROR",
                                    status_message="merge conflict",
                                )
                            return run
                        if merge_span:
                            self._tracer.end_span(
                                merge_span,
                                {"success": False, "partial_success": True},
                                level="WARNING",
                                status_message="merge conflict (partial success mode)",
                            )
                    if merge_span:
                        self._tracer.end_span(merge_span, {"success": True})
                except Exception as exc:
                    logger.error("Merge failed for subtask %s: %s", subtask.id, exc)
                    subtask.state = SubtaskState.failed
                    if not self._allow_partial_success:
                        run.state = TaskState.failed
                        if self._tracer and merge_span:
                            self._tracer.end_span(
                                merge_span,
                                {"error": str(exc)},
                                level="ERROR",
                                status_message=str(exc),
                            )
                        return run

        has_failures = any(s.state == SubtaskState.failed for s in run.subtasks)
        run.state = TaskState.partial_success if (has_failures and self._allow_partial_success) else TaskState.completed
        return run

    @staticmethod
    def _deps_met(run: Task, subtask) -> bool:
        """Check if all dependencies of a subtask are done."""
        if not subtask.deps:
            return True
        done_ids = {s.id for s in run.subtasks if s.state == SubtaskState.done}
        return all(dep in done_ids for dep in subtask.deps)

    @staticmethod
    def _resolve_deps(subtasks: list[Subtask]) -> list[Subtask]:
        """Convert description-based deps to ID-based deps.

        Builds a mapping from description to ID, then replaces each dep
        with the corresponding ID if found. Unknown deps are kept as-is.
        """
        desc_to_id: dict[str, str] = {s.description: s.id for s in subtasks}
        for subtask in subtasks:
            if subtask.deps:
                subtask.deps = [desc_to_id.get(dep, dep) for dep in subtask.deps]
        return subtasks


_Handler = type(Orchestrator._plan)  # just for type alias readability
