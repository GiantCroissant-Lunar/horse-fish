# Cognee Minus Kuzu + Middleware Chain Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove Kuzu graph layer from Cognee (keep vector-only search) and extract cross-cutting concerns from Orchestrator into a state-level middleware chain.

**Architecture:** Two independent changes. Part 1 modifies cognee_store.py to skip cognify() and use CHUNKS search. Part 2 creates a new middleware.py module with Protocol + 5 implementations, then refactors engine.py's run() loop to use the middleware chain.

**Tech Stack:** Python 3.12+, Cognee (vector-only), Pydantic, asyncio, pytest

---

### Task 1: Remove Kuzu from CogneeMemory

**Files:**
- Modify: `src/horse_fish/memory/cognee_store.py`

**Step 1: Update docstring and class doc**

Change the class docstring from mentioning Kuzu to vector-only:

```python
class CogneeMemory:
    """Orchestrator-level memory using Cognee vector search.

    Uses FastEmbed (CPU embeddings) and LanceDB (vector store).
    Graph layer (Kuzu) is disabled — pure vector similarity search.
    LLM fallback chain: Mercury 2 → Dashscope (qwen3.5-plus).
    """
```

**Step 2: Remove Kuzu config and monkey-patch from _configure()**

Remove these lines from `_configure()`:
```python
# Remove:
cognee.config.set_graph_database_provider("kuzu")
cognee.config.system_root_directory(str(self._data_dir))
self._patch_custom_endpoint()
```

Remove the entire `_patch_custom_endpoint()` static method (lines 121-144).

Keep: FastEmbed env vars, LanceDB config, LLM config, `COGNEE_SKIP_CONNECTION_TEST`.

**Step 3: Remove cognify() from ingest()**

Change `ingest()` to only call `cognee.add()`:

```python
async def ingest(self, content: str, metadata: dict[str, Any] | None = None) -> None:
    """Add content to Cognee vector store.

    Calls cognee.add() to store content with embeddings.
    Uses dataset_name from metadata (default: "general").
    """
    self._ensure_configured()

    dataset = (metadata or {}).get("dataset", "general")
    await cognee.add(content, dataset_name=dataset)
```

**Step 4: Remove cognify() from ingest_run_result()**

Remove the cognify block at the end (lines 233-239). Keep the `cognee.add()` calls with node_sets.

```python
async def ingest_run_result(self, run: Task, subtask_results: list[SubtaskResult]) -> None:
    """Ingest a completed run into vector store using structured node_sets."""
    self._ensure_configured()

    task_summary = f"Task: {run.task}\nState: {run.state}\nSubtasks: {len(run.subtasks)}"
    await cognee.add(task_summary, dataset_name="run_results", node_set=["task_summaries"])

    for result in subtask_results:
        subtask_content = f"Subtask {result.subtask_id}:\n  Success: {result.success}\n  Output: {result.output}"
        await cognee.add(subtask_content, dataset_name="run_results", node_set=["subtask_outcomes"])

        if result.diff:
            await cognee.add(result.diff, dataset_name="run_results", node_set=["code_diffs"])
```

**Step 5: Switch search from GRAPH_COMPLETION to CHUNKS**

In `_search_cognee()`:

```python
async def _search_cognee(
    self, query: str, top_k: int = 5, timeout: float = 60.0, **extra_kwargs: Any
) -> list[CogneeHit]:
    """Shared search implementation with vector similarity (CHUNKS)."""
    self._ensure_configured()

    kwargs: dict[str, Any] = {"query_text": query, **extra_kwargs}
    if SearchType:
        kwargs["query_type"] = SearchType.CHUNKS

    results = await asyncio.wait_for(cognee.search(**kwargs), timeout=timeout)
    return [self._parse_result(r) for r in results[:top_k]]
```

Update docstrings on `search()` and `find_similar_tasks()` to say "vector similarity" instead of "knowledge graph".

**Step 6: Remove cognify() from batch_ingest()**

Remove the cognify block per domain (lines 280-285). Keep `cognee.add()` calls:

