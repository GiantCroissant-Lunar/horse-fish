# Session 9 Handover — 2026-03-09

## Context

Continued from Session 8 (318 tests, `hf smoke` passing). This session addressed findings from an external code review, added a TUI dashboard, Kimi runtime, and infrastructure hardening.

## What Was Done

### Bug Fixes (318 → 359 tests)

Used 3 overstory Pi agents in parallel, all clean merges.

| Bug | Fix | Agent |
|-----|-----|-------|
| Retry deadlock in engine.py | `_check_stalls()` return value now decrements `active_count` | pi-retry-fix |
| Task identity lost in pool.py | `send_task()` now persists `task_id` in SQLite | pi-pool-state |
| Incomplete cleanup in pool.py | `cleanup()` now releases busy agents too | pi-pool-state |
| Broken e2e tests | Added `BashRuntime` adapter, tests use `runtime="bash"` | pi-bash-runtime |

### TUI Dashboard (359 → 363 tests)

Used 3 overstory Pi agents (2 parallel + 1 sequential), all clean merges.

| Component | Agent | Description |
|-----------|-------|-------------|
| SQLite persistence | pi-sqlite-persist | `runs` + `subtasks` tables, orchestrator write-through via `upsert_run`/`upsert_subtask` |
| TUI widgets | pi-tui-widgets | PipelineBar, AgentTable, SubtaskTable, AgentLog (Textual) |
| DashApp + CLI | pi-dash-app | `hf dash` command, 2s poll loop, keyboard nav |

Dashboard is a read-only observer — imports only `Store` and `TmuxManager`, never orchestration code. Swarm runs fine without it.

### Kimi Runtime

Added `KimiRuntime` adapter for kimi CLI v1.12+ (kimi-for-coding model, Moonshot AI).

- Spawn command: `kimi --yolo --model <model>`
- Ready pattern: `yolo\s+agent|Send /help` (banner scrolls off in 80x24 tmux)
- Verified end-to-end: pool spawn → ready detection → release

### Infrastructure

| Change | Details |
|--------|---------|
| `infra/` folder | `.env.example`, `setup-env.sh` (loads .env into shell + tmux), `docker-compose.yml` (moved from root) |
| Pre-commit hooks | `detect-secrets`, ruff lint+format, large file check, merge conflict check |
| Git history scrub | Leaked Dashscope + Inception API keys replaced with `REDACTED_*` via `git filter-repo`, force-pushed |
| README.md | Project overview, quick start, architecture, CLI commands |
| Taskfile.yml | `task test`, `task lint`, `task smoke`, `task dash`, `task langfuse:up`, etc. |

## Current State

- **363 tests passing** (pytest, ~2 min)
- **7 runtimes**: claude, copilot, pi, opencode, kimi, bash (test-only)
- **GitHub remote**: `git@github.com:GiantCroissant-Lunar/horse-fish.git`
- Pre-commit hooks active (detect-secrets blocks new API key leaks)

## Files Changed This Session

```
src/horse_fish/orchestrator/engine.py    # Retry fix + SQLite persistence
src/horse_fish/agents/pool.py            # task_id + cleanup fix
src/horse_fish/agents/runtime.py         # BashRuntime + KimiRuntime
src/horse_fish/store/db.py               # runs + subtasks tables
src/horse_fish/dashboard/__init__.py     # New
src/horse_fish/dashboard/app.py          # New — DashApp
src/horse_fish/dashboard/widgets.py      # New — TUI widgets
src/horse_fish/cli.py                    # hf dash command
pyproject.toml                           # dashboard optional dep
infra/.env.example                       # New
infra/setup-env.sh                       # New
infra/docker-compose.yml                 # Moved from root
.pre-commit-config.yaml                  # New
.secrets.baseline                        # New
README.md                                # New
Taskfile.yml                             # New
CLAUDE.md                                # Added kimi runtime
```

## API Keys Status

Both keys were leaked in git history and scrubbed. **Rotate immediately:**
- `DASHSCOPE_API_KEY` — Alibaba Cloud console
- `INCEPTION_API_KEY` — Inception AI dashboard

After rotating, store in `infra/.env` (gitignored).

## Next Steps

- Test `hf dash` live against a real `hf run` (visual verification)
- Multi-agent parallel execution test (max-agents > 1)
- Configure Langfuse API keys
- CoPaw integration for web dashboard (phase 2)
- Watchdog/health reconciler (from code review recommendations)
