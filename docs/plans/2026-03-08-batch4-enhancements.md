# Batch 4: Orchestrator Enhancements Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Wire pending integrations (Tracer, MemoryStore) and add three new capabilities borrowed from Symphony/Cognee (stall detection, per-state concurrency, provenance stamping).

**Architecture:** All five tasks modify the orchestrator engine and/or models. Tasks are independent — no ordering dependencies. Each adds tests first (TDD), then implementation, then wiring.

**Tech Stack:** Python 3.12+, Pydantic, asyncio, pytest, pytest-asyncio

---

## Task 1: Wire Tracer into Orchestrator

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py`
- Modify: `src/horse_fish/cli.py`
- Test: `tests/test_orchestrator.py`

**Step 1: Write failing test — tracer span per state handler**

```python
# In tests/test_orchestrator.py

@pytest.fixture
def mock_tracer():
    """Mock Tracer."""
    from horse_fish.observability.traces import RunTrace, Span, Tracer
    tracer = MagicMock(spec=Tracer)
    trace = RunTrace(run_id="test", task="test task")
    span = Span(name="test", trace=trace)
    tracer.trace_run.return_value = trace
    tracer.span.return_value = span
    return tracer


@pytest.mark.asyncio
async def test_run_creates_trace_and_spans(mock_pool, mock_planner, mock_gates, mock_tracer):
    """Test run() creates a trace and spans for each phase."""
    mock_planner.decompose.return_value = [
        Subtask(id="subtask-1", description="Task 1"),
    ]
    slot = AgentSlot(
        id="agent-1", name="hf-subtask-1", runtime="claude",
        model="claude-sonnet-4.6", capability="builder",
        state=AgentState.busy, worktree_path="/tmp/wt",
    )
    mock_pool.spawn.return_value = slot
    mock_pool.send_task = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="subtask-1", success=True, output="Done",
            diff="commit", duration_seconds=10.0,
        )
    )
    mock_pool._get_slot.return_value = slot
    mock_gates.run_all = AsyncMock(
        return_value=[GateResult(gate="compile", passed=True, output="ok", duration_seconds=1.0)]
    )
    mock_pool._worktrees.merge = AsyncMock(return_value=True)

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates,
        runtime="claude", tracer=mock_tracer,
    )

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator.run("Build system")

    assert result.state == RunState.completed
    mock_tracer.trace_run.assert_called_once()
    # Should have spans for plan, execute, review, merge
    assert mock_tracer.span.call_count == 4
    assert mock_tracer.end_span.call_count == 4
    mock_tracer.end_trace.assert_called_once_with(mock_tracer.trace_run.return_value, "completed")


@pytest.mark.asyncio
async def test_run_ends_trace_on_failure(mock_pool, mock_planner, mock_gates, mock_tracer):
    """Test run() ends trace with 'failed' on failure."""
    mock_planner.decompose.side_effect = Exception("LLM error")

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates,
        runtime="claude", tracer=mock_tracer,
    )
    result = await orchestrator.run("Build system")

    assert result.state == RunState.failed
    mock_tracer.end_trace.assert_called_once_with(mock_tracer.trace_run.return_value, "failed")
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_orchestrator.py::test_run_creates_trace_and_spans -v`
Expected: FAIL — `Orchestrator.__init__() got an unexpected keyword argument 'tracer'`

**Step 3: Implement — add Tracer to Orchestrator**

In `src/horse_fish/orchestrator/engine.py`:

```python
# Add import at top
from horse_fish.observability.traces import Tracer

# Add tracer param to __init__
def __init__(
    self,
    pool: AgentPool,
    planner: Planner,
    gates: ValidationGates,
    runtime: str = "claude",
    model: str | None = None,
    max_agents: int = 3,
    selector: AgentSelector | None = None,
    merge_queue: MergeQueue | None = None,
    tracer: Tracer | None = None,
) -> None:
    # ... existing code ...
    self._tracer = tracer

