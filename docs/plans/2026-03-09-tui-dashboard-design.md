# TUI Dashboard Design

## Overview

Read-only Textual TUI dashboard for monitoring horse-fish agent swarms. Launched via `hf dash`, attaches to an existing `hf run` by polling the same SQLite database and tmux sessions.

## Architecture

```
hf run (terminal 1)          hf dash (terminal 2)
    |                             |
    v                             v
Orchestrator --writes--> SQLite <--polls-- DashApp
    |                                        |
    v                                        v
tmux panes <---------- capture-pane ---- TmuxManager
```

The dashboard is a read-only observer. It imports `Store` and `TmuxManager` but never imports `Orchestrator`, `AgentPool`, or any orchestration code. If the TUI crashes or isn't running, the swarm is unaffected.

## Layout

```
+- Pipeline ------------------------------------------------------+
|  plan > [EXECUTING] > review > merge > learn     Run: abc-123   |
+- Agents ---------------------+- Subtasks ------------------------+
|  NAME       RUNTIME  STATE   |  DESCRIPTION        STATE  AGENT  |
| > hf-abc123  pi       busy   |  Fix the add fn     done   hf-a   |
|   hf-def456  claude   idle   |  Update tests       running hf-b  |
|                              |  Refactor utils     pending  -    |
+- Agent Log -----------------------------------------------------|
|  $ pi --provider dashscope --model qwen3.5-plus                  |
|  Reading file src/utils.py...                                    |
|  Editing line 42...                                              |
+------------------------------------------------------------------+
```

- Top bar: Pipeline phase indicator with current run ID
- Middle-left: Agent table (selectable with arrow keys)
- Middle-right: Subtask table with state and assigned agent
- Bottom: Live tmux capture of selected agent, auto-refreshing

## Components

### New files

- `src/horse_fish/dashboard/__init__.py` — empty
- `src/horse_fish/dashboard/app.py` — Textual App subclass, poll loop, keybindings
- `src/horse_fish/dashboard/widgets.py` — PipelineBar, AgentTable, SubtaskTable, AgentLog widgets

### Prerequisite: SQLite persistence for runs and subtasks

Currently Run and Subtask objects live only in memory during orchestrator.run(). The dashboard needs to read them from SQLite. Two new tables:

**runs** table:
- id TEXT PRIMARY KEY
- task TEXT NOT NULL
- state TEXT NOT NULL
- complexity TEXT
- created_at TEXT NOT NULL
- completed_at TEXT

**subtasks** table:
- id TEXT PRIMARY KEY
- run_id TEXT NOT NULL (FK to runs)
- description TEXT NOT NULL
- state TEXT NOT NULL
- agent_id TEXT
- deps TEXT (JSON array)
- retry_count INTEGER DEFAULT 0
- created_at TEXT NOT NULL

The orchestrator writes to these tables as it transitions state. The dashboard reads them.

### Orchestrator state persistence

Add write-through calls in engine.py at each state transition:
- `_plan()`: INSERT run + subtasks after planning
- `_execute()`: UPDATE subtask state/agent_id on dispatch, completion, failure
- `_review()`: UPDATE subtask state on gate pass/fail
- `_merge()`: UPDATE run state on merge complete/fail
- `run()`: UPDATE run state + completed_at on terminal state

### CLI command

```python
@main.command()
def dash():
    """Live TUI dashboard (read-only)."""
    from horse_fish.dashboard.app import DashApp
    app = DashApp(db_path=DB_PATH)
    app.run()
```

### Dependency

```toml
[project.optional-dependencies]
dashboard = ["textual>=1.0"]
```

## Keybindings

| Key | Action |
|-----|--------|
| up/down or j/k | Select agent |
| tab | Switch focus between agent and subtask tables |
| q | Quit |
| r | Force refresh |

No kill/control actions. Read-only only.

## Implementation batches

### Batch 1: SQLite persistence (prerequisite)
- Add runs and subtasks tables to store migration
- Add write-through methods to Store (upsert_run, upsert_subtask)
- Wire orchestrator to persist state at each transition
- Tests for persistence

### Batch 2: TUI widgets
- PipelineBar widget
- AgentTable widget
- SubtaskTable widget
- AgentLog widget
- Widget unit tests

### Batch 3: Dashboard app + CLI
- DashApp (layout, poll loop, keybindings)
- `hf dash` CLI command
- Integration test

## Out of scope

- Agent control (kill, retry, pause)
- Historical run browser
- CoPaw integration (phase 2)