```python
for domain, domain_entries in by_domain.items():
    try:
        for entry in domain_entries:
            await cognee.add(entry.content, dataset_name=domain, node_set=[domain])
        ingested_count += len(domain_entries)
    except Exception as exc:
        logger.warning("Failed to ingest entries for domain %s: %s", domain, exc)
```

**Step 7: Remove LLM config from _configure() since it's only needed for cognify/graph search**

Actually, keep the LLM config — `COGNEE_SKIP_CONNECTION_TEST` and LLM config are needed even for `cognee.add()` initialization. But remove `use_fallback` parameter and fallback re-configure logic since we no longer retry cognify.

Simplify `_configure()`:
```python
def _configure(self) -> None:
    """Configure Cognee providers. Lazy — called on first use."""
    self._data_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("EMBEDDING_PROVIDER", "fastembed")
    os.environ.setdefault("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
    os.environ.setdefault("EMBEDDING_DIMENSIONS", "384")
    os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"

    cognee.config.set_vector_db_provider("lancedb")
    cognee.config.set_vector_db_url(str(self._data_dir / "lancedb"))

    # LLM still needed for cognee initialization
    api_key = self._llm_api_key or self._fallback_llm_api_key
    model = self._llm_model if self._llm_api_key else self._fallback_llm_model
    endpoint = self._llm_endpoint if self._llm_api_key else self._fallback_llm_endpoint
    if api_key:
        cognee.config.set_llm_config({
            "llm_provider": "custom",
            "llm_api_key": api_key,
            "llm_model": model,
            "llm_endpoint": endpoint,
        })

    self._configured = True
```

Update `_ensure_configured()` to remove `use_fallback`:
```python
def _ensure_configured(self) -> None:
    if not self._configured:
        self._configure()
```

**Step 8: Run tests to verify it fails**

Run: `pytest tests/test_cognee_memory.py -v`
Expected: Several failures (tests expect cognify calls, GRAPH_COMPLETION, temporal)

**Step 9: Commit**

```bash
git add src/horse_fish/memory/cognee_store.py
git commit -m "refactor: remove Kuzu graph layer from CogneeMemory (vector-only)"
```

---

### Task 2: Update Cognee Tests

**Files:**
- Modify: `tests/test_cognee_memory.py`

**Step 1: Update TestCogneeSearchType to expect CHUNKS**

```python
class TestCogneeSearchType:
    """Tests that search uses CHUNKS (vector-only)."""

    @pytest.mark.asyncio
    async def test_search_uses_chunks(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.search = AsyncMock(return_value=[])
            mock_cognee.config = MagicMock()
            mock_search_type = MagicMock()
            with patch("horse_fish.memory.cognee_store.SearchType", mock_search_type):
                await mem.search("test query")

            call_kwargs = mock_cognee.search.call_args
            assert call_kwargs is not None
            assert call_kwargs.kwargs.get("query_type") == mock_search_type.CHUNKS
```

**Step 2: Update TestCogneeDatasets — remove cognify expectations**

In `test_ingest_uses_dataset_name` and `test_ingest_uses_custom_dataset`:
- Remove `mock_cognee.cognify = AsyncMock()` setup
- Remove any assertions on `cognify`

**Step 3: Remove TestCogneeTemporalCognify class entirely**

Delete the whole class — temporal cognify is gone.

**Step 4: Update TestCogneeStructuredIngestion — remove cognify**

In `test_ingest_run_result_uses_node_sets`:
- Remove `mock_cognee.cognify = AsyncMock()`
- Keep assertions on `cognee.add()` calls with node_sets

**Step 5: Update TestCogneeMemoryIngest**

In `test_ingest_calls_cognee_add_and_cognify`:
- Rename to `test_ingest_calls_cognee_add`
- Remove `mock_cognee.cognify = AsyncMock()` and `mock_cognee.cognify.assert_awaited_once()`
- Only assert `mock_cognee.add.assert_awaited_once()`

In `test_ingest_run_result_structured_content`:
- Remove `mock_cognee.cognify = AsyncMock()`

**Step 6: Remove TestCogneeMemoryFallback class entirely**

The fallback was only for cognify retries — no longer needed.

**Step 7: Run tests**

Run: `pytest tests/test_cognee_memory.py -v`
Expected: All pass