# Modify run() to create trace and wrap handlers with spans
async def run(self, task: str) -> Run:
    run = Run.create(task)
    logger.info("Starting run %s for task: %s", run.id, task)

    trace = self._tracer.trace_run(run.id, task) if self._tracer else None

    while run.state not in (RunState.completed, RunState.failed):
        handler = self._handlers.get(run.state)
        if handler is None:
            raise OrchestratorError(f"No handler for state {run.state}")

        span = self._tracer.span(trace, run.state.value) if self._tracer and trace else None
        run = await handler(run)
        if self._tracer and span:
            self._tracer.end_span(span, {"state": run.state.value})

        logger.info("Run %s transitioned to %s", run.id, run.state)

    run.completed_at = datetime.now(UTC)

    if self._tracer and trace:
        self._tracer.end_trace(trace, run.state.value)

    return run
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_orchestrator.py -v`
Expected: ALL PASS

**Step 5: Wire Tracer in CLI**

In `src/horse_fish/cli.py`, add to `_init_components`:

```python
from horse_fish.observability.traces import Tracer

# Inside _init_components, before creating orchestrator:
tracer = Tracer()

# Add tracer=tracer to Orchestrator constructor
```

**Step 6: Run full test suite**

Run: `pytest tests/ -v`
Expected: ALL PASS

**Step 7: Commit**

```bash
git add src/horse_fish/orchestrator/engine.py src/horse_fish/cli.py tests/test_orchestrator.py
git commit -m "feat: wire Tracer into Orchestrator with per-phase spans"
```

---

## Task 2: Wire MemoryStore into Orchestrator

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py`
- Modify: `src/horse_fish/cli.py`
- Test: `tests/test_orchestrator.py`

**Step 1: Write failing test — memory store wired on completion**

```python
# In tests/test_orchestrator.py

@pytest.fixture
def mock_memory():
    """Mock MemoryStore."""
    memory = AsyncMock()
    memory.store_run_result = AsyncMock()
    memory.find_similar_tasks = AsyncMock(return_value=[])
    return memory


@pytest.mark.asyncio
async def test_run_stores_result_in_memory_on_completion(mock_pool, mock_planner, mock_gates, mock_memory):
    """Test run() stores result in memory when completed."""
    mock_planner.decompose.return_value = [
        Subtask(id="subtask-1", description="Task 1"),
    ]
    slot = AgentSlot(
        id="agent-1", name="hf-subtask-1", runtime="claude",
        model="claude-sonnet-4.6", capability="builder",
        state=AgentState.busy, worktree_path="/tmp/wt",
    )
    mock_pool.spawn.return_value = slot
    mock_pool.send_task = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    result_obj = SubtaskResult(
        subtask_id="subtask-1", success=True, output="Done",
        diff="commit", duration_seconds=10.0,
    )
    mock_pool.collect_result = AsyncMock(return_value=result_obj)
    mock_pool._get_slot.return_value = slot
    mock_gates.run_all = AsyncMock(
        return_value=[GateResult(gate="compile", passed=True, output="ok", duration_seconds=1.0)]
    )
    mock_pool._worktrees.merge = AsyncMock(return_value=True)

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates,
        runtime="claude", memory=mock_memory,
    )

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        run = await orchestrator.run("Build system")

    assert run.state == RunState.completed
    mock_memory.store_run_result.assert_called_once()
    call_args = mock_memory.store_run_result.call_args
    assert call_args[0][0].id == run.id  # first arg is the Run
    assert len(call_args[0][1]) == 1  # second arg is subtask_results list


@pytest.mark.asyncio
async def test_run_does_not_store_memory_on_failure(mock_pool, mock_planner, mock_gates, mock_memory):
    """Test run() does NOT store in memory when failed."""
    mock_planner.decompose.side_effect = Exception("LLM error")

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates,
        runtime="claude", memory=mock_memory,
    )
    run = await orchestrator.run("Build system")

    assert run.state == RunState.failed
    mock_memory.store_run_result.assert_not_called()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_orchestrator.py::test_run_stores_result_in_memory_on_completion -v`
Expected: FAIL — `Orchestrator.__init__() got an unexpected keyword argument 'memory'`

**Step 3: Implement — add MemoryStore to Orchestrator**

In `src/horse_fish/orchestrator/engine.py`:

```python
# Add import
from horse_fish.memory.store import MemoryStore

# Add memory param to __init__
def __init__(self, ..., memory: MemoryStore | None = None) -> None:
    # ... existing code ...
    self._memory = memory

# Add _learn() method
async def _learn(self, run: Run) -> None:
    """Store completed run results in memory for future learning."""
    if not self._memory:
        return
    subtask_results = [s.result for s in run.subtasks if s.result]
    try:
        await self._memory.store_run_result(run, subtask_results)
    except Exception as exc:
        logger.warning("Failed to store run in memory: %s", exc)

# Call _learn() in run() after completion, before return:
if run.state == RunState.completed:
    await self._learn(run)
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_orchestrator.py -v`
Expected: ALL PASS

