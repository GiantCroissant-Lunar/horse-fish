# Gate-Failure Retry Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** When validation gates fail in `_review()`, send failure feedback to the still-alive agent, let it fix the issues, then re-run gates — instead of immediately failing the run.

**Architecture:** Add a `gate_retry_count` field to Subtask (separate from stall `retry_count`). In `_review()`, when gates fail and retries remain, build a fix prompt from gate output, send it to the agent via `send_task`, reset subtask to `running`, and transition run back to `executing`. The existing `_execute()` poll loop handles re-entry correctly.

**Tech Stack:** Python, asyncio, Pydantic models, pytest

---

### Task 1: Add `gate_retry_count` to Subtask model

**Files:**
- Modify: `src/horse_fish/models.py:71-81`
- Test: `tests/test_engine.py`

**Step 1: Write the failing test**

In `tests/test_engine.py`, add:

```python
def test_subtask_has_gate_retry_fields():
    """Test Subtask has gate_retry_count and max_gate_retries fields."""
    subtask = Subtask.create("test")
    assert subtask.gate_retry_count == 0
    assert subtask.max_gate_retries == 1
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_engine.py::test_subtask_has_gate_retry_fields -v`
Expected: FAIL with "AttributeError"

**Step 3: Write minimal implementation**

In `src/horse_fish/models.py`, add two fields to `Subtask`:

```python
class Subtask(BaseModel):
    # ... existing fields ...
    gate_retry_count: int = 0
    max_gate_retries: int = 1
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_engine.py::test_subtask_has_gate_retry_fields -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/horse_fish/models.py tests/test_engine.py
git commit -m "feat: add gate_retry_count to Subtask model"
```

---

### Task 2: Add `build_fix_prompt` to prompt.py

**Files:**
- Modify: `src/horse_fish/agents/prompt.py`
- Test: `tests/test_prompt.py`

**Step 1: Write the failing test**

In `tests/test_prompt.py`, add (or create if needed):

```python
from horse_fish.agents.prompt import build_fix_prompt


def test_build_fix_prompt_contains_gate_output():
    """Test fix prompt includes gate failure output and worktree path."""
    result = build_fix_prompt(
        gate_output="ruff-check: F401 unused import 'os'",
        worktree_path="/tmp/wt",
        branch="feat-x",
    )
    assert "F401 unused import" in result
    assert "/tmp/wt" in result
    assert "fix" in result.lower()
    assert "commit" in result.lower()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompt.py::test_build_fix_prompt_contains_gate_output -v`
Expected: FAIL with "ImportError" or "cannot import name"

**Step 3: Write minimal implementation**

In `src/horse_fish/agents/prompt.py`, add:

```python
FIX_PROMPT_TEMPLATE = """Your previous changes failed the following quality gates:

{gate_output}

## Worktree Information
- Worktree path: {worktree_path}
- Branch: {branch}

## Instructions
1. Fix ALL issues listed above.
2. Run `ruff check --fix src/ tests/` and `ruff format src/ tests/`.
3. Run `pytest tests/` to verify tests pass.
4. Commit your fixes when done.
"""


def build_fix_prompt(
    gate_output: str,
    worktree_path: str,
    branch: str,
) -> str:
    """Build a prompt telling the agent to fix gate failures."""
    return FIX_PROMPT_TEMPLATE.format(
        gate_output=gate_output,
        worktree_path=worktree_path,
        branch=branch,
    )
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_prompt.py::test_build_fix_prompt_contains_gate_output -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/horse_fish/agents/prompt.py tests/test_prompt.py
git commit -m "feat: add build_fix_prompt for gate-failure feedback"
```

---