Run: `pytest tests/ -q --timeout=60 -k "not test_store_default_data_dir and not test_smoke"`
Expected: 630+ pass, 0 fail

**Step 8: Commit**

```bash
git add tests/test_cognee_memory.py
git commit -m "test: update cognee tests for vector-only search (no Kuzu)"
```

---

### Task 3: Create Middleware Protocol and compose_chain

**Files:**
- Create: `src/horse_fish/orchestrator/middleware.py`
- Create: `tests/test_middleware.py`

**Step 1: Write the failing test for compose_chain**

```python
"""Tests for orchestrator middleware chain."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock

import pytest

from horse_fish.models import Task, TaskState
from horse_fish.orchestrator.middleware import Middleware, MiddlewareContext, compose_chain


@pytest.mark.asyncio
async def test_compose_chain_calls_handler():
    """compose_chain with no middleware calls handler directly."""
    handler = AsyncMock(side_effect=lambda run: _transition(run, TaskState.planning))
    ctx = MiddlewareContext()

    chain = compose_chain([], handler, ctx)
    run = Task.create("test")
    result = await chain(run)

    handler.assert_awaited_once_with(run)
    assert result.state == TaskState.planning


@pytest.mark.asyncio
async def test_compose_chain_middleware_order():
    """Middleware executes in order: first registered runs outermost."""
    call_order: list[str] = []

    async def mw_a(run: Task, next_handler, ctx: MiddlewareContext) -> Task:
        call_order.append("a_before")
        result = await next_handler(run)
        call_order.append("a_after")
        return result

    async def mw_b(run: Task, next_handler, ctx: MiddlewareContext) -> Task:
        call_order.append("b_before")
        result = await next_handler(run)
        call_order.append("b_after")
        return result

    handler = AsyncMock(side_effect=lambda run: _transition(run, TaskState.executing))
    ctx = MiddlewareContext()

    chain = compose_chain([mw_a, mw_b], handler, ctx)
    await chain(Task.create("test"))

    assert call_order == ["a_before", "b_before", "b_after", "a_after"]


def _transition(run: Task, state: TaskState) -> Task:
    run.state = state
    return run
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_middleware.py -v`
Expected: FAIL (module not found)

**Step 3: Write middleware.py with Protocol and compose_chain**

```python
"""State-level middleware chain for the Orchestrator.

Middleware wraps each state handler call (scout, plan, execute, review, merge)
with cross-cutting concerns like tracing, persistence, and logging.

Execution model: onion/nested — first middleware registered runs outermost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from horse_fish.models import Task

if TYPE_CHECKING:
    from horse_fish.observability.traces import Tracer
    from horse_fish.models import ContextBrief

Handler = Callable[[Task], Any]  # async (Task) -> Task


@dataclass
class MiddlewareContext:
    """Shared mutable context passed through the middleware chain."""

    trace: Any | None = None
    context_brief: Any | None = None  # ContextBrief from scout phase
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Middleware(Protocol):
    """Protocol for state-level middleware."""

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task: ...


def compose_chain(
    middlewares: list[Middleware], handler: Handler, ctx: MiddlewareContext
) -> Handler:
    """Compose a list of middleware around a handler into a single callable.

    Middleware[0] is outermost (runs first on entry, last on exit).
    """
    chain = handler
    for mw in reversed(middlewares):
        prev = chain

        async def _wrapped(run: Task, _mw=mw, _prev=prev) -> Task:
            return await _mw(run, _prev, ctx)

        chain = _wrapped
    return chain
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_middleware.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/horse_fish/orchestrator/middleware.py tests/test_middleware.py
git commit -m "feat: add middleware Protocol and compose_chain"
```

---

### Task 4: Implement TracingMiddleware

**Files:**
- Modify: `src/horse_fish/orchestrator/middleware.py`
- Modify: `tests/test_middleware.py`

**Step 1: Write the failing test**