**Step 5: Wire MemoryStore in CLI**

In `src/horse_fish/cli.py`:

```python
from horse_fish.memory.store import MemoryStore

# Inside _init_components:
memory = MemoryStore()

# Add memory=memory to Orchestrator constructor
```

**Step 6: Commit**

```bash
git add src/horse_fish/orchestrator/engine.py src/horse_fish/cli.py tests/test_orchestrator.py
git commit -m "feat: wire MemoryStore into Orchestrator for cross-session learning"
```

---

## Task 3: Stall Detection + Auto-Retry

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py`
- Modify: `src/horse_fish/models.py`
- Test: `tests/test_orchestrator.py`

**Step 1: Write failing test — stall detection model fields**

```python
# In tests/test_models.py

def test_subtask_retry_fields():
    """Test Subtask has retry_count and last_activity_at fields."""
    subtask = Subtask.create("Test task")
    assert subtask.retry_count == 0
    assert subtask.max_retries == 2
    assert subtask.last_activity_at is None
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py::test_subtask_retry_fields -v`
Expected: FAIL — `AttributeError: 'Subtask' object has no attribute 'retry_count'`

**Step 3: Add fields to Subtask model**

In `src/horse_fish/models.py`, add to Subtask:

```python
class Subtask(BaseModel):
    # ... existing fields ...
    retry_count: int = 0
    max_retries: int = 2
    last_activity_at: datetime | None = None
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py::test_subtask_retry_fields -v`
Expected: PASS

**Step 5: Write failing test — stall detection in execute**

```python
# In tests/test_orchestrator.py

@pytest.mark.asyncio
async def test_execute_detects_stalled_agent_and_retries(mock_pool, mock_planner, mock_gates):
    """Test _execute detects a stalled agent and retries the subtask."""
    from datetime import timedelta

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates,
        runtime="claude", stall_timeout_seconds=30,
    )

    subtask = Subtask(id="subtask-1", description="Task 1")
    # Simulate: subtask is running, last activity was 60 seconds ago (stalled)
    subtask.state = SubtaskState.running
    subtask.agent = "agent-1"
    subtask.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)

    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    # Mock: agent is alive but stalled (no new diff)
    mock_pool.check_status = AsyncMock(return_value=AgentState.busy)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="subtask-1", success=False, output="", diff="", duration_seconds=0,
        )
    )
    # After retry, agent completes
    slot = AgentSlot(
        id="agent-2", name="hf-retry", runtime="claude",
        model="claude-sonnet-4.6", capability="builder", state=AgentState.busy,
    )
    mock_pool.spawn.return_value = slot
    mock_pool.send_task = AsyncMock()
    mock_pool.release = AsyncMock()

    # Second poll: agent dies with result
    call_count = 0
    async def status_side_effect(agent_id):
        nonlocal call_count
        call_count += 1
        if call_count <= 1:
            return AgentState.busy  # First check: stalled
        return AgentState.dead  # After retry: completed

    mock_pool.check_status = AsyncMock(side_effect=status_side_effect)

    collect_count = 0
    async def collect_side_effect(agent_id):
        nonlocal collect_count
        collect_count += 1
        if collect_count <= 1:
            return SubtaskResult(subtask_id="subtask-1", success=False, output="", diff="", duration_seconds=0)
        return SubtaskResult(subtask_id="subtask-1", success=True, output="Done", diff="commit", duration_seconds=10)

    mock_pool.collect_result = AsyncMock(side_effect=collect_side_effect)

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    assert subtask.retry_count >= 1
    mock_pool.release.assert_called()  # Old agent was released


