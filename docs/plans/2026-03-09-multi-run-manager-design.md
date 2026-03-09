# Multi-Run Manager + TUI Design

## Overview

Add a RunManager to handle multiple concurrent/queued runs, and redesign the TUI dashboard with a screen-stack navigation pattern (queue list → run detail drill-down).

## Goals

1. Submit multiple tasks — they queue up and execute with configurable concurrency
2. Python API (`RunManager`) for programmatic use, CLI wraps it
3. TUI dashboard shows all runs, drill into any run's detail with live tmux agent output
4. Non-blocking `hf run` by default (returns run ID immediately)

## Architecture

```
CLI / Python API
      │
      ▼
  RunManager (async event loop)
      │
      ├── max_concurrent_runs semaphore (default: 2)
      ├── run queue (SQLite-backed, ordered by created_at)
      └── per-run Orchestrator instances (asyncio.Task each)
            │
            └── existing pipeline: plan → execute → review → merge → learn
```

### RunManager (`src/horse_fish/orchestrator/run_manager.py`)

```python
class RunManager:
    def __init__(self, db_path: str, max_concurrent_runs: int = 2, **orchestrator_kwargs)
    async def submit(self, task: str) -> str  # returns run_id, non-blocking
    async def cancel(self, run_id: str) -> bool
    async def start(self)  # main event loop — dequeues and dispatches
    async def stop(self)   # graceful shutdown
    def list_runs(self) -> list[dict]  # all runs from SQLite
    def get_run(self, run_id: str) -> dict | None
```

**Event loop**: `start()` runs forever, polling for queued runs every 2s. When a slot opens (active < max_concurrent), it pops the oldest queued run, creates an Orchestrator instance, and launches `orchestrator.run(task)` as an asyncio.Task. On task completion, the slot is freed.

**Orchestrator creation**: RunManager holds the orchestrator kwargs (runtime, model, max_agents, etc.) and creates a fresh Orchestrator per run. Each Orchestrator gets its own AgentPool (separate tmux sessions, separate worktrees).

**Cancellation**: Sets run state to `cancelled`. If the run is active, cancels its asyncio.Task and triggers agent cleanup via `pool.cleanup()`.

### Model Changes (`src/horse_fish/models.py`)

Add to `RunState`:
```python
queued = "queued"
cancelled = "cancelled"
```

### Store Changes (`src/horse_fish/store/db.py`)

New methods:
- `fetch_queued_runs(limit: int) -> list[dict]` — ordered by created_at ASC
- `fetch_active_runs() -> list[dict]` — state in (planning, executing, reviewing, merging)
- `update_run_state(run_id: str, state: str) -> None`
- `insert_queued_run(run_id: str, task: str) -> None`

### CLI Changes (`src/horse_fish/cli.py`)

**Modified commands:**
- `hf run TASK` — non-blocking by default: submits to RunManager via a lightweight daemon, prints run_id
- `hf run TASK --foreground` — blocking (current behavior, for backward compat)
- `hf dash` — launches new two-screen TUI

**New commands:**
- `hf queue` — list pending/active/recent runs (shortcut for `hf report` filtered view)
- `hf cancel <RUN_ID>` — cancel a queued or running run

### TUI Dashboard (`src/horse_fish/dashboard/`)

**Screen stack pattern using Textual's `push_screen` / `pop_screen`:**

#### QueueScreen (default screen)

```
┌─ Horse-Fish Queue ─────────────────────────────────────────────┐
│ ID       │ Task                │ State     │ Agents │ Duration │
│──────────┼─────────────────────┼───────────┼────────┼──────────│
│ abc123   │ fix auth bug        │ executing │ 2/3    │ 5m       │
│ def456   │ add search          │ planning  │ 0/2    │ 1m       │
│ ghi789   │ refactor models     │ queued    │ -      │ 30s      │
│ jkl012   │ update docs         │ completed │ 1/1    │ 1h       │
├────────────────────────────────────────────────────────────────┤
│ 2 active │ 1 queued │ 1 completed    [Enter] detail  [q] quit │
└────────────────────────────────────────────────────────────────┘
```

- DataTable with cursor_type="row"
- Polls SQLite every 2s for all runs
- Footer shows summary counts
- Keybindings: Enter → push RunDetailScreen, q → quit, r → refresh