### Task 3: Add gate retry logic to `_review()` in engine.py

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py:367-396`
- Test: `tests/test_engine.py`

**Step 1: Write the failing test — retry on gate failure**

In `tests/test_engine.py`, add:

```python
@pytest.mark.asyncio
async def test_review_retries_on_gate_failure(orchestrator, mock_pool, mock_gates):
    """Test that _review sends fix prompt and returns to executing when gates fail and retries remain."""
    from horse_fish.validation.gates import GateResult

    subtask = Subtask.create("do something")
    subtask.state = SubtaskState.done
    subtask.agent = "agent-1"
    subtask.gate_retry_count = 0
    subtask.max_gate_retries = 1

    run = Run.create("test task")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/test-worktree",
        branch="feat-test",
    )
    mock_pool._get_slot.return_value = slot

    # Gates fail
    mock_gates.auto_fix_and_commit = AsyncMock(
        return_value=GateResult(gate="auto-fix", passed=True, output="ok", duration_seconds=0.1)
    )
    failed_gate = GateResult(gate="ruff-check", passed=False, output="F401 unused import", duration_seconds=0.1)
    mock_gates.run_all = AsyncMock(return_value=[failed_gate])
    mock_gates.all_passed = MagicMock(return_value=False)

    # Agent is still alive
    mock_pool.check_status = AsyncMock(return_value=AgentState.busy)
    mock_pool.send_task = AsyncMock()

    result = await orchestrator._review(run)

    # Should transition back to executing, not failed
    assert result.state == RunState.executing
    assert subtask.state == SubtaskState.running
    assert subtask.gate_retry_count == 1
    # Should have sent fix prompt to agent
    mock_pool.send_task.assert_called_once()
    call_args = mock_pool.send_task.call_args
    assert "F401 unused import" in call_args[0][1]  # prompt contains gate output
```

**Step 2: Write another test — exhausted retries still fails**

```python
@pytest.mark.asyncio
async def test_review_fails_when_gate_retries_exhausted(orchestrator, mock_pool, mock_gates):
    """Test that _review fails the run when gate retries are exhausted."""
    from horse_fish.validation.gates import GateResult

    subtask = Subtask.create("do something")
    subtask.state = SubtaskState.done
    subtask.agent = "agent-1"
    subtask.gate_retry_count = 1
    subtask.max_gate_retries = 1  # Already at max

    run = Run.create("test task")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/test-worktree",
    )
    mock_pool._get_slot.return_value = slot

    mock_gates.auto_fix_and_commit = AsyncMock(
        return_value=GateResult(gate="auto-fix", passed=True, output="ok", duration_seconds=0.1)
    )
    failed_gate = GateResult(gate="pytest", passed=False, output="2 failed", duration_seconds=1.0)
    mock_gates.run_all = AsyncMock(return_value=[failed_gate])
    mock_gates.all_passed = MagicMock(return_value=False)

    result = await orchestrator._review(run)

    # Should fail — no retries left
    assert result.state == RunState.failed
    assert subtask.state == SubtaskState.failed
```

**Step 3: Write test — dead agent skips retry**

```python
@pytest.mark.asyncio
async def test_review_skips_retry_when_agent_dead(orchestrator, mock_pool, mock_gates):
    """Test that _review doesn't retry when agent tmux session is dead."""
    from horse_fish.validation.gates import GateResult

    subtask = Subtask.create("do something")
    subtask.state = SubtaskState.done
    subtask.agent = "agent-1"
    subtask.gate_retry_count = 0
    subtask.max_gate_retries = 1

    run = Run.create("test task")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/test-worktree",
    )
    mock_pool._get_slot.return_value = slot

    mock_gates.auto_fix_and_commit = AsyncMock(
        return_value=GateResult(gate="auto-fix", passed=True, output="ok", duration_seconds=0.1)
    )
    failed_gate = GateResult(gate="pytest", passed=False, output="1 failed", duration_seconds=1.0)
    mock_gates.run_all = AsyncMock(return_value=[failed_gate])
    mock_gates.all_passed = MagicMock(return_value=False)

    # Agent is dead
    mock_pool.check_status = AsyncMock(return_value=AgentState.dead)

    result = await orchestrator._review(run)

    # Should fail — can't retry with dead agent
    assert result.state == RunState.failed
    assert subtask.state == SubtaskState.failed
