# Orchestrator + CLI Design

Date: 2026-03-08

## Overview

Implement the orchestrator state machine and CLI to drive the agent swarm end-to-end. Horse-fish manages its own agent pool (not overstory wrappers).

## Orchestrator State Machine (`src/horse_fish/orchestrator/engine.py`)

Simple async state machine. Dict of `RunState → handler` transitions. No framework.

### API

```python
class Orchestrator:
    def __init__(self, store: Store, pool: AgentPool, planner: Planner, gates: ValidationGates)

    async def run(self, task: str) -> Run
        # Create Run, drive through states until completed/failed

    async def _plan(self, run: Run) -> Run
        # planner.decompose(task) → populate run.subtasks → state = executing

    async def _execute(self, run: Run) -> Run
        # For each ready subtask (deps met): pool.spawn + send_task
        # Poll loop: check git diff for commits + capture_pane for errors
        # As subtasks complete: collect_result, mark done/failed
        # When all done → state = reviewing

    async def _review(self, run: Run) -> Run
        # For each completed subtask: gates.run_all(worktree_path)
        # If all pass → state = merging
        # If any fail → mark subtask failed, state = failed

    async def _merge(self, run: Run) -> Run
        # For each passed subtask: worktree.merge()
        # Handle conflicts (mark failed)
        # state = completed

    async def _learn(self, run: Run) -> Run
        # Placeholder for v1 — just log metrics (duration, pass/fail per subtask)
```

### Polling Strategy

- 10s interval
- Primary: `worktree.get_diff()` for new commits (agent finished writing)
- Secondary: `pool.capture_pane()` for error detection (stack traces, exit codes)
- Configurable timeout per subtask (default 10min)

### DAG Execution

- Subtasks with no deps start immediately
- As subtasks complete, newly unblocked subtasks are dispatched
- Max concurrency = number of available agent slots
- Round-robin runtime assignment for v1 (no market-first bidding yet)

### State Transitions

```
planning → executing → reviewing → merging → completed
    ↓          ↓           ↓          ↓
  failed     failed      failed    failed
```

## CLI (`src/horse_fish/cli.py`)

Flesh out existing Click stub. All commands interact with Store + Orchestrator directly.

### Commands

```python
@main.command()
@click.argument("task")
@click.option("--runtime", default="claude")
@click.option("--model", default=None)
@click.option("--max-agents", default=3, type=int)
def run(task, runtime, model, max_agents):
    """Submit task to swarm."""
    # Init Store, AgentPool, Planner, ValidationGates, Orchestrator
    # asyncio.run(orchestrator.run(task))

@main.command()
def status():
    """Show active runs, agents, subtask progress."""
    # Read from Store, print formatted table

@main.command()
@click.argument("run_id")
def merge(run_id):
    """Manually merge a completed run's worktrees."""

@main.command()
def clean():
    """Kill all agents, remove worktrees, reset state."""
```

## Parallel Agent Tasks

Three Pi agents, split by dependency:

1. **Orchestrator engine** — `orchestrator/engine.py` + `tests/test_orchestrator.py`. Mock AgentPool, Planner, ValidationGates. Test each state transition.
2. **CLI** — `cli.py` + `tests/test_cli.py`. Stub orchestrator. Test Click commands with CliRunner.
3. **Integration wiring** — `tests/test_integration.py`. Wire real components (mocked tmux/subprocess). Test a full run lifecycle: plan → execute → review → merge.

## Dependencies

Orchestrator imports: Store, AgentPool, Planner, ValidationGates, Run, Subtask, RunState, SubtaskState
CLI imports: Orchestrator, Store, AgentPool, Planner, ValidationGates, TmuxManager, WorktreeManager
