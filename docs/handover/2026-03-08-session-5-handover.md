# Session 5 Handover — 2026-03-08

## Context

Continued from Session 4. Designed and swarmed batch 5 (3 features) using overstory with Pi/qwen3.5-plus. Also assessed horse-fish self-hosting readiness.

## What Was Done (Session 5)

### Batch 5: 3 Features + Housekeeping Merged

| Agent | Runtime | Time | New Tests | Description |
|-------|---------|------|-----------|-------------|
| blocker-dispatch | Pi/qwen3.5-plus | ~5min | +5 | ID-based deps, `_resolve_deps()`, updated existing dep tests |
| cli-logs | Pi/qwen3.5-plus | ~1.5min | +4 | `hf logs [--agent NAME] [--lines N]` command |
| e2e-test | Pi/qwen3.5-plus | ~5min | +2 | Real tmux+worktree e2e tests (skipped if no tmux) |
| (manual) | — | — | — | Root `.gitignore` for `__pycache__/`, `.env`, `.horse-fish/` |

**Post-merge fixup:** Integration test `test_orchestrator_respects_dependency_ordering` asserted description-based deps — updated to ID-based.

**Post-merge: 237 tests passing.**

### Self-Hosting Assessment

Horse-fish is ~2 batches from replacing overstory for its own development:

| Capability | Status |
|---|---|
| Task decomposition (DAG) | Done |
| Agent spawn in tmux + worktree | Done |
| Status polling + stall detection | Done |
| Merge queue + validation gates | Done |
| Memory/learning | Done |
| **Agent prompt injection** | **Gap** — agents don't get project context |
| **Ready detection** | **Gap** — no wait for runtime prompt |
| **Runtime interactive mode** | **Gap** — CLI commands need testing for interactive tmux |

## Current State

### All Components on Main

```
src/horse_fish/
├── orchestrator/engine.py    # State machine + ID-based deps + _resolve_deps + stall detection
├── planner/decompose.py      # LLM task decomposition → DAG
├── dispatch/selector.py      # Market-first agent scoring
├── agents/pool.py            # Agent lifecycle management
├── agents/runtime.py         # Runtime adapters: claude, copilot, pi, opencode
├── agents/tmux.py            # Tmux session management
├── agents/worktree.py        # Git worktree isolation
├── merge/queue.py            # FIFO merge queue with priority
├── memory/store.py           # Memvid-based semantic memory
├── store/db.py               # SQLite persistence layer
├── validation/gates.py       # Pre-merge quality gates
├── observability/traces.py   # Langfuse instrumentation
└── cli.py                    # Click CLI: run, status, clean, merge, logs
```

### Key Changes (Batch 5)

- `_deps_met()` now matches by subtask ID, not description string
- `_resolve_deps()` converts planner's description-based deps to IDs after planning
- `hf logs` command shows agent tmux output (list all or `--agent NAME`)
- E2e tests prove real tmux + worktree + agent spawn flow works

## Batch 6 Candidates (Self-Hosting)

1. **Agent prompt template** — inject CLAUDE.md, project structure, test commands into agent context
2. **Ready detection per runtime** — wait for `>` prompt (claude), `>>>` (pi), etc. before sending task
3. **Runtime interactive mode** — fix `build_command()` for interactive tmux sessions vs `--print` mode

## Environment Setup

```bash
tmux set-environment -g DASHSCOPE_API_KEY "REDACTED_DASHSCOPE_KEY"
docker compose up -d   # Langfuse
ov status              # monitor
```
