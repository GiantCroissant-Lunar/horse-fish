# Orchestrator + CLI Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement the orchestrator state machine and CLI to drive the agent swarm end-to-end.

**Architecture:** Simple async state machine (dict of RunState → handler). CLI uses Click, delegates to Orchestrator. Three independent tasks for parallel agent execution.

**Tech Stack:** Python 3.12+, asyncio, Click, pytest-asyncio, Pydantic

---

### Task 1: Orchestrator Engine

**Files:**
- Create: `src/horse_fish/orchestrator/__init__.py`
- Create: `src/horse_fish/orchestrator/engine.py`
- Create: `tests/test_orchestrator.py`

**Implementation:**

```python
# src/horse_fish/orchestrator/__init__.py
from horse_fish.orchestrator.engine import Orchestrator

__all__ = ["Orchestrator"]
```

```python
# src/horse_fish/orchestrator/engine.py
"""Orchestrator state machine: plan → execute → review → merge → learn."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from horse_fish.agents.pool import AgentPool
from horse_fish.models import Run, RunState, SubtaskState
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
    ) -> None:
        self._pool = pool
        self._planner = planner
        self._gates = gates
        self._runtime = runtime
        self._model = model or ""
        self._max_agents = max_agents

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

        while run.state not in (RunState.completed, RunState.failed):
            handler = self._handlers.get(run.state)
            if handler is None:
                raise OrchestratorError(f"No handler for state {run.state}")
            run = await handler(run)
            logger.info("Run %s transitioned to %s", run.id, run.state)

        run.completed_at = datetime.now(UTC)
        return run

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
                if active_count >= self._max_agents:
                    break
                if not self._deps_met(run, subtask):
                    continue

                try:
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
                    subtask.state = SubtaskState.done if result.success else SubtaskState.failed
                    active_count -= 1
                    continue

                # Check for new commits in worktree (primary completion signal)
                try:
                    result = await self._pool.collect_result(agent_id)
                    if result.diff:
                        subtask.result = result
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
```

**Tests** (`tests/test_orchestrator.py`):

Mock AgentPool, Planner, and ValidationGates. Test:
- `_plan` success → state becomes executing with subtasks populated
- `_plan` failure → state becomes failed
- `_plan` empty subtasks → state becomes failed
- `_execute` dispatches subtasks and polls for completion
- `_execute` respects DAG deps (blocked subtasks wait)
- `_execute` handles agent spawn failure
- `_execute` deadlock detection
- `_review` all gates pass → state becomes merging
- `_review` gate failure → state becomes failed
- `_merge` success → state becomes completed
- `_merge` conflict → state becomes failed
- `run()` drives full lifecycle (plan → execute → review → merge → completed)

Conventions: async, ruff py312 line-length 120, pytest-asyncio. All mocks use unittest.mock.AsyncMock.

---

### Task 2: CLI

**Files:**
- Modify: `src/horse_fish/cli.py`
- Create: `tests/test_cli.py`

**Implementation:**

```python
# src/horse_fish/cli.py
"""Horse-fish CLI — agent swarm coordinator."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.models import RunState, SubtaskState
from horse_fish.orchestrator.engine import Orchestrator
from horse_fish.planner.decompose import Planner
from horse_fish.store.db import Store
from horse_fish.validation.gates import ValidationGates

DB_PATH = ".horse-fish/state.db"


def _init_components(runtime: str, model: str | None, max_agents: int):
    """Initialize all components needed for orchestration."""
    repo_root = str(Path.cwd())
    store = Store(DB_PATH)
    store.migrate()
    tmux = TmuxManager()
    worktrees = WorktreeManager(repo_root)
    pool = AgentPool(store, tmux, worktrees)
    planner = Planner(runtime=runtime, model=model)
    gates = ValidationGates()
    orchestrator = Orchestrator(
        pool=pool, planner=planner, gates=gates, runtime=runtime, model=model or "", max_agents=max_agents
    )
    return orchestrator, store, pool


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Horse-fish: agent swarm coordinator."""


@main.command()
@click.argument("task", type=str)
@click.option("--runtime", default="claude", help="Default runtime for agents")
@click.option("--model", default=None, help="Model override")
@click.option("--max-agents", default=3, type=int, help="Max concurrent agents")
def run(task: str, runtime: str, model: str | None, max_agents: int):
    """Submit a task to the swarm."""
    orchestrator, store, _pool = _init_components(runtime, model, max_agents)
    try:
        result = asyncio.run(orchestrator.run(task))
        click.echo(f"Run {result.id}: {result.state}")
        for subtask in result.subtasks:
            click.echo(f"  [{subtask.state}] {subtask.description}")
    finally:
        store.close()


@main.command()
def status():
    """Show active runs, agents, subtask progress."""
    store = Store(DB_PATH)
    store.migrate()
    try:
        agents = store.fetchall("SELECT id, name, runtime, state, task_id FROM agents")
        if not agents:
            click.echo("No active agents.")
            return
        click.echo(f"{'Name':<20} {'Runtime':<10} {'State':<8} {'Task'}")
        click.echo("-" * 60)
        for row in agents:
            click.echo(f"{row['name']:<20} {row['runtime']:<10} {row['state']:<8} {row['task_id'] or '-'}")
    finally:
        store.close()


@main.command()
def clean():
    """Kill all agents, remove worktrees, reset state."""
    repo_root = str(Path.cwd())
    store = Store(DB_PATH)
    store.migrate()
    tmux = TmuxManager()
    worktrees = WorktreeManager(repo_root)
    pool = AgentPool(store, tmux, worktrees)
    try:
        released = asyncio.run(pool.cleanup())
        click.echo(f"Released {released} agents.")
    finally:
        store.close()


if __name__ == "__main__":
    main()
```

**Tests** (`tests/test_cli.py`):

Use Click's CliRunner to test commands. Mock _init_components or underlying classes. Test:
- `hf run "task"` invokes orchestrator and prints result
- `hf status` with no agents prints "No active agents."
- `hf status` with agents prints table
- `hf clean` calls pool.cleanup() and prints count
- `hf --version` prints version

Conventions: ruff py312 line-length 120, pytest.

---

### Task 3: Integration Test

**Files:**
- Create: `tests/test_integration.py`

**Implementation:**

Write a test that wires Orchestrator with mocked subprocess layer (TmuxManager and Planner CLI calls mocked, but real Store, real models, real ValidationGates logic).

Test the full lifecycle:
1. Create Orchestrator with mocked pool (spawn returns slot, send_task succeeds, check_status returns dead after first poll, collect_result returns SubtaskResult with diff)
2. Planner mocked to return 2 subtasks (one depends on the other)
3. ValidationGates mocked to return all-pass
4. WorktreeManager.merge mocked to return True
5. Call orchestrator.run("build feature X")
6. Assert: run.state == completed, both subtasks done, correct ordering (dep subtask ran first)

Also test failure scenarios:
- Planner returns error → run.state == failed
- Gate fails → run.state == failed
- Merge conflict → run.state == failed

Conventions: async, ruff py312 line-length 120, pytest-asyncio. All subprocess mocks via unittest.mock.AsyncMock.