@pytest.mark.asyncio
async def test_execute_fails_after_max_retries(mock_pool, mock_planner, mock_gates):
    """Test _execute marks subtask failed after max retries exhausted."""
    from datetime import timedelta

    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates,
        runtime="claude", stall_timeout_seconds=30,
    )

    subtask = Subtask(id="subtask-1", description="Task 1", max_retries=0)
    subtask.state = SubtaskState.running
    subtask.agent = "agent-1"
    subtask.last_activity_at = datetime.now(UTC) - timedelta(seconds=60)

    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    mock_pool.check_status = AsyncMock(return_value=AgentState.busy)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(subtask_id="subtask-1", success=False, output="", diff="", duration_seconds=0)
    )
    mock_pool.release = AsyncMock()

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    assert result.state == RunState.failed
    assert subtask.state == SubtaskState.failed
```

**Step 6: Run test to verify it fails**

Run: `pytest tests/test_orchestrator.py::test_execute_detects_stalled_agent_and_retries -v`
Expected: FAIL — `Orchestrator.__init__() got an unexpected keyword argument 'stall_timeout_seconds'`

**Step 7: Implement stall detection in Orchestrator**

In `src/horse_fish/orchestrator/engine.py`:

```python
STALL_TIMEOUT_SECONDS = 300  # 5 minutes default

# Add to __init__:
def __init__(self, ..., stall_timeout_seconds: int = STALL_TIMEOUT_SECONDS) -> None:
    # ... existing ...
    self._stall_timeout = stall_timeout_seconds

# Add stall detection in the polling loop inside _execute, after checking running subtasks:
async def _check_stalls(self, run: Run, agent_map: dict[str, str]) -> int:
    """Check for stalled agents. Returns count of retried subtasks."""
    retried = 0
    now = datetime.now(UTC)

    for subtask in run.subtasks:
        if subtask.state != SubtaskState.running:
            continue
        if subtask.last_activity_at is None:
            continue

        elapsed = (now - subtask.last_activity_at).total_seconds()
        if elapsed < self._stall_timeout:
            continue

        logger.warning("Subtask %s stalled (%.0fs since last activity)", subtask.id, elapsed)

        agent_id = agent_map.get(subtask.id)
        if agent_id:
            try:
                await self._pool.release(agent_id)
            except Exception:
                pass

        if subtask.retry_count < subtask.max_retries:
            subtask.retry_count += 1
            subtask.state = SubtaskState.pending
            subtask.agent = None
            subtask.last_activity_at = None
            if subtask.id in agent_map:
                del agent_map[subtask.id]
            retried += 1
            logger.info("Retrying subtask %s (attempt %d/%d)", subtask.id, subtask.retry_count, subtask.max_retries)
        else:
            subtask.state = SubtaskState.failed
            logger.error("Subtask %s failed after %d retries", subtask.id, subtask.max_retries)

    return retried

# In _execute, set last_activity_at when dispatching:
subtask.last_activity_at = datetime.now(UTC)

# In _execute polling loop, update last_activity_at when diff detected:
subtask.last_activity_at = datetime.now(UTC)

# In _execute polling loop, call _check_stalls after checking running subtasks:
await self._check_stalls(run, agent_map)
```

**Step 8: Run tests**

Run: `pytest tests/test_orchestrator.py -v`
Expected: ALL PASS

**Step 9: Commit**

```bash
git add src/horse_fish/models.py src/horse_fish/orchestrator/engine.py tests/test_models.py tests/test_orchestrator.py
git commit -m "feat: add stall detection with auto-retry for hung agents"
```

---

## Task 4: Per-State Concurrency Limits

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py`
- Test: `tests/test_orchestrator.py`

**Step 1: Write failing test — per-state limits**

