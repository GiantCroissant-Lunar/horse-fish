# Batch 3: Parallel Feature Design — 2026-03-08

4 features, dispatched in parallel via overstory agent swarm.

## Feature 1: Memory Module (memvid)

**File**: `src/horse_fish/memory/store.py`
**Tests**: `tests/test_memory.py`

Cross-session learning store using memvid (video-based AI memory). Stores task results, agent performance, solutions for semantic retrieval.

### API

```python
class MemoryStore:
    def __init__(self, data_dir: Path):
        """Initialize memvid memory at data_dir/knowledge.mv2"""

    async def store(self, content: str, metadata: dict) -> str:
        """Store a text chunk with metadata. Returns chunk_id."""

    async def search(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        """Semantic search. Returns ranked hits."""

    async def store_run_result(self, run: Run, subtask_results: list[SubtaskResult]):
        """Store a completed run's results for future learning."""

    async def find_similar_tasks(self, task_description: str, top_k: int = 3) -> list[MemoryHit]:
        """Find past tasks similar to a new one."""

    async def close(self):
        """Flush and close memvid file."""
```

### Models

```python
class MemoryHit(BaseModel):
    chunk_id: str
    content: str
    score: float
    metadata: dict
```

### Dependencies

- `pip install memvid-sdk` (add to pyproject.toml `[project.optional-dependencies] memory`)
- Remove sqlite-vec and fastembed from memory extras

### Notes

- Use memvid Python SDK, NOT the Rust core
- Store .mv2 files in configurable data_dir (default: `.horse-fish/memory/`)
- Mock memvid in tests — don't require actual memvid install for pytest
- Import pattern: `from horse_fish.memory.store import MemoryStore`
- Create `src/horse_fish/memory/__init__.py` with `from .store import MemoryStore`

---

## Feature 2: Orchestrator Integration (Dispatch + MergeQueue)

**File**: `src/horse_fish/orchestrator/engine.py` (modify existing)
**Tests**: `tests/test_orchestrator.py` (extend existing)

Wire AgentSelector and MergeQueue into the orchestrator engine.

### Changes

1. **Constructor**: Accept optional `AgentSelector` and `MergeQueue` params
2. **`_execute()`**: Use `AgentSelector.select(subtask, available_agents)` instead of round-robin assignment
3. **`_merge()`**: Use `MergeQueue.enqueue()` + `MergeQueue.process()` instead of direct worktree merge
4. **Fallback**: If no AgentSelector provided, keep current round-robin. If no MergeQueue, keep direct merge.

### AgentSelector Integration

```python
# In _execute(), replace direct dispatch with:
if self._selector:
    agent = self._selector.select(subtask, self._pool.list_agents(status="idle"))
    if agent is None:
        continue  # no suitable agent, retry next poll
else:
    agent = self._next_round_robin()
```

### MergeQueue Integration

```python
# In _merge(), replace direct merge with:
if self._merge_queue:
    for subtask in completed:
        await self._merge_queue.enqueue(subtask.id, agent_name, branch)
    results = await self._merge_queue.process()
else:
    # existing direct merge logic
```

### New Tests (add to existing file)

- test_execute_uses_agent_selector_when_provided
- test_execute_falls_back_to_round_robin_without_selector
- test_merge_uses_queue_when_provided
- test_merge_falls_back_to_direct_without_queue
- test_selector_returns_none_skips_dispatch

---

## Feature 3: CLI `hf merge` Command

**File**: `src/horse_fish/cli.py` (modify existing)
**Tests**: `tests/test_cli.py` (extend existing)

### Command

```
hf merge [RUN_ID]    # Process merge queue for a run
  --dry-run           # Show what would be merged without merging
  --force             # Force merge even if gates fail
```

### Implementation

```python
@cli.command()
@click.argument("run_id", required=False)
@click.option("--dry-run", is_flag=True)
@click.option("--force", is_flag=True)
def merge(run_id, dry_run, force):
    """Process merge queue."""
    store = Store(DB_PATH)
    merge_queue = MergeQueue(store)

    if dry_run:
        pending = asyncio.run(merge_queue.pending())
        # display pending merges
        return

    results = asyncio.run(merge_queue.process())
    # display results
```

### New Tests

- test_merge_command_processes_queue
- test_merge_dry_run_shows_pending
- test_merge_no_pending_shows_message

---

## Feature 4: Langfuse Instrumentation

**File**: `src/horse_fish/observability/traces.py`
**Tests**: `tests/test_observability.py`

Add observability traces to orchestrator run lifecycle.

### API

```python
class Tracer:
    def __init__(self, enabled: bool = True):
        """Init Langfuse client from env vars. No-op if disabled or missing keys."""

    def trace_run(self, run_id: str, task: str) -> RunTrace:
        """Start a trace for an orchestrator run."""

    def span(self, trace: RunTrace, name: str, metadata: dict = None) -> Span:
        """Create a span within a trace (plan, dispatch, execute, review, merge)."""

    def end_span(self, span: Span, output: dict = None):
        """End a span with optional output."""

    def end_trace(self, trace: RunTrace, status: str):
        """End the run trace."""
```

### Design Principles

- **No-op by default**: If LANGFUSE_PUBLIC_KEY not set, all methods are silent no-ops
- **No test dependency**: Tests mock langfuse — never call real API
- **Env vars**: LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY, LANGFUSE_HOST

### Dependencies

- `pip install langfuse` (add to pyproject.toml `[project.optional-dependencies] observability`)

### New Tests

- test_tracer_noop_when_disabled
- test_tracer_noop_when_missing_env
- test_trace_run_creates_trace
- test_span_creates_child_span
- test_end_trace_flushes