```python
from horse_fish.orchestrator.middleware import TracingMiddleware


@pytest.mark.asyncio
async def test_tracing_middleware_creates_and_ends_span():
    """TracingMiddleware creates span before handler, ends after."""
    tracer = AsyncMock()
    trace = object()
    span = object()
    tracer.span.return_value = span

    mw = TracingMiddleware(tracer)
    ctx = MiddlewareContext(trace=trace)
    handler = AsyncMock(side_effect=lambda run: _transition(run, TaskState.planning))

    run = Task.create("test")
    result = await mw(run, handler, ctx)

    tracer.span.assert_called_once_with(trace, TaskState.scouting.value)
    handler.assert_awaited_once()
    tracer.end_span.assert_called_once()
    assert result.state == TaskState.planning


@pytest.mark.asyncio
async def test_tracing_middleware_skips_when_no_tracer():
    """TracingMiddleware is a no-op when tracer is None."""
    mw = TracingMiddleware(None)
    ctx = MiddlewareContext()
    handler = AsyncMock(side_effect=lambda run: _transition(run, TaskState.planning))

    run = Task.create("test")
    result = await mw(run, handler, ctx)

    handler.assert_awaited_once()
    assert result.state == TaskState.planning
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_middleware.py::test_tracing_middleware_creates_and_ends_span -v`
Expected: FAIL (TracingMiddleware not found)

**Step 3: Implement TracingMiddleware**

Add to `middleware.py`:

```python
class TracingMiddleware:
    """Creates and ends a trace span around each state handler."""

    def __init__(self, tracer: Tracer | None) -> None:
        self._tracer = tracer

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task:
        if not self._tracer or not ctx.trace:
            return await next(run)

        span = self._tracer.span(ctx.trace, run.state.value)
        result = await next(run)
        if span:
            self._tracer.end_span(
                span,
                {"state": result.state.value},
                metadata={"subtask_count": len(result.subtasks)},
            )
        return result
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_middleware.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/horse_fish/orchestrator/middleware.py tests/test_middleware.py
git commit -m "feat: add TracingMiddleware"
```

---

### Task 5: Implement LogContextMiddleware

**Files:**
- Modify: `src/horse_fish/orchestrator/middleware.py`
- Modify: `tests/test_middleware.py`

**Step 1: Write the failing test**

```python
from unittest.mock import patch
from horse_fish.orchestrator.middleware import LogContextMiddleware


@pytest.mark.asyncio
async def test_log_context_middleware_sets_and_clears():
    """LogContextMiddleware sets log context before handler, clears after executing."""
    mw = LogContextMiddleware()
    ctx = MiddlewareContext()

    run = Task.create("test")
    handler = AsyncMock(side_effect=lambda r: _transition(r, TaskState.executing))

    with patch("horse_fish.orchestrator.middleware.set_log_context") as mock_set, \
         patch("horse_fish.orchestrator.middleware.clear_log_context") as mock_clear:
        result = await mw(run, handler, ctx)

    handler.assert_awaited_once()
    # Should have called set_log_context at least once
    mock_set.assert_called()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_middleware.py::test_log_context_middleware_sets_and_clears -v`
Expected: FAIL

**Step 3: Implement LogContextMiddleware**

```python
from horse_fish.observability.log_context import clear_log_context, set_log_context
from horse_fish.models import TaskState

class LogContextMiddleware:
    """Sets structured log context around state handlers."""

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task:
        set_log_context(run_id=run.id, state=run.state.value)
        result = await next(run)
        if result.state == TaskState.executing:
            clear_log_context()
            set_log_context(run_id=run.id)
        return result
```

**Step 4: Run tests**

Run: `pytest tests/test_middleware.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/horse_fish/orchestrator/middleware.py tests/test_middleware.py
git commit -m "feat: add LogContextMiddleware"
```

---

### Task 6: Implement PersistenceMiddleware

**Files:**
- Modify: `src/horse_fish/orchestrator/middleware.py`
- Modify: `tests/test_middleware.py`

**Step 1: Write the failing test**

```python
from horse_fish.orchestrator.middleware import PersistenceMiddleware


@pytest.mark.asyncio
async def test_persistence_middleware_persists_after_handler():
    """PersistenceMiddleware calls persist_fn after handler returns."""
    persist_fn = AsyncMock()
    mw = PersistenceMiddleware(persist_fn)
    ctx = MiddlewareContext()
    handler = AsyncMock(side_effect=lambda run: _transition(run, TaskState.planning))

    run = Task.create("test")
    result = await mw(run, handler, ctx)

    handler.assert_awaited_once()
    persist_fn.assert_awaited_once_with(result)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_middleware.py::test_persistence_middleware_persists_after_handler -v`
