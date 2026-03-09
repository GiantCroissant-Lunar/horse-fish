# Session 3 Handover — 2026-03-08

## Context

Continued from Session 2. Merged all pending batch 2 branches (5), then built and merged batch 3 (4 features) using overstory agent swarm with Pi/dashscope (qwen3.5-plus).

## What Was Done (Session 3)

### Merged Batch 2 (from Session 2)

5 branches merged to main in dependency order:

| Branch | Conflicts | Resolution |
|--------|-----------|------------|
| `overstory/orchestrator-builder/horse-fish-32d9` | None (fast-forward) | — |
| `overstory/cli-builder/horse-fish-046c` | `engine.py`, `test_orchestrator.py`, mulch config | Kept orchestrator-builder's versions (authoritative) |
| `overstory/integration-builder/horse-fish-594d` | None | — |
| `overstory/merge-queue-builder/horse-fish-566a` | mulch config only | — |
| `overstory/dispatch-builder/horse-fish-8e77` | mulch config only | — |

**Post-merge: 183 tests passing.**

### Built & Merged Batch 3 (4 agents, Pi/dashscope runtime)

| Agent | Task ID | File | New Tests | Description | Status |
|-------|---------|------|-----------|-------------|--------|
| memory-builder | horse-fish-c193 | `src/horse_fish/memory/store.py` | 15 | MemoryStore using memvid-sdk: store, search, store_run_result, find_similar_tasks | Merged |
| orchestrator-integrator-2 | horse-fish-160f | `src/horse_fish/orchestrator/engine.py` | 5 | Wired AgentSelector + MergeQueue into engine with fallback | Merged |
| cli-merge-builder | horse-fish-8df6 | `src/horse_fish/cli.py` | 4 | `hf merge` command with --dry-run and --force options | Merged |
| langfuse-builder | horse-fish-c352 | `src/horse_fish/observability/traces.py` | 9 | Tracer: trace_run, span, end_span, end_trace. No-op when disabled | Merged |

**Note:** First orchestrator-integrator attempt (horse-fish-f519) failed — agent ran `bun test` instead of `pytest`. Re-dispatched with explicit pytest instruction and succeeded.

**Post-merge: 216 tests passing.**

## Current State

### All Components on Main

```
src/horse_fish/
├── orchestrator/engine.py    # State machine with AgentSelector + MergeQueue integration
├── planner/decompose.py      # LLM task decomposition → DAG
├── dispatch/selector.py      # Market-first agent scoring (capability/runtime/files/idle)
├── agents/pool.py            # Agent lifecycle: spawn/send/check/collect/release
├── agents/runtime.py         # Runtime adapters: claude, copilot, pi, opencode
├── agents/tmux.py            # Tmux session management
├── agents/worktree.py        # Git worktree isolation
├── merge/queue.py            # FIFO merge queue with priority
├── memory/store.py           # Memvid-based semantic memory (NEW)
├── store/sqlite.py           # SQLite persistence layer
├── validation/gates.py       # Pre-merge quality gates (compile/ruff/pytest)
├── observability/traces.py   # Langfuse instrumentation (NEW)
└── cli.py                    # Click CLI: hf run, hf status, hf clean, hf merge (NEW)
```

### Test Counts by Module

| Module | Tests |
|--------|-------|
| test_orchestrator.py | 28 |
| test_dispatch.py | 25 |
| test_planner.py | 30 |
| test_memory.py | 15 |
| test_pool.py | 12 |
| test_validation.py | 16 |
| test_cli.py | 10 |
| test_store.py | 10 |
| test_observability.py | 9 |
| test_integration.py | 8 |
| test_tmux.py | 8 |
| test_worktree.py | 14 |
| **Total** | **216** (est.) |

## Key Decisions

- **Memvid over SQLite-vec**: Memory module uses `memvid-sdk` for video-based semantic storage instead of sqlite-vec. Stores .mv2 files in configurable data_dir.
- **Horse-fish is standalone**: Overstory is only used as a build tool to construct horse-fish. Horse-fish has NO runtime dependency on overstory.
- **Orchestrator fallback**: AgentSelector and MergeQueue are optional — engine falls back to round-robin dispatch and direct merge if not provided.

## What's NOT Built Yet

### Wiring (Integration)

- **Wire Tracer into Orchestrator** — instrument run lifecycle with Langfuse spans
- **Wire MemoryStore into Orchestrator** — call `store_run_result()` after run completion, `find_similar_tasks()` during planning
- **Configure Langfuse API keys** — create project at localhost:3000, get keys into .env

### Remaining Features

- **End-to-end test** — real subprocess test (not mocked) with a simple task
- **CLI `hf logs`** — view agent tmux output from CLI
- **Error recovery** — retry failed subtasks, reassign to different agents
- **.gitignore** — exclude `__pycache__/`, `.env`, `.horse-fish/` from repo

## Stale Worktrees to Clean

```bash
# Old batch 2 worktrees (already merged):
ov clean --all
# Or manually:
# overstory/cli-builder/horse-fish-046c
# overstory/dispatch-builder/horse-fish-8e77
# overstory/integration-builder/horse-fish-594d
# overstory/merge-queue-builder/horse-fish-566a
# overstory/orchestrator-builder/horse-fish-32d9
# overstory/orchestrator-integrator/horse-fish-f519 (failed, no commits)
# Batch 3 worktrees (already merged):
# overstory/memory-builder/horse-fish-c193
# overstory/orchestrator-integrator-2/horse-fish-160f
# overstory/cli-merge-builder/horse-fish-8df6
# overstory/langfuse-builder/horse-fish-c352
```

## Environment Setup

```bash
# Pi/dashscope agents:
tmux set-environment -g DASHSCOPE_API_KEY "REDACTED_DASHSCOPE_KEY"

# Langfuse (docker-compose):
docker compose up -d
# Then create project at localhost:3000 and add keys to .env

# Overstory:
ov status                    # monitor agents
sd create --title "..." --description "..." --json   # create task
ov sling <task-id> --capability builder --runtime pi --name <name>  # launch
ov clean --all               # cleanup stale worktrees
```

## Design Docs

- `docs/plans/2026-03-08-parallel-batch-1-design.md` — Agent Pool, Validation Gates, Planner
- `docs/plans/2026-03-08-runtime-fixes-design.md` — Copilot env isolation, dashscope setup
- `docs/plans/2026-03-08-orchestrator-cli-design.md` — Orchestrator + CLI design
- `docs/plans/2026-03-08-orchestrator-cli-implementation.md` — Full implementation plan
- `docs/plans/2026-03-08-batch3-parallel-design.md` — Memory, Orchestrator Integration, CLI merge, Langfuse

## Lessons Learned

- **Overstory agents use `bun test` by default** — always specify `pytest` explicitly in task descriptions for Python projects
- **Pi/qwen3.5-plus** is reliable for medium-complexity builder tasks (~4-5 min)
- **mulch.config.yaml** conflicts are cosmetic — safe to take --theirs
- **4 parallel agents** is a good batch size — manageable merge conflicts, fast turnaround
