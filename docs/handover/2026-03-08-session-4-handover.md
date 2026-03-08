# Session 4 Handover — 2026-03-08

## Context

Continued from Session 3. Researched 3 open-source repos (cognee, symphony, t3code) for borrowable concepts, then implemented batch 4 (5 features) using overstory agent swarm. Also benchmarked 3 runtimes.

## Research Phase

Analyzed 3 repos for patterns to borrow into horse-fish:

| Repo | Key Concept Borrowed |
|------|---------------------|
| [cognee](https://github.com/topoteretes/cognee) | Provenance stamping on artifacts, batch-level concurrency, hybrid retrieval |
| [symphony](https://github.com/openai/symphony) | Stall detection + auto-retry, per-state concurrency limits, blocker-aware dispatch |
| [t3code](https://github.com/pingdotgg/t3code) | Event sourcing for orchestrator state, checkpoint-per-turn as git refs, typed event taxonomy |

## What Was Done (Session 4)

### Batch 4: 5 Features Merged

| Agent | Runtime | Task | New Tests | Description |
|-------|---------|------|-----------|-------------|
| tracer-wire | Claude | Wire Tracer into Orchestrator | +2 | Trace per run, span per state handler, end_trace on completion |
| memory-wire | Claude | Wire MemoryStore into Orchestrator | +2 | `_learn()` calls `store_run_result()` on completion |
| stall-detect | Pi/qwen3.5-plus | Stall detection + auto-retry | +8 | `_check_stalls()` detects hung agents, retries up to max_retries |
| concurrency-limits | Pi/qwen3.5-plus | Per-state concurrency limits | +2 | `concurrency_limits` dict overrides `max_agents` per state |
| provenance-stamp | Pi/qwen3.5-plus | Provenance stamping | +3 | `_stamp_provenance()` tags results with agent_id, runtime, model, run_id |

**Post-merge: 228 tests passing.**

### Runtime Benchmark

Ran the same provenance-stamping task on 3 runtimes, and concurrency-limits on 2:

| Runtime | Model | Time | Result | Cost |
|---------|-------|------|--------|------|
| Pi/dashscope | qwen3.5-plus | ~3min | All tests pass | Free |
| Pi/dashscope | glm-5 | ~5min | All tests pass | Free |
| Codex | gpt-5.4 | 6min+ | **Stalled** — sandbox blocks git | Paid |

**Verdict:** qwen3.5-plus is best default for Pi. glm-5 works but ~60% slower. Codex needs sandbox config work.

### Pi Runtime Fix

Pi agents were failing silently because `.overstory/config.yaml` pointed at `kimi-coding` provider (expired KIMI_API_KEY). Fixed by switching to `dashscope` provider with `qwen3.5-plus` model.

### Merge Conflict Pattern

Overstory's auto-resolve consistently drops `__init__` parameters when multiple branches modify the same class. Had to manually restore:
- `tracer` param (dropped by memory-wire merge)
- `concurrency_limits` param (dropped by stall-detect merge)
- `_stamp_provenance()` method (dropped by stall-detect merge)

**Always run tests immediately after each `ov merge`.**

## Current State

### All Components on Main

```
src/horse_fish/
├── orchestrator/engine.py    # State machine + Tracer + MemoryStore + stall detection + concurrency limits
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
└── cli.py                    # Click CLI: run, status, clean, merge
```

### Orchestrator.__init__ Parameters (Current)

```python
Orchestrator(
    pool: AgentPool,
    planner: Planner,
    gates: ValidationGates,
    runtime: str = "claude",
    model: str | None = None,
    max_agents: int = 3,
    selector: AgentSelector | None = None,
    merge_queue: MergeQueue | None = None,
    tracer: Tracer | None = None,           # NEW in batch 4
    memory: MemoryStore | None = None,       # NEW in batch 4
    stall_timeout_seconds: int = 300,        # NEW in batch 4
    concurrency_limits: dict | None = None,  # NEW in batch 4
)
```

### Models Changes (Batch 4)

**Subtask** — new fields:
- `retry_count: int = 0`
- `max_retries: int = 2`
- `last_activity_at: datetime | None = None`

**SubtaskResult** — new provenance fields:
- `agent_id: str | None = None`
- `agent_runtime: str | None = None`
- `agent_model: str | None = None`
- `run_id: str | None = None`
- `completed_at: datetime | None = None`

## What's NOT Built Yet

### Batch 5 Candidates (from research)

- **Event sourcing for orchestrator state** (t3code) — bigger refactor, full audit trail + crash recovery
- **Checkpoint-per-turn as git refs** (t3code) — selective rollback per agent
- **Blocker-aware dispatch** (Symphony) — lightweight `_deps_met()` enhancement at dispatch time
- **Hot-reloadable workflow config** (Symphony) — change orchestrator behavior without restart
- **Batch-level concurrency control** (Cognee) — overlaps with per-state limits

### Other Remaining Work

- Configure Langfuse API keys from localhost:3000
- End-to-end test (real subprocess, not mocked)
- CLI `hf logs` — view agent tmux output
- .gitignore for `__pycache__/`, `.env`, `.horse-fish/`
- Fix Codex runtime (sandbox permissions for git)

## Environment Setup

```bash
# Pi/dashscope agents (IMPORTANT: config.yaml must use dashscope, not kimi-coding):
tmux set-environment -g DASHSCOPE_API_KEY "REDACTED_DASHSCOPE_KEY"

# Langfuse (docker-compose):
docker compose up -d

# Overstory:
ov status                    # monitor agents
sd create --title "..." --description "..." --json
ov sling <task-id> --capability builder --runtime pi --name <name>
ov merge --branch <branch>   # ALWAYS run pytest after merge
ov clean --all               # cleanup
```

## Design Docs

- `docs/plans/2026-03-08-batch4-enhancements.md` — Batch 4 implementation plan (this session)
- `docs/plans/2026-03-08-batch3-parallel-design.md` — Memory, Orchestrator Integration, CLI merge, Langfuse
- `docs/plans/2026-03-08-orchestrator-cli-design.md` — Orchestrator + CLI design
- `docs/plans/2026-03-08-parallel-batch-1-design.md` — Agent Pool, Validation Gates, Planner

## Lessons Learned

- **Pi config must use dashscope provider** — kimi-coding KIMI_API_KEY is expired, always use `provider: dashscope` + `model: qwen3.5-plus` in `.overstory/config.yaml`
- **qwen3.5-plus >> glm-5 for speed** — same quality, 60% faster
- **Codex sandbox blocks git** — needs `--dangerously-bypass-approvals-and-sandbox` or workspace-write config for overstory use
- **Auto-resolve drops __init__ params** — overstory's AI merge consistently loses constructor parameters when 3+ branches modify the same class. Run tests after every merge.
- **5 parallel agents works** — but expect 1-2 merge fixups on shared files like engine.py
- **Always say "pytest" in Pi task descriptions** — default quality gates in config.yaml say `bun test`
