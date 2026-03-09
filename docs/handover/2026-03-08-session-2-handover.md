# Session 2 Handover — 2026-03-08

## Context

Continued from Session 1. Built batch 1 (Agent Pool, Validation Gates, Planner), then batch 2 (Orchestrator, CLI, Integration Tests, Merge Queue, Dispatch). All built using overstory agent swarm with Pi CLI + Alibaba Coding Plan (dashscope) as free runtime.

## What Was Built (Session 2)

### Batch 1: Foundation Wiring (3 agents, Claude runtime)

| File | Tests | Description | Runtime |
|------|-------|-------------|---------|
| `src/horse_fish/agents/pool.py` | 12 | AgentPool: spawn/send_task/check_status/collect_result/release/cleanup | Claude |
| `src/horse_fish/validation/gates.py` | 16 | ValidationGates: compile/ruff-check/pytest gates, run_all, all_passed | Claude |
| `src/horse_fish/planner/decompose.py` | 30 | Planner: task decomposition via CLI runtime, JSON parsing | Claude |

**58 new tests, all merged to main. Total: 114 tests.**

### Batch 2: Orchestrator + CLI + Extras (5 agents, Pi/dashscope runtime)

**NOT YET MERGED — pending merge in next session.**

| Branch | File | Tests | Description | Model |
|--------|------|-------|-------------|-------|
| `overstory/orchestrator-builder/horse-fish-32d9` | `src/horse_fish/orchestrator/engine.py` | 23 | State machine: plan → execute → review → merge. DAG execution, polling, deadlock detection | glm-4.7 |
| `overstory/cli-builder/horse-fish-046c` | `src/horse_fish/cli.py` | 6 | Click CLI: hf run, hf status, hf clean | glm-4.7 |
| `overstory/integration-builder/horse-fish-594d` | `tests/test_integration.py` | 8 | Full lifecycle integration tests with mocked subprocess layer | glm-4.7 |
| `overstory/merge-queue-builder/horse-fish-566a` | `src/horse_fish/merge/queue.py` | 10 | FIFO merge queue with priority support | qwen3.5-plus |
| `overstory/dispatch-builder/horse-fish-8e77` | `src/horse_fish/dispatch/selector.py` | 25 | AgentSelector: capability/runtime/files/idle scoring | qwen3.5-plus |

**72 new tests, all passing in worktrees. Merge order: orchestrator → cli → integration → merge-queue → dispatch.**

### Runtime Fixes

| Change | Detail |
|--------|--------|
| Pi + dashscope | Added dashscope provider to `~/.pi/agent/models.json` (OpenAI-compatible endpoint) |
| 5 free models | glm-4.7, glm-5, qwen3.5-plus, kimi-k2.5, MiniMax-M2.5 — all verified working |
| Copilot env fix | Documented: do NOT set `ANTHROPIC_DEFAULT_SONNET_MODEL` globally |
| OpenCode disabled | `detectReady` stub in overstory → use Pi + dashscope instead |
| PiRuntime.build_env() | Now passes DASHSCOPE_API_KEY, KIMI_API_KEY, ZAI_API_KEY to tmux sessions |

### Infrastructure

- **Langfuse v3** running via `docker-compose.yml` on `localhost:3000` (fresh instance, no API keys configured yet)
- **Overstory tools** installed: `ov`, `sd`, `ml`, `cn` (npm, requires `bun`)

## Merge Instructions (Next Session)

```bash
# Merge in dependency order:
ov merge --branch overstory/orchestrator-builder/horse-fish-32d9
ov merge --branch overstory/cli-builder/horse-fish-046c
ov merge --branch overstory/integration-builder/horse-fish-594d
ov merge --branch overstory/merge-queue-builder/horse-fish-566a
ov merge --branch overstory/dispatch-builder/horse-fish-8e77

# Verify full suite:
pytest tests/ -v

# Cleanup:
ov clean --all
```

**Expected test count after merge: ~186 tests** (114 existing + 72 new).

Note: cli-builder also created its own orchestrator engine.py — there may be a merge conflict with orchestrator-builder. Orchestrator-builder's version is authoritative (23 tests vs 6).

## What's NOT Built Yet

### Next Priority: Langfuse Instrumentation

- Add `langfuse` SDK to `pyproject.toml`
- Create Langfuse project at `localhost:3000`, get API keys
- Add `.env` with `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`
- Instrument Orchestrator with traces (run lifecycle, subtask spans, timing)

### Remaining Components

- `memory/` — SQLite-vec embeddings + knowledge store (needs `sqlite-vec`, `fastembed`)
- Orchestrator improvements: integrate Dispatch (AgentSelector) and MergeQueue into engine
- CLI improvements: `hf merge <run_id>` command

## Key Findings

### Pi/Dashscope Model Performance

| Model | Task Complexity | Time | Quality |
|-------|----------------|------|---------|
| glm-4.7 | Complex (orchestrator) | ~11-15min | Good, but slow |
| qwen3.5-plus | Medium (merge queue, dispatch) | ~4min | Good, fast |
| kimi-k2.5 | Untested in swarm | — | Should be fast |

**Recommendation: Use qwen3.5-plus for builder tasks, kimi-k2.5 for lighter tasks. Avoid glm-4.7 for complex work.**

### Overstory Config

```yaml
# .overstory/config.yaml runtime section
runtime:
  default: claude
  pi:
    provider: dashscope
    model: qwen3.5-plus
    modelMap:
      opus: dashscope/qwen3.5-plus
      sonnet: dashscope/qwen3.5-plus
      haiku: dashscope/kimi-k2.5
```

### Environment Setup

```bash
# Required before spawning Pi agents:
tmux set-environment -g DASHSCOPE_API_KEY "REDACTED_DASHSCOPE_KEY"

# Langfuse (docker-compose):
docker compose up -d

# Overstory:
ov status                    # monitor
ov sling <task-id> --capability builder --runtime pi --name <name>
ov merge --branch <branch>
ov clean --all
```

## Design Docs

- `docs/plans/2026-03-08-parallel-batch-1-design.md` — Agent Pool, Validation Gates, Planner
- `docs/plans/2026-03-08-runtime-fixes-design.md` — Copilot env isolation, dashscope setup
- `docs/plans/2026-03-08-orchestrator-cli-design.md` — Orchestrator + CLI design
- `docs/plans/2026-03-08-orchestrator-cli-implementation.md` — Full implementation plan with code
