"""Agent pool — wires together TmuxManager, WorktreeManager, RUNTIME_REGISTRY, and Store."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import UTC, datetime

from horse_fish.agents.prompt import build_prompt
from horse_fish.agents.runtime import RUNTIME_REGISTRY
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.models import AgentSlot, AgentState, SubtaskResult
from horse_fish.store.db import Store


class AgentPool:
    """Manages the full lifecycle of agent slots: spawn, task, collect, release."""

    def __init__(
        self,
        store: Store,
        tmux: TmuxManager,
        worktrees: WorktreeManager,
        project_context: str | None = None,
    ) -> None:
        self._store = store
        self._tmux = tmux
        self._worktrees = worktrees
        self._project_context = project_context

    async def spawn(self, name: str, runtime: str, model: str, capability: str) -> AgentSlot:
        """Create a worktree, start a tmux session, persist the slot, and return it."""
        if runtime not in RUNTIME_REGISTRY:
            raise ValueError(f"unknown runtime {runtime!r}; available: {sorted(RUNTIME_REGISTRY)}")

        adapter = RUNTIME_REGISTRY[runtime]
        command = adapter.build_spawn_command(model)
        env = adapter.build_env() or None

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

        return slot

    async def send_task(self, agent_id: str, prompt: str, task_id: str | None = None, raw: bool = False) -> None:
        """Send a prompt to the agent's tmux session and mark it busy."""
        slot = self._get_slot(agent_id)
        if raw:
            full_prompt = prompt
        else:
            full_prompt = build_prompt(
                task=prompt,
                worktree_path=slot.worktree_path or "",
                branch=slot.branch or "",
                project_context=self._project_context,
            )
        # Claude Code needs a longer delay between paste and Enter for large prompts
        enter_delay = 0.5 if slot.runtime == "claude" else 0.1
        await self._tmux.send_keys(slot.tmux_session, full_prompt, enter_delay=enter_delay)
        self._store.execute(
            "UPDATE agents SET state = ?, task_id = ? WHERE id = ?",
            (AgentState.busy, task_id, agent_id),
        )

    async def check_status(self, agent_id: str) -> AgentState:
        """Return the agent's current state; mark dead if its tmux session is gone."""
        slot = self._get_slot(agent_id)
        alive = await self._tmux.is_alive(slot.tmux_session)
        if not alive and slot.state != AgentState.dead:
            self._store.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.dead, agent_id))
            return AgentState.dead
        return slot.state

    async def respawn(self, agent_id: str) -> AgentSlot:
        """Re-spawn a dead agent in its existing worktree with a fresh tmux session.

        Used by gate-retry when the original agent exited but the worktree
        still contains fixable code.
        """
        slot = self._get_slot(agent_id)
        if not slot.worktree_path:
            raise ValueError(f"Agent {agent_id} has no worktree path for respawn")

        adapter = RUNTIME_REGISTRY[slot.runtime]
        command = adapter.build_spawn_command(slot.model)
        env = adapter.build_env() or None

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

        return slot

    async def collect_result(self, agent_id: str) -> SubtaskResult:
        """Capture pane output and worktree diff; return a SubtaskResult."""
        slot = self._get_slot(agent_id)
        started_at = slot.started_at or datetime.now(UTC)

        output = await self._tmux.capture_pane(slot.tmux_session) or ""
        diff = await self._worktrees.get_diff(slot.name)
        duration = (datetime.now(UTC) - started_at).total_seconds()

        return SubtaskResult(
            subtask_id=slot.task_id or agent_id,
            success=bool(output),
            output=output,
            diff=diff,
            duration_seconds=duration,
        )

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

        dismiss_compiled = [(re.compile(p, re.MULTILINE), key) for p, key in adapter.dismiss_patterns]
        dismissed: set[str] = set()

        while elapsed < timeout:
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
                    return
            await asyncio.sleep(1.0)
            elapsed += 1.0

        # Timeout: kill session and remove worktree
        await self._tmux.kill_session(slot.tmux_session)
        await self._worktrees.remove(slot.name)
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