Expected: FAIL

**Step 3: Implement PersistenceMiddleware**

```python
class PersistenceMiddleware:
    """Persists run state to SQLite after each state transition."""

    def __init__(self, persist_fn: Callable[[Task], Any]) -> None:
        self._persist = persist_fn

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task:
        result = await next(run)
        await self._persist(result)
        return result
```

**Step 4: Run tests**

Run: `pytest tests/test_middleware.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/horse_fish/orchestrator/middleware.py tests/test_middleware.py
git commit -m "feat: add PersistenceMiddleware"
```

---

### Task 7: Implement MemoryMiddleware

**Files:**
- Modify: `src/horse_fish/orchestrator/middleware.py`
- Modify: `tests/test_middleware.py`

**Step 1: Write the failing test**

```python
from horse_fish.orchestrator.middleware import MemoryMiddleware


@pytest.mark.asyncio
async def test_memory_middleware_calls_learn_on_completed():
    """MemoryMiddleware calls learn_fn when run reaches completed state."""
    learn_fn = AsyncMock()
    mw = MemoryMiddleware(learn_fn)
    ctx = MiddlewareContext()
    handler = AsyncMock(side_effect=lambda run: _transition(run, TaskState.completed))

    run = Task.create("test")
    run.state = TaskState.merging
    result = await mw(run, handler, ctx)

    learn_fn.assert_awaited_once_with(result)


@pytest.mark.asyncio
async def test_memory_middleware_skips_non_terminal():
    """MemoryMiddleware does not call learn_fn for non-terminal states."""
    learn_fn = AsyncMock()
    mw = MemoryMiddleware(learn_fn)
    ctx = MiddlewareContext()
    handler = AsyncMock(side_effect=lambda run: _transition(run, TaskState.executing))

    run = Task.create("test")
    result = await mw(run, handler, ctx)

    learn_fn.assert_not_awaited()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_middleware.py::test_memory_middleware_calls_learn_on_completed -v`
Expected: FAIL

**Step 3: Implement MemoryMiddleware**

```python
class MemoryMiddleware:
    """Stores run results in memory when task completes successfully."""

    def __init__(self, learn_fn: Callable[[Task], Any]) -> None:
        self._learn = learn_fn

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task:
        result = await next(run)
        if result.state == TaskState.completed:
            await self._learn(result)
        return result
```

**Step 4: Run tests**

Run: `pytest tests/test_middleware.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/horse_fish/orchestrator/middleware.py tests/test_middleware.py
git commit -m "feat: add MemoryMiddleware"
```

---

### Task 8: Implement ScoutContextMiddleware

**Files:**
- Modify: `src/horse_fish/orchestrator/middleware.py`
- Modify: `tests/test_middleware.py`

**Step 1: Write the failing test**

```python
from horse_fish.orchestrator.middleware import ScoutContextMiddleware


@pytest.mark.asyncio
async def test_scout_context_middleware_stores_brief():
    """ScoutContextMiddleware stores context_brief in ctx after scout phase."""
    brief = object()  # Mock brief
    mw = ScoutContextMiddleware(get_brief_fn=lambda: brief)
    ctx = MiddlewareContext()

    run = Task.create("test")  # starts in scouting state
    handler = AsyncMock(side_effect=lambda run: _transition(run, TaskState.planning))

    result = await mw(run, handler, ctx)

    assert ctx.context_brief is brief


@pytest.mark.asyncio
async def test_scout_context_middleware_skips_non_scout():
    """ScoutContextMiddleware does nothing for non-scout states."""
    mw = ScoutContextMiddleware(get_brief_fn=lambda: None)
    ctx = MiddlewareContext()

    run = Task.create("test")
    run.state = TaskState.planning
    handler = AsyncMock(side_effect=lambda run: _transition(run, TaskState.executing))

    result = await mw(run, handler, ctx)

    assert ctx.context_brief is None
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_middleware.py::test_scout_context_middleware_stores_brief -v`
Expected: FAIL

