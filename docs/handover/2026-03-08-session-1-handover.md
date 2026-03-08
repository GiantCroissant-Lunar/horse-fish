# Session 1 Handover — 2026-03-08

## Context

Horse-fish is a new agent swarm orchestration framework, built from scratch using lessons learned from two prior projects:
- **tentacle-punch** (Python) — LangGraph-based orchestrator with A2A protocol. Core problem: CLI tools piped via stdin/stdout failed to write files ~50% of the time ("no diff" issue)
- **giant-isopod** (C#) — Akka.NET actor-based system with market-first dispatch. More mature memory/knowledge design but same CLI subprocess issues

Horse-fish replaces both with a simpler approach: agents run in **interactive tmux sessions** (not piped subprocesses), each with their own **git worktree** for isolation.

## Development Tool: Overstory

Horse-fish is built **using** overstory (`@os-eco/overstory-cli` v0.8.6) as the development tool — overstory orchestrates a swarm of AI coding agents that write horse-fish's code. Overstory is NOT a runtime dependency.

### Overstory Setup (in this repo)

```bash
# Already initialized: .overstory/ directory, seeds task tracker, mulch knowledge
# Ecosystem tools installed globally: ov, sd (seeds), ml (mulch), cn (canopy)

# Before spawning agents:
tmux set-environment -g KIMI_API_KEY "$KIMI_API_KEY"
# For copilot runtime only (don't set when using claude runtime):
export ANTHROPIC_DEFAULT_SONNET_MODEL=gpt-5.4
```

### Spawning agents

```bash
sd create --title "Task title" --description "Detailed description" --json
ov sling <task-id> --capability builder --runtime <claude|pi|copilot|opencode> --name <agent-name>
ov status                    # monitor
ov merge --branch <branch>   # merge completed work
ov clean --all               # cleanup after run
```

## What Was Built (Session 1)

### Foundation Components (4 agents, 3 runtimes, all merged)

| File | Lines | Description | Built By |
|------|-------|-------------|----------|
| `src/horse_fish/models.py` | 83 | Pydantic v2 domain models: AgentSlot, AgentState, Subtask, SubtaskState, SubtaskResult, Run, RunState | Claude Code |
| `src/horse_fish/store/db.py` | 113 | SQLite store with WAL mode, migration system, v1 schema (agents, runs, subtasks, mail tables) | Claude Code |
| `src/horse_fish/agents/tmux.py` | 118 | TmuxManager: spawn/send_keys/capture_pane/kill_session/list_sessions/is_alive | Copilot/gpt-5.4 |
| `src/horse_fish/agents/runtime.py` | 82 | RuntimeAdapter protocol + ClaudeRuntime, CopilotRuntime, PiRuntime, OpenCodeRuntime + RUNTIME_REGISTRY | Copilot/gpt-5.4 |
| `src/horse_fish/agents/worktree.py` | 298 | WorktreeManager: create/merge/remove/list/cleanup/get_diff with WorktreeInfo model | Pi/kimi-for-coding |
| `src/horse_fish/cli.py` | 15 | Stub CLI entry point (Click) | Manual |
| `tests/test_models.py` | 196 | 20 tests for models | Claude Code |
| `tests/test_store.py` | 119 | 10 tests for store | Claude Code |
| `tests/test_tmux.py` | 140 | 8 tests for tmux (mocked) | Copilot/gpt-5.4 |
| `tests/test_worktree.py` | 347 | 15 tests for worktree (real git repos via tmp_path) | Pi/kimi-for-coding |

**53 tests, all passing.**

### Key Files

- `CLAUDE.md` — Project conventions and architecture overview
- `pyproject.toml` — Python 3.12+, ruff, pytest config
- `docs/runtimes/` — Runtime setup docs (pi-kimi, copilot, opencode)
- `docs/findings/2026-03-08-overstory-swarm-validation.md` — Full validation report

## What's NOT Built Yet

### Next Priority: Orchestrator + Planner

```
src/horse_fish/
├── orchestrator/    # EMPTY — State machine: plan → dispatch → execute → review → merge → learn
├── planner/         # EMPTY — LLM task decomposition (task → DAG of subtasks)
├── dispatch/        # EMPTY — Market-first agent selection + bidding
├── merge/           # EMPTY — Merge queue + conflict resolution
├── memory/          # EMPTY — SQLite-vec embeddings + knowledge store
├── validation/      # EMPTY — Pre-merge quality gates
```

### Suggested Next Tasks (in dependency order)

1. **Agent Pool** — `src/horse_fish/agents/pool.py` — Manages AgentSlot lifecycle, spawn via TmuxManager + WorktreeManager, track state in Store. This wires together the 4 foundation components.

2. **Orchestrator State Machine** — `src/horse_fish/orchestrator/engine.py` — Simple state machine (not LangGraph): plan → dispatch → execute → review → merge → learn. Drives the agent pool.

3. **Planner** — `src/horse_fish/planner/decompose.py` — Takes a task description, calls an LLM (via any runtime in headless/print mode), returns a DAG of Subtasks.

4. **Validation Gates** — `src/horse_fish/validation/gates.py` — Run compile check, tests, lint on a worktree before allowing merge.

5. **CLI** — Flesh out `cli.py` with `hf run "task"`, `hf status`, `hf merge`, `hf clean`.

## Runtime Findings

### What Works
- **Claude Code**: Best runtime — fast (~90s), reliable, good instruction following
- **Pi/kimi-for-coding**: Good secondary — free, reliable, 4min for tasks
- **All runtimes write files** when run in tmux sessions (no "no diff" issue)

### What Doesn't
- **Copilot**: Slow (~9min), wastes time exploring mulch infrastructure
- **OpenCode/qwen**: Stalled waiting for coordinator, had to kill and re-sling with Claude
- **`ANTHROPIC_DEFAULT_SONNET_MODEL` env var**: Global — setting it for copilot breaks claude. Need per-runtime model config

### Environment Requirements

```bash
# KIMI_API_KEY: Required for pi runtime
# Must be in tmux global env (not just shell):
tmux set-environment -g KIMI_API_KEY "$KIMI_API_KEY"

# Copilot trusted folders: worktree base dir must be trusted
# ~/.copilot/config.json → trusted_folders includes .overstory/worktrees

# Python: 3.12+, install with: pip install -e ".[dev]"
# Tests: pytest tests/ (53 passing)
# Lint: ruff check src/ tests/
```

## Design Principles (from CLAUDE.md)

- **No heavy frameworks**: No LangGraph, no Akka. Simple Python state machines
- **SQLite for everything**: Mail, tasks, artifacts, metrics, memory — all in SQLite
- **Tmux for agents**: Each agent runs in its own tmux pane
- **Git worktree isolation**: Each agent works on its own branch
- **Async by default**: asyncio for subprocess management and I/O
