# Cognee as Universal Memory

**Date:** 2026-03-09
**Status:** Approved

## Problem

Memory is fragmented across three systems:
- **Memvid** — agent-local, rarely used
- **Cognee** — orchestrator-only, blocks on `cognify()` during learn phase
- **Mulch** — overstory external tool, not integrated with horse-fish

Agents can't share knowledge. Claude Code uses flat files. No unified read path.

## Design

**Write fast (memvid), organize later (Cognee), read smart (graph queries).**

```
Agents / Orchestrator / Claude Code
        | write                | read
        v                      v
     memvid (local)         Cognee (Kuzu + LanceDB)
        |                      ^
        +---- hf memory organize ---+
              (batch ingestion)
```

### Write Path

Everything writes to memvid — fast, local, no lock contention.

Each entry is tagged with metadata:
- `agent`: agent name or "interactive" for Claude Code
- `run_id`: optional, links to pipeline run
- `domain`: topic tag (e.g., "planner", "dispatch", "convention")
- `tags`: user-supplied comma-separated tags
- `timestamp`: ISO 8601
- `ingested`: boolean, false until organized into Cognee

### Organize

`hf memory organize` — batch process that:
1. Reads all uningested memvid entries
2. Ingests into Cognee with structured node_sets and dataset_name
3. Marks entries as ingested
4. Reports count of processed entries

Single-writer to Kuzu, runs on-demand only (for now).

### Read Path

Everything reads from Cognee via `hf memory search "query"`.
Uses `SearchType.GRAPH_COMPLETION` for graph-traversal + vector search.
SmartPlanner continues reading from Cognee (no change needed).

### CLI Commands

| Command | Description |
|---------|-------------|
| `hf memory store "content" --tags "x,y"` | Write to memvid with metadata |
| `hf memory search "query" [--top-k N]` | Read from Cognee |
| `hf memory organize` | Batch ingest memvid -> Cognee |
| `hf memory status` | Show uningested count, Cognee stats |

### Changes to Existing Code

1. **MemoryStore** — add metadata fields (agent, run_id, domain, tags, ingested flag), add `get_uningested()` method
2. **Orchestrator `_learn()`** — write to memvid only, drop direct Cognee ingestion
3. **CogneeMemory** — add `batch_ingest(entries)` method for organize command
4. **Agent prompt** — replace `ml record`/`ml prime` with `hf memory store`/`hf memory search`
5. **CLI** — add `hf memory` command group with store/search/organize/status subcommands
6. **Claude Code skill** — document CLI commands for interactive use

### Not In Scope (Yet)

- Auto-organize after pipeline runs
- Cron/scheduled ingestion
- Removing mulch from overstory overlays (separate concern)

## Implementation Tasks

### Task 1: MemoryStore metadata + uningested tracking
**Files:** `src/horse_fish/memory/store.py`, `tests/test_memory.py`
**What:** Add metadata fields to memvid entries (agent, run_id, domain, tags, timestamp, ingested). Add `get_uningested()` and `mark_ingested(ids)` methods. Since memvid is append-only video, track ingestion state in SQLite side-table.

### Task 2: CogneeMemory batch ingestion
**Files:** `src/horse_fish/memory/cognee_store.py`, `tests/test_cognee_memory.py`
**What:** Add `batch_ingest(entries: list[MemoryEntry])` method. Groups entries by domain into node_sets, calls cognify once per batch. Returns count of successfully ingested entries.

### Task 3: CLI `hf memory` command group
**Files:** `src/horse_fish/cli.py`
**What:** Add `memory` Click group with `store`, `search`, `organize`, `status` subcommands. Wire to MemoryStore and CogneeMemory.

### Task 4: Orchestrator learn phase simplification
**Files:** `src/horse_fish/orchestrator/engine.py`, `tests/test_engine.py`
**What:** `_learn()` writes to memvid only. Remove direct CogneeMemory.ingest_run_result() call. Tag entries with run_id and agent metadata.

### Task 5: Agent prompt update
**Files:** `src/horse_fish/agents/prompt.py`
**What:** Replace `ml record`/`ml prime` references with `hf memory store`/`hf memory search` in agent prompt template.

### Task 6: Claude Code skill
**Files:** `.agent/skills/hf-memory/SKILL.md`
**What:** Document `hf memory` CLI commands for Claude Code interactive use.

## Swarm Execution Plan

Tasks 1 and 2 are independent (different files). Task 3 depends on 1+2. Task 4 depends on 1. Task 5 and 6 are independent leaf tasks.

```
  [Task 1: MemoryStore]  [Task 2: CogneeMemory]  [Task 5: Prompt]  [Task 6: Skill]
         \                    /                        |                |
          \                  /                         |                |
           [Task 3: CLI]                               |                |
               \                                       |                |
                [Task 4: Orchestrator]                 |                |
```

Parallelizable as 3 agents:
- Agent A: Task 1 -> Task 3 -> Task 4
- Agent B: Task 2 (merges into A's CLI work)
- Agent C: Task 5 + Task 6