**Step 3: Implement ScoutContextMiddleware**

```python
class ScoutContextMiddleware:
    """Carries context_brief from scout phase to subsequent handlers."""

    def __init__(self, get_brief_fn: Callable[[], Any]) -> None:
        self._get_brief = get_brief_fn

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task:
        result = await next(run)
        if run.state == TaskState.scouting:
            ctx.context_brief = self._get_brief()
        return result
```

**Step 4: Run tests**

Run: `pytest tests/test_middleware.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/horse_fish/orchestrator/middleware.py tests/test_middleware.py
git commit -m "feat: add ScoutContextMiddleware"
```

---

### Task 9: Wire Middleware Chain into Orchestrator

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py`

**Step 1: Add middleware imports and chain setup in __init__**

Add to imports:
```python
from horse_fish.orchestrator.middleware import (
    MiddlewareContext,
    TracingMiddleware,
    LogContextMiddleware,
    PersistenceMiddleware,
    MemoryMiddleware,
    ScoutContextMiddleware,
    compose_chain,
)
```

Add at end of `__init__()`:
```python
# Build middleware chain
self._middlewares: list = [
    TracingMiddleware(tracer),
    LogContextMiddleware(),
    PersistenceMiddleware(self._async_persist_run),
    MemoryMiddleware(self._learn),
    ScoutContextMiddleware(lambda: self._context_brief),
]
```

Add a thin async wrapper for `_persist_run`:
```python
async def _async_persist_run(self, run: Task) -> None:
    """Async wrapper for _persist_run (middleware expects async)."""
    self._persist_run(run)
```

**Step 2: Simplify run() loop**

Replace the current run() loop body (lines 323-343) with:

```python
try:
    while run.state not in (TaskState.completed, TaskState.failed, TaskState.partial_success):
        handler = self._handlers.get(run.state)
        if handler is None:
            raise OrchestratorError(f"No handler for state {run.state}")

        ctx = MiddlewareContext(trace=trace, context_brief=self._context_brief)
        chain = compose_chain(self._middlewares, handler, ctx)
        run = await chain(run)
        # Update context_brief from middleware if scout produced one
        if ctx.context_brief is not None:
            self._context_brief = ctx.context_brief

        logger.info("Run %s transitioned to %s", run.id, run.state)
finally:
    run.completed_at = datetime.now(UTC)
    self._persist_run(run)
    reset_log_context(*context_tokens)

    if self._tracer and trace:
        self._score_run_outcomes(run, trace)
        self._tracer.end_trace(
            trace,
            run.state.value,
            output=self._trace_output(run),
        )
    self._active_trace = None
```

Remove the inlined tracing span create/end, `_persist_run(run)`, log context management, and the `_learn()` call from after the finally block (it's now in MemoryMiddleware).

**Step 3: Run full test suite**

Run: `pytest tests/ -q --timeout=60 -k "not test_store_default_data_dir and not test_smoke"`
Expected: 630+ pass, 0 fail

If tests fail, debug and fix. Common issues:
- `_persist_run` is sync but middleware expects async — use the `_async_persist_run` wrapper
- `_learn` might need to handle the case where memory is None
- Context brief passing may need adjustment

**Step 4: Commit**

```bash
git add src/horse_fish/orchestrator/engine.py
git commit -m "refactor: wire middleware chain into Orchestrator.run() loop"
```

---

### Task 10: Final Verification and Cleanup

**Files:**
- All modified files

**Step 1: Run full test suite**

Run: `pytest tests/ -q --timeout=60 -k "not test_store_default_data_dir and not test_smoke"`
Expected: 630+ pass, 0 fail

**Step 2: Lint**

Run: `ruff check src/ tests/`
Expected: All checks passed

**Step 3: Format**

Run: `ruff format src/ tests/`

**Step 4: Verify no regressions in orchestrator tests specifically**

Run: `pytest tests/test_orchestrator.py tests/test_engine.py tests/test_cognee_memory.py tests/test_cognee_orchestrator.py tests/test_middleware.py -v --timeout=60`
Expected: All pass

**Step 5: Commit any cleanup**

```bash
git add -u
git commit -m "chore: lint and format after middleware refactor"
```