```python
# In tests/test_orchestrator.py

def test_orchestrator_accepts_per_state_limits(mock_pool, mock_planner, mock_gates):
    """Test Orchestrator accepts per-state concurrency limits."""
    limits = {RunState.executing: 5, RunState.reviewing: 2}
    orch = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates,
        runtime="claude", max_agents=3, concurrency_limits=limits,
    )
    assert orch._concurrency_limits[RunState.executing] == 5
    assert orch._concurrency_limits[RunState.reviewing] == 2


@pytest.mark.asyncio
async def test_execute_respects_per_state_concurrency_limit(mock_pool, mock_planner, mock_gates):
    """Test _execute uses per-state limit instead of global max_agents."""
    # Global max is 10 but execute-state limit is 1
    limits = {RunState.executing: 1}
    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates,
        runtime="claude", max_agents=10, concurrency_limits=limits,
    )

    subtask1 = Subtask(id="s1", description="Task 1")
    subtask2 = Subtask(id="s2", description="Task 2")
    run = Run.create("Build system")
    run.subtasks = [subtask1, subtask2]
    run.state = RunState.executing

    spawned_agents = []
    slot_counter = 0

    async def mock_spawn(**kwargs):
        nonlocal slot_counter
        slot_counter += 1
        slot = AgentSlot(
            id=f"agent-{slot_counter}", name=f"hf-{slot_counter}",
            runtime="claude", model="claude-sonnet-4.6",
            capability="builder", state=AgentState.busy,
        )
        spawned_agents.append(slot)
        return slot

    mock_pool.spawn = AsyncMock(side_effect=mock_spawn)
    mock_pool.send_task = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="test", success=True, output="Done",
            diff="commit", duration_seconds=10.0,
        )
    )

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    assert result.state == RunState.reviewing
    # Both subtasks complete, but only 1 at a time
    assert all(s.state == SubtaskState.done for s in run.subtasks)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_orchestrator.py::test_orchestrator_accepts_per_state_limits -v`
Expected: FAIL — `unexpected keyword argument 'concurrency_limits'`

**Step 3: Implement per-state concurrency limits**

In `src/horse_fish/orchestrator/engine.py`:

```python
# Add to __init__:
def __init__(
    self, ...,
    concurrency_limits: dict[RunState, int] | None = None,
) -> None:
    # ... existing ...
    self._concurrency_limits = concurrency_limits or {}

# In _execute, replace the max_agents check:
# OLD: if active_count >= self._max_agents:
# NEW:
max_concurrent = self._concurrency_limits.get(RunState.executing, self._max_agents)
# ... then use max_concurrent in the check:
if active_count >= max_concurrent:
    break
```

**Step 4: Run tests**

Run: `pytest tests/test_orchestrator.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/horse_fish/orchestrator/engine.py tests/test_orchestrator.py
git commit -m "feat: add per-state concurrency limits to Orchestrator"
```

---

## Task 5: Provenance Stamping on Artifacts

**Files:**
- Modify: `src/horse_fish/models.py`
- Modify: `src/horse_fish/orchestrator/engine.py`
- Test: `tests/test_models.py`
- Test: `tests/test_orchestrator.py`

**Step 1: Write failing test — provenance fields on SubtaskResult**

```python
# In tests/test_models.py

def test_subtask_result_provenance_fields():
    """Test SubtaskResult has provenance metadata."""
    result = SubtaskResult(
        subtask_id="s1",
        success=True,
        output="Done",
        diff="commit",
        duration_seconds=10.0,
        agent_id="agent-1",
        agent_runtime="claude",
        agent_model="claude-sonnet-4.6",
        run_id="run-1",
        completed_at=datetime.now(UTC),
    )
    assert result.agent_id == "agent-1"
    assert result.agent_runtime == "claude"
    assert result.agent_model == "claude-sonnet-4.6"
    assert result.run_id == "run-1"
    assert result.completed_at is not None


def test_subtask_result_provenance_defaults():
    """Test SubtaskResult provenance fields default to None."""
    result = SubtaskResult(
        subtask_id="s1", success=True, output="Done",
        diff="", duration_seconds=5.0,
    )
    assert result.agent_id is None
    assert result.agent_runtime is None
    assert result.agent_model is None
    assert result.run_id is None
    assert result.completed_at is None
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_models.py::test_subtask_result_provenance_fields -v`
Expected: FAIL — `unexpected keyword argument 'agent_id'`

**Step 3: Add provenance fields to SubtaskResult**

In `src/horse_fish/models.py`:

```python
class SubtaskResult(BaseModel):
    subtask_id: str
    success: bool
    output: str
    diff: str
    duration_seconds: float
    # Provenance fields (Cognee pattern)
    agent_id: str | None = None
    agent_runtime: str | None = None
    agent_model: str | None = None
    run_id: str | None = None
    completed_at: datetime | None = None
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_models.py -v`
Expected: ALL PASS

**Step 5: Write failing test — orchestrator stamps provenance**