```

**Step 4: Run tests to verify they fail**

Run: `pytest tests/test_engine.py -k "gate_retry or gate_retries or agent_dead" -v`
Expected: FAIL (3 tests fail — `gate_retry_count` attribute exists from Task 1, but `_review()` doesn't retry yet)

**Step 5: Implement gate retry in `_review()`**

Replace the `_review()` method in `src/horse_fish/orchestrator/engine.py`:

```python
async def _review(self, run: Run) -> Run:
    """Run validation gates on each completed subtask's worktree.

    If gates fail and the agent is alive with retries remaining,
    send fix feedback and return to executing state.
    """
    all_passed = True
    needs_re_execute = False

    for subtask in run.subtasks:
        if subtask.state != SubtaskState.done or not subtask.agent:
            continue

        try:
            slot = self._pool._get_slot(subtask.agent)
            if not slot.worktree_path:
                continue

            # Auto-fix lint before running gates
            fix_result = await self._gates.auto_fix_and_commit(slot.worktree_path)
            if not fix_result.passed:
                logger.warning("Auto-fix failed for subtask %s: %s", subtask.id, fix_result.output)

            results = await self._gates.run_all(slot.worktree_path)
            if self._gates.all_passed(results):
                continue

            # Gates failed — try retry
            gate_output = "; ".join(f"{r.gate}: {r.output}" for r in results if not r.passed)
            logger.warning("Subtask %s failed gates: %s", subtask.id, gate_output)

            if subtask.gate_retry_count < subtask.max_gate_retries:
                # Check agent is still alive
                agent_status = await self._pool.check_status(subtask.agent)
                if agent_status != AgentState.dead:
                    # Send fix prompt to agent
                    from horse_fish.agents.prompt import build_fix_prompt

                    fix_prompt = build_fix_prompt(
                        gate_output=gate_output,
                        worktree_path=slot.worktree_path,
                        branch=slot.branch or "",
                    )
                    await self._pool.send_task(subtask.agent, fix_prompt)
                    subtask.state = SubtaskState.running
                    subtask.gate_retry_count += 1
                    subtask.last_activity_at = datetime.now(UTC)
                    self._persist_subtask(subtask, run.id)
                    needs_re_execute = True
                    logger.info(
                        "Sent fix prompt to agent for subtask %s (gate retry %d/%d)",
                        subtask.id, subtask.gate_retry_count, subtask.max_gate_retries,
                    )
                    continue

            # No retries left or agent dead
            subtask.state = SubtaskState.failed
            self._persist_subtask(subtask, run.id)
            all_passed = False

        except Exception as exc:
            logger.error("Review failed for subtask %s: %s", subtask.id, exc)
            subtask.state = SubtaskState.failed
            self._persist_subtask(subtask, run.id)
            all_passed = False

    if needs_re_execute:
        run.state = RunState.executing
        return run

    run.state = RunState.merging if all_passed else RunState.failed
    return run
```

**Step 6: Run tests to verify they pass**

Run: `pytest tests/test_engine.py -v`
Expected: ALL PASS

**Step 7: Run existing review test still passes**

Run: `pytest tests/test_engine.py::test_review_calls_auto_fix_before_run_all -v`
Expected: PASS (existing behavior preserved)

**Step 8: Commit**

```bash
git add src/horse_fish/orchestrator/engine.py tests/test_engine.py
git commit -m "feat: add gate-failure retry in review phase"
```

---

### Task 4: Update `send_task` to handle fix prompts (use raw prompt, not build_prompt wrapper)

**Files:**
- Modify: `src/horse_fish/agents/pool.py:90-103`
- Test: `tests/test_engine.py`

**Step 1: Analyze the issue**

Currently `send_task()` wraps every prompt with `build_prompt()` (adds worktree info, rules). For fix prompts, this double-wraps because `build_fix_prompt` already includes worktree info. We need `send_task` to accept a `raw=True` parameter to skip wrapping.

**Step 2: Write the failing test**

```python
@pytest.mark.asyncio
async def test_send_task_raw_skips_prompt_wrapping():
    """Test that send_task with raw=True sends prompt as-is."""
    from unittest.mock import AsyncMock, MagicMock
    from horse_fish.agents.pool import AgentPool
    from horse_fish.models import AgentState

    store = MagicMock()
    store.fetchone = MagicMock(return_value={
        "id": "a1", "name": "test", "runtime": "claude", "model": "m",
        "capability": "builder", "state": "busy", "pid": 1,
        "tmux_session": "hf-test", "worktree_path": "/tmp/wt",
        "branch": "b", "task_id": None, "started_at": None, "idle_since": None,
    })
    store.execute = MagicMock()

    tmux = AsyncMock()
    worktrees = AsyncMock()

    pool = AgentPool(store=store, tmux=tmux, worktrees=worktrees)
    await pool.send_task("a1", "fix this", raw=True)

    # Should send "fix this" directly, not wrapped with build_prompt
    tmux.send_keys.assert_called_once()
    sent_text = tmux.send_keys.call_args[0][1]
    assert sent_text == "fix this"
    assert "## Worktree Information" not in sent_text
