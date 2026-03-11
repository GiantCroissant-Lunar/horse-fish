# Cognee Minus Kuzu + Middleware Chain Design

Date: 2026-03-11
Session: 25

## Overview

Two changes:
1. Remove Kuzu graph layer from Cognee integration (keep vector-only search)
2. Extract cross-cutting concerns from Orchestrator into a state-level middleware chain

## Part 1: Cognee Minus Kuzu

### Problem

Kuzu's single-process write model causes lock contention. Zombie processes block all subsequent runs. The graph layer adds complexity (temporal cognify, LLM-powered entity extraction) but we haven't needed graph traversal in practice — our actual usage is flat fact retrieval via vector similarity.

### Change

- **Remove**: `cognee.cognify()` calls, `temporal_cognify`, Kuzu graph config, custom endpoint monkey-patch (only needed for cognify's LLM calls)
- **Keep**: `cognee.add()` (content + embeddings → LanceDB), `cognee.search()` (vector-only), FastEmbed embeddings, fallback LLM chain, write-fast/organize-later pattern
- **Switch**: Search type from `GRAPH_COMPLETION` to `CHUNKS` (pure vector search)

### Files

- `src/horse_fish/memory/cognee_store.py` — remove cognify calls, switch search type, remove Kuzu config
- `tests/test_cognee_memory.py` — update expectations (no cognify, no temporal, vector search type)

### What Stays Unchanged

- `src/horse_fish/memory/store.py` — SQLite memory_entries (unchanged)
- `src/horse_fish/memory/lessons.py` — deterministic lesson extraction (unchanged)
- CLI commands (`hf memory organize/search/status`) — same interface, just no graph
- SmartPlanner integration — `find_similar_tasks()` still works (vector search)
- Write-fast/organize-later pattern — `batch_ingest()` still calls `cognee.add()`

## Part 2: State-Level Middleware Chain

### Problem

The `run()` loop in engine.py has cross-cutting concerns (tracing, persistence, logging, memory, context passing) inlined around handler calls. This makes the loop hard to extend and couples concerns together.

### Design

#### Protocol

```python
Handler = Callable[[Task], Awaitable[Task]]

class Middleware(Protocol):
    async def __call__(
        self, run: Task, next: Handler, ctx: MiddlewareContext
    ) -> Task: ...

@dataclass
class MiddlewareContext:
    """Shared context passed through the middleware chain."""
    orchestrator: Orchestrator
    trace: Any | None = None  # Active Langfuse trace
    context_brief: ContextBrief | None = None  # Scout output
```

#### Onion Model

```
request → Tracing → LogContext → Persistence → Memory → ScoutContext → handler(run) → response
                                                                          ↑
                                                        each middleware calls next(run)
```

#### 5 Middleware

1. **TracingMiddleware** — create span before handler, end span after with state/subtask metadata
2. **LogContextMiddleware** — `set_log_context(run_id, state)` before, `clear_log_context()` after
3. **PersistenceMiddleware** — `_persist_run(run)` after handler returns
4. **MemoryMiddleware** — on terminal state (completed): call `_learn(run)` for memory storage
5. **ScoutContextMiddleware** — after scout handler: store `context_brief` in ctx; before plan handler: inject brief into SmartPlanner

#### Chain Composition

```python
def compose_chain(middlewares: list[Middleware], handler: Handler) -> Handler:
    """Compose middleware list into a single callable chain."""
    chain = handler
    for mw in reversed(middlewares):
        prev = chain
        chain = lambda run, _mw=mw, _prev=prev, ctx=ctx: _mw(run, _prev, ctx)
    return chain
```

#### Simplified run() Loop

```python
async def run(self, task: str) -> Task:
    run = Task.create(task)
    ctx = MiddlewareContext(orchestrator=self, trace=trace)

    while run.state not in TERMINAL_STATES:
        handler = self._handlers[run.state]
        chain = compose_chain(self._middlewares, handler)
        run = await chain(run)

    return run
```

### Files

- `src/horse_fish/orchestrator/middleware.py` — new: Protocol, MiddlewareContext, 5 implementations, compose_chain
- `src/horse_fish/orchestrator/engine.py` — simplify run() loop, move cross-cutting logic out
- `tests/test_middleware.py` — new: unit tests per middleware + chain ordering + compose tests

### Migration Strategy

1. Create middleware.py with Protocol and compose_chain
2. Extract TracingMiddleware first (most isolated)
3. Extract remaining 4 one at a time, running tests after each
4. Simplify run() loop to use chain
5. Verify all 630+ tests still pass