#### RunDetailScreen (pushed on Enter)

```
┌─ Run abc123: fix auth bug ──────────────────────────────────────┐
│ planning → [EXECUTING] → reviewing → merging     Run: abc123   │
├─────────────────────────────┬───────────────────────────────────┤
│ Agent      │ Runtime │ State│ Subtask              │ State │ Agt│
│────────────┼─────────┼──────│──────────────────────┼───────┼────│
│ worker-1   │ pi      │ busy │ fix login validation │running│ w-1│
│ worker-2   │ claude  │ busy │ add session timeout  │running│ w-2│
│            │         │      │ update tests         │pending│ -  │
├─────────────────────────────────────────────────────────────────┤
│ [worker-1 tmux output]                                          │
│ $ pi --provider dashscope --model qwen3.5-plus                  │
│ Reading file src/utils.py...                                    │
│ Editing line 42...                                              │
│                                                     [ESC] back  │
└─────────────────────────────────────────────────────────────────┘
```

- Reuses existing widgets: PipelineBar, AgentTable, SubtaskTable, AgentLog
- Polls only the selected run's data (filtered by run_id)
- AgentTable selection → updates AgentLog with tmux capture
- Keybindings: Escape → pop_screen back to queue, r → refresh

### New Files

```
src/horse_fish/
├── orchestrator/
│   └── run_manager.py        # NEW — RunManager class
├── dashboard/
│   ├── app.py                # REWRITE — DashApp with screen stack
│   ├── screens.py            # NEW — QueueScreen + RunDetailScreen
│   └── widgets.py            # MINOR EDITS — add queued/cancelled styles
├── models.py                 # EDIT — add queued/cancelled to RunState
├── store/
│   └── db.py                 # EDIT — add queue query methods
└── cli.py                    # EDIT — non-blocking run, queue, cancel commands
```

### RunState Flow

```
queued → planning → executing → reviewing → merging → completed
                                                    → failed
queued → cancelled (user cancel before start)
planning/executing/reviewing → cancelled (user cancel during run, triggers cleanup)
```

### Concurrency Model

- RunManager uses `asyncio.Semaphore(max_concurrent_runs)` to limit active runs
- Each run gets its own Orchestrator + AgentPool (already isolated via tmux sessions + git worktrees)
- SQLite WAL mode handles concurrent reads safely; writes serialize (fine for throughput)
- No shared mutable state between runs — each Orchestrator is independent

### Non-Blocking `hf run` Implementation

Option: RunManager runs as a background asyncio loop in the same process as `hf dash`. The `hf run` command writes a queued run directly to SQLite and exits. The dashboard (or a separate `hf daemon` process) picks up queued runs.

Simplest approach: `hf run` inserts a queued row into SQLite. `hf dash` (or `hf start`) launches RunManager alongside the TUI. This avoids daemon complexity.

For foreground mode (`hf run --foreground`), the existing blocking behavior is preserved.

## Out of Scope

- Web UI (future)
- Cross-run agent reuse / worker pool (each run spawns fresh agents)
- Inter-run merge ordering (runs merge independently)
- Run priorities / reordering
- Agent control from TUI (kill, retry — still read-only)

## Implementation Tasks

### Task 1: Models + Store (no deps)
- Add `queued`, `cancelled` to RunState enum
- Add Store methods: `insert_queued_run()`, `fetch_queued_runs()`, `fetch_active_runs()`, `update_run_state()`
- Tests for new store methods

### Task 2: RunManager (depends on Task 1)
- `RunManager` class with submit/cancel/start/stop
- Async event loop with semaphore-based concurrency
- Per-run Orchestrator creation and lifecycle
- Cancellation with agent cleanup
- Tests for RunManager (mock orchestrator)

### Task 3: TUI Screens (depends on Task 1)
- QueueScreen with DataTable, polling, summary footer
- RunDetailScreen reusing existing widgets, scoped to run_id
- Screen stack navigation (push/pop)
- Rewrite DashApp to use screen stack
- Update widget styles for queued/cancelled states

### Task 4: CLI Integration (depends on Task 2, Task 3)
- `hf run` non-blocking mode (insert queued + exit)
- `hf run --foreground` preserves current behavior
- `hf queue` command
- `hf cancel` command
- `hf dash` launches RunManager + TUI together