```python
# In tests/test_orchestrator.py

@pytest.mark.asyncio
async def test_execute_stamps_provenance_on_results(mock_pool, mock_planner, mock_gates):
    """Test _execute stamps provenance metadata on SubtaskResults."""
    orchestrator = Orchestrator(
        pool=mock_pool, planner=mock_planner, gates=mock_gates,
        runtime="claude", model="claude-sonnet-4.6",
    )

    subtask = Subtask(id="subtask-1", description="Task 1")
    run = Run.create("Build system")
    run.subtasks = [subtask]
    run.state = RunState.executing

    slot = AgentSlot(
        id="agent-1", name="hf-subtask-1", runtime="claude",
        model="claude-sonnet-4.6", capability="builder",
        state=AgentState.busy,
    )
    mock_pool.spawn.return_value = slot
    mock_pool.send_task = AsyncMock()
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)
    mock_pool.collect_result = AsyncMock(
        return_value=SubtaskResult(
            subtask_id="subtask-1", success=True, output="Done",
            diff="commit", duration_seconds=10.0,
        )
    )

    async def mock_sleep(seconds):
        return None

    with pytest.MonkeyPatch().context() as m:
        m.setattr("horse_fish.orchestrator.engine.asyncio.sleep", mock_sleep)
        result = await orchestrator._execute(run)

    # Check provenance was stamped
    sr = subtask.result
    assert sr is not None
    assert sr.agent_id == "agent-1"
    assert sr.agent_runtime == "claude"
    assert sr.agent_model == "claude-sonnet-4.6"
    assert sr.run_id == run.id
    assert sr.completed_at is not None
```

**Step 6: Run test to verify it fails**

Run: `pytest tests/test_orchestrator.py::test_execute_stamps_provenance_on_results -v`
Expected: FAIL — `assert sr.agent_id == "agent-1"` (agent_id is None)

**Step 7: Implement provenance stamping in _execute**

In `src/horse_fish/orchestrator/engine.py`, after collecting a result in _execute:

```python
# After: subtask.result = result
# Add provenance stamping:
def _stamp_provenance(self, result: SubtaskResult, run: Run, agent_id: str) -> None:
    """Stamp provenance metadata on a SubtaskResult."""
    try:
        slot = self._pool._get_slot(agent_id)
        result.agent_id = slot.id
        result.agent_runtime = slot.runtime
        result.agent_model = slot.model
    except Exception:
        result.agent_id = agent_id
    result.run_id = run.id
    result.completed_at = datetime.now(UTC)

# Call after setting subtask.result in both the dead-agent and diff-detected paths:
self._stamp_provenance(result, run, agent_id)
```

**Step 8: Run tests**

Run: `pytest tests/ -v`
Expected: ALL PASS

**Step 9: Commit**

```bash
git add src/horse_fish/models.py src/horse_fish/orchestrator/engine.py tests/test_models.py tests/test_orchestrator.py
git commit -m "feat: add provenance stamping to SubtaskResult artifacts"
```

---

## Overstory Dispatch Guide

These 5 tasks are independent and can be dispatched to agents in parallel using overstory:

```bash
# Create tasks
sd create --title "Wire Tracer into Orchestrator" --description "Task 1 from docs/plans/2026-03-08-batch4-enhancements.md" --json
sd create --title "Wire MemoryStore into Orchestrator" --description "Task 2 from docs/plans/2026-03-08-batch4-enhancements.md" --json
sd create --title "Stall detection + auto-retry" --description "Task 3 from docs/plans/2026-03-08-batch4-enhancements.md" --json
sd create --title "Per-state concurrency limits" --description "Task 4 from docs/plans/2026-03-08-batch4-enhancements.md" --json
sd create --title "Provenance stamping on artifacts" --description "Task 5 from docs/plans/2026-03-08-batch4-enhancements.md" --json

# Sling to agents (3-5 at a time)
ov sling <task-1-id> --capability builder --runtime claude --name tracer-wire
ov sling <task-2-id> --capability builder --runtime claude --name memory-wire
ov sling <task-3-id> --capability builder --runtime pi --name stall-detect
ov sling <task-4-id> --capability builder --runtime pi --name concurrency-limits
ov sling <task-5-id> --capability builder --runtime pi --name provenance-stamp
```

**Merge order:** Any order works since all tasks are independent. However, tasks 1 and 2 both modify `engine.py` and `cli.py`, so merge those first to minimize conflicts. Tasks 3-5 also modify `engine.py` so expect minor merge conflicts on imports and `__init__` params.

**Recommended merge order:** 1 → 2 → 5 → 4 → 3 (ascending complexity of engine.py changes)