```

**Step 3: Run test to verify it fails**

Run: `pytest tests/test_engine.py::test_send_task_raw_skips_prompt_wrapping -v`
Expected: FAIL with "unexpected keyword argument 'raw'"

**Step 4: Implement raw parameter**

In `src/horse_fish/agents/pool.py`, modify `send_task`:

```python
async def send_task(self, agent_id: str, prompt: str, task_id: str | None = None, raw: bool = False) -> None:
    """Send a prompt to the agent's tmux session and mark it busy."""
    slot = self._get_slot(agent_id)
    if raw:
        full_prompt = prompt
    else:
        full_prompt = build_prompt(
            task=prompt,
            worktree_path=slot.worktree_path or "",
            branch=slot.branch or "",
            project_context=self._project_context,
        )
    await self._tmux.send_keys(slot.tmux_session, full_prompt)
    self._store.execute(
        "UPDATE agents SET state = ?, task_id = ? WHERE id = ?",
        (AgentState.busy, task_id, agent_id),
    )
```

**Step 5: Update `_review()` to use `raw=True`**

In `engine.py`, change the `send_task` call in `_review()`:

```python
await self._pool.send_task(subtask.agent, fix_prompt, raw=True)
```

**Step 6: Run tests**

Run: `pytest tests/test_engine.py -v`
Expected: ALL PASS

**Step 7: Commit**

```bash
git add src/horse_fish/agents/pool.py src/horse_fish/orchestrator/engine.py tests/test_engine.py
git commit -m "feat: add raw parameter to send_task for fix prompts"
```

---

### Task 5: Full integration verification

**Step 1: Run full test suite**

Run: `pytest tests/ -x -q`
Expected: ALL PASS (404+ tests)

**Step 2: Run lint**

Run: `ruff check src/ tests/ && ruff format --check src/ tests/`
Expected: No errors

**Step 3: Final commit if needed**

```bash
git add -A
git commit -m "chore: lint fixes for gate retry"
```

---

## State Transition Diagram (After)

```
planning → executing → reviewing → merging → completed
               ^            |
               |            v (if retries remain + agent alive)
               +--- executing (re-enter poll loop, wait for agent fix)
                            |
                            v (if no retries or agent dead)
                          failed
```

## Key Design Decisions

1. **Separate `gate_retry_count` from `retry_count`**: Stall retries (agent crash) and gate retries (bad code) are different failure modes. Sharing the counter would reduce total attempts.
2. **Liveness check before retry**: Don't send fix prompts to dead tmux sessions.
3. **`raw=True` on send_task**: Fix prompts already include worktree info from `build_fix_prompt`, so skip the `build_prompt` wrapper to avoid duplication.
4. **Re-enter `_execute()` poll loop**: The existing poll loop correctly handles partial state (skips `done` subtasks, waits for `running` ones). No changes needed to `_execute()`.
5. **Default `max_gate_retries=1`**: One retry attempt. Conservative — can increase later.
