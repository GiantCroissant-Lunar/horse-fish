"""Agent pool — wires together TmuxManager, WorktreeManager, RUNTIME_REGISTRY, and Store."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from horse_fish.agents.runtime import RUNTIME_REGISTRY
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.models import AgentSlot, AgentState, SubtaskResult
from horse_fish.store.db import Store


class AgentPool:
    """Manages the full lifecycle of agent slots: spawn, task, collect, release."""

    def __init__(self, store: Store, tmux: TmuxManager, worktrees: WorktreeManager) -> None:
        self._store = store
        self._tmux = tmux
        self._worktrees = worktrees

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

    async def send_task(self, agent_id: str, prompt: str) -> None:
        """Send a prompt to the agent's tmux session and mark it busy."""
        slot = self._get_slot(agent_id)
        await self._tmux.send_keys(slot.tmux_session, prompt)
        self._store.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.busy, agent_id))

    async def check_status(self, agent_id: str) -> AgentState:
        """Return the agent's current state; mark dead if its tmux session is gone."""
        slot = self._get_slot(agent_id)
        alive = await self._tmux.is_alive(slot.tmux_session)
        if not alive and slot.state != AgentState.dead:
            self._store.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.dead, agent_id))
            return AgentState.dead
        return slot.state

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
        """Release all dead or idle agents; prune stale worktrees. Returns count released."""
        agents = self.list_agents()
        released = 0
        for slot in agents:
            if slot.state in (AgentState.dead, AgentState.idle):
                try:
                    await self.release(slot.id)
                    released += 1
                except Exception:
                    pass
        await self._worktrees.cleanup()
        return released

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
