"""Agent pool — wires together TmuxManager, WorktreeManager, RUNTIME_REGISTRY, and Store."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime

from horse_fish.agents.prompt import resolve_fix_prompt, resolve_task_prompt
from horse_fish.agents.runtime import RUNTIME_REGISTRY
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.models import AgentSlot, AgentState, SubtaskResult
from horse_fish.observability.traces import Tracer
from horse_fish.store.db import Store


class AgentPool:
    """Manages the full lifecycle of agent slots: spawn, task, collect, release."""

    def __init__(
        self,
        store: Store,
        tmux: TmuxManager,
        worktrees: WorktreeManager,
        project_context: str | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._store = store
        self._tmux = tmux
        self._worktrees = worktrees
        self._project_context = project_context
        self._tracer = tracer

    def _trace_span(self, name: str, **metadata):
        """Create a best-effort span for agent lifecycle operations."""
        if not self._tracer:
            return None
        return self._tracer.span(None, name, metadata)

    async def spawn(self, name: str, runtime: str, model: str, capability: str) -> AgentSlot:
        """Create a worktree, start a tmux session, persist the slot, and return it."""
        if runtime not in RUNTIME_REGISTRY:
            raise ValueError(f"unknown runtime {runtime!r}; available: {sorted(RUNTIME_REGISTRY)}")

        spawn_span = self._trace_span(
            "agent.spawn",
            agent_name=name,
            runtime=runtime,
            model=model,
            capability=capability,
        )
        adapter = RUNTIME_REGISTRY[runtime]
        command = adapter.build_spawn_command(model)
        env = adapter.build_env() or None

        try:
            worktree = await self._worktrees.create(name)
            tmux_session = f"hf-{name}"

            pid = await self._tmux.spawn(name=tmux_session, command=command, cwd=worktree.path, env=env)

            slot = AgentSlot(
                id=str(uuid.uuid4()),
                name=name,
                runtime=runtime,
                model=model,
                capability=capability,
                state=AgentState.idle,
                pid=pid,
                tmux_session=tmux_session,
                worktree_path=worktree.path,
                branch=worktree.branch,
                started_at=datetime.now(UTC),
            )

            # Wait for the runtime to show its ready prompt before proceeding
            await self._wait_for_ready(slot)

            self._store.execute(
                """
                INSERT INTO agents
                    (id, name, runtime, model, capability, state, pid,
                     tmux_session, worktree_path, branch, task_id, started_at, idle_since)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    slot.id,
                    slot.name,
                    slot.runtime,
                    slot.model,
                    slot.capability,
                    slot.state,
                    slot.pid,
                    slot.tmux_session,
                    slot.worktree_path,
                    slot.branch,
                    slot.task_id,
                    slot.started_at.isoformat() if slot.started_at else None,
                    slot.idle_since.isoformat() if slot.idle_since else None,
                ),
            )
            if self._tracer and spawn_span:
                self._tracer.end_span(
                    spawn_span,
                    {"spawned": True},
                    metadata={
                        "agent_id": slot.id,
                        "tmux_session": slot.tmux_session,
                        "worktree_path": slot.worktree_path,
                        "branch": slot.branch,
                    },
                )

            return slot
        except Exception as exc:
            if self._tracer and spawn_span:
                self._tracer.end_span(
                    spawn_span,
                    {"spawned": False, "error": str(exc)},
                    level="ERROR",
                    status_message=str(exc),
                )
            raise

    async def send_task(
        self,
        agent_id: str,
        prompt: str,
        task_id: str | None = None,
        raw: bool = False,
        prompt_kind: str = "task",
    ) -> None:
        """Send a prompt to the agent's tmux session and mark it busy."""
        slot = self._get_slot(agent_id)
        generation = None
        if raw:
            full_prompt = prompt
            prompt_name = "raw"
            prompt_source = "raw"
            prompt_version = None
            if self._tracer:
                generation = self._tracer.generation(
                    None,
                    "agent.raw_prompt",
                    input={"prompt": prompt},
                    metadata={
                        "agent_id": slot.id,
                        "runtime": slot.runtime,
                        "model": slot.model,
                        "task_id": task_id,
                        "prompt_name": prompt_name,
                        "prompt_source": prompt_source,
                        "prompt_version": prompt_version,
                    },
                    model=slot.model,
                    model_parameters={"runtime": slot.runtime},
                )
        else:
            if prompt_kind == "fix":
                resolved_prompt = resolve_fix_prompt(
                    self._tracer,
                    gate_output=prompt,
                    worktree_path=slot.worktree_path or "",
                    branch=slot.branch or "",
                )
                generation_name = "agent.fix_prompt"
                generation_input = {
                    "gate_output": prompt,
                    "worktree_path": slot.worktree_path or "",
                    "branch": slot.branch or "",
                }
            else:
                resolved_prompt = resolve_task_prompt(
                    self._tracer,
                    task=prompt,
                    worktree_path=slot.worktree_path or "",
                    branch=slot.branch or "",
                    project_context=self._project_context,
                )
                generation_name = "agent.task_prompt"
                generation_input = {
                    "task": prompt,
                    "worktree_path": slot.worktree_path or "",
                    "branch": slot.branch or "",
                    "project_context": bool(self._project_context),
                }
            full_prompt = resolved_prompt.compiled
            prompt_name = resolved_prompt.name
            prompt_source = resolved_prompt.source
            prompt_version = resolved_prompt.version
            if self._tracer:
                generation = self._tracer.generation(
                    None,
                    generation_name,
                    input=generation_input,
                    metadata={
                        "agent_id": slot.id,
                        "runtime": slot.runtime,
                        "model": slot.model,
                        "task_id": task_id,
                        "prompt_name": prompt_name,
                        "prompt_source": prompt_source,
                        "prompt_version": prompt_version,
                    },
                    model=slot.model,
                    model_parameters={"runtime": slot.runtime},
                    prompt=resolved_prompt.prompt_client,
                )
        # Claude Code needs a longer delay between paste and Enter for large prompts
        enter_delay = 0.5 if slot.runtime == "claude" else 0.1
        await self._tmux.send_keys(slot.tmux_session, full_prompt, enter_delay=enter_delay)
        self._store.execute(
            "UPDATE agents SET state = ?, task_id = ? WHERE id = ?",
            (AgentState.busy, task_id, agent_id),
        )
        if self._tracer and generation:
            self._tracer.end_span(
                generation,
                {"prompt_length": len(full_prompt)},
                metadata={
                    "agent_id": slot.id,
                    "runtime": slot.runtime,
                    "model": slot.model,
                    "task_id": task_id,
                    "prompt_name": prompt_name,
                    "prompt_source": prompt_source,
                    "prompt_version": prompt_version,
                },
            )

    async def check_status(self, agent_id: str) -> AgentState:
        """Return the agent's current state; mark dead if its tmux session is gone."""
        slot = self._get_slot(agent_id)
        status_span = self._trace_span(
            "agent.check_status",
            agent_id=slot.id,
            agent_name=slot.name,
            runtime=slot.runtime,
            model=slot.model,
            prior_state=slot.state.value,
            task_id=slot.task_id,
        )
        try:
            alive = await self._tmux.is_alive(slot.tmux_session)
            next_state = AgentState.dead if (not alive and slot.state != AgentState.dead) else slot.state
            if next_state == AgentState.dead and slot.state != AgentState.dead:
                self._store.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.dead, agent_id))
            if self._tracer and status_span:
                self._tracer.end_span(
                    status_span,
                    {"alive": alive, "state": next_state.value},
                    metadata={"tmux_session": slot.tmux_session},
                    level="WARNING" if not alive else None,
                )
            return next_state
        except Exception as exc:
            if self._tracer and status_span:
                self._tracer.end_span(
                    status_span,
                    {"error": str(exc)},
                    level="ERROR",
                    status_message=str(exc),
                )
            raise

    async def respawn(self, agent_id: str) -> AgentSlot:
        """Re-spawn a dead agent in its existing worktree with a fresh tmux session.

        Used by gate-retry when the original agent exited but the worktree
        still contains fixable code.
        """
        slot = self._get_slot(agent_id)
        if not slot.worktree_path:
            raise ValueError(f"Agent {agent_id} has no worktree path for respawn")

        respawn_span = self._trace_span(
            "agent.respawn",
            agent_id=slot.id,
            agent_name=slot.name,
            runtime=slot.runtime,
            model=slot.model,
        )
        adapter = RUNTIME_REGISTRY[slot.runtime]
        command = adapter.build_spawn_command(slot.model)
        env = adapter.build_env() or None

        try:
            # Kill old session if it somehow lingers
            await self._tmux.kill_session(slot.tmux_session)

            pid = await self._tmux.spawn(name=slot.tmux_session, command=command, cwd=slot.worktree_path, env=env)

            # Update in-memory slot
            slot.state = AgentState.idle
            slot.pid = pid
            slot.started_at = datetime.now(UTC)

            # Wait for ready prompt
            await self._wait_for_ready(slot)

            self._store.execute(
                "UPDATE agents SET state = ?, pid = ?, started_at = ? WHERE id = ?",
                (AgentState.idle, pid, slot.started_at.isoformat(), agent_id),
            )
            if self._tracer and respawn_span:
                self._tracer.end_span(
                    respawn_span,
                    {"respawned": True},
                    metadata={"pid": pid, "tmux_session": slot.tmux_session},
                )
            return slot
        except Exception as exc:
            if self._tracer and respawn_span:
                self._tracer.end_span(
                    respawn_span,
                    {"respawned": False, "error": str(exc)},
                    level="ERROR",
                    status_message=str(exc),
                )
            raise

    async def collect_result(self, agent_id: str) -> SubtaskResult:
        """Capture pane output and worktree diff; return a SubtaskResult."""
        slot = self._get_slot(agent_id)
        started_at = slot.started_at or datetime.now(UTC)
        result_span = self._trace_span(
            "agent.collect_result",
            agent_id=slot.id,
            agent_name=slot.name,
            runtime=slot.runtime,
            model=slot.model,
            task_id=slot.task_id,
        )

        try:
            output = await self._tmux.capture_pane(slot.tmux_session) or ""
            diff = await self._worktrees.get_diff(slot.name)
            duration = (datetime.now(UTC) - started_at).total_seconds()
            result = SubtaskResult(
                subtask_id=slot.task_id or agent_id,
                success=bool(output),
                output=output,
                diff=diff,
                duration_seconds=duration,
            )
            if self._tracer and result_span:
                self._tracer.end_span(
                    result_span,
                    {
                        "success": result.success,
                        "has_diff": bool(result.diff),
                        "has_output": bool(result.output),
                    },
                    metadata={
                        "duration_seconds": result.duration_seconds,
                        "output_chars": len(result.output),
                        "diff_chars": len(result.diff),
                        "subtask_id": result.subtask_id,
                    },
                    level="WARNING" if not result.output and not result.diff else None,
                )
            return result
        except Exception as exc:
            if self._tracer and result_span:
                self._tracer.end_span(
                    result_span,
                    {"error": str(exc)},
                    level="ERROR",
                    status_message=str(exc),
                )
            raise

    async def release(self, agent_id: str) -> None:
        """Kill the tmux session, remove the worktree, and mark the slot dead."""
        slot = self._get_slot(agent_id)
        await self._tmux.kill_session(slot.tmux_session)
        await self._worktrees.remove(slot.name)
        self._store.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.dead, agent_id))

    def list_agents(self) -> list[AgentSlot]:
        """Return all persisted agent slots."""
        rows = self._store.fetchall("SELECT * FROM agents")
        return [_row_to_slot(row) for row in rows]

    async def cleanup(self) -> int:
        """Release all dead, idle, or busy agents; prune stale worktrees. Returns count released."""
        agents = self.list_agents()
        released = 0
        for slot in agents:
            if slot.state in (AgentState.dead, AgentState.idle, AgentState.busy):
                try:
                    await self.release(slot.id)
                    released += 1
                except Exception:
                    pass
        await self._worktrees.cleanup()
        return released

    async def _wait_for_ready(self, slot: AgentSlot) -> None:
        """Wait for the agent runtime to show its ready prompt."""
        adapter = RUNTIME_REGISTRY[slot.runtime]
        pattern = re.compile(adapter.ready_pattern, re.MULTILINE)
        timeout = adapter.ready_timeout_seconds
        elapsed = 0.0
        poll_count = 0
        post_ready_command_count = 0

        dismiss_compiled = [(re.compile(p, re.MULTILINE), key) for p, key in adapter.dismiss_patterns]
        dismissed: set[str] = set()
        ready_span = self._trace_span(
            "agent.wait_for_ready",
            agent_id=slot.id,
            agent_name=slot.name,
            runtime=slot.runtime,
            model=slot.model,
            timeout_seconds=timeout,
        )

        while elapsed < timeout:
            poll_count += 1
            output = await self._tmux.capture_pane(slot.tmux_session)
            if output:
                # Check for dialogs that need dismissing
                dialog_found = False
                for dismiss_re, key in dismiss_compiled:
                    pat_str = dismiss_re.pattern
                    if pat_str not in dismissed and dismiss_re.search(output):
                        await self._tmux.send_raw_key(slot.tmux_session, key)
                        dismissed.add(pat_str)
                        await asyncio.sleep(1.0)
                        dialog_found = True
                        break
                if not dialog_found and pattern.search(output):
                    # Send post-ready commands (e.g. model selection for droid)
                    for cmd in adapter.post_ready_commands(slot.model):
                        await self._tmux.send_keys(slot.tmux_session, cmd)
                        await asyncio.sleep(2.0)
                        post_ready_command_count += 1
                    if self._tracer and ready_span:
                        self._tracer.end_span(
                            ready_span,
                            {"ready": True},
                            metadata={
                                "elapsed_seconds": elapsed,
                                "poll_count": poll_count,
                                "dismissed_dialog_count": len(dismissed),
                                "post_ready_command_count": post_ready_command_count,
                            },
                        )
                    return
            await asyncio.sleep(1.0)
            elapsed += 1.0

        # Timeout: kill session and remove worktree
        await self._tmux.kill_session(slot.tmux_session)
        await self._worktrees.remove(slot.name)
        if self._tracer and ready_span:
            self._tracer.end_span(
                ready_span,
                {"ready": False},
                metadata={
                    "elapsed_seconds": elapsed,
                    "poll_count": poll_count,
                    "dismissed_dialog_count": len(dismissed),
                },
                level="ERROR",
                status_message="agent readiness timeout",
            )
        raise RuntimeError(f"Agent {slot.name!r} (runtime={slot.runtime}) did not become ready within {timeout}s")

    def _get_slot(self, agent_id: str) -> AgentSlot:
        row = self._store.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
        if row is None:
            raise KeyError(f"agent {agent_id!r} not found")
        return _row_to_slot(row)


def _row_to_slot(row: dict) -> AgentSlot:
    return AgentSlot(
        id=row["id"],
        name=row["name"],
        runtime=row["runtime"],
        model=row["model"],
        capability=row["capability"],
        state=AgentState(row["state"]),
        pid=row["pid"],
        tmux_session=row["tmux_session"],
        worktree_path=row["worktree_path"],
        branch=row["branch"],
        task_id=row["task_id"],
        started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
        idle_since=datetime.fromisoformat(row["idle_since"]) if row["idle_since"] else None,
    )
