# Review Gate Auto-Fix & Pipeline Hardening

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make `hf run` complete e2e by auto-fixing lint in the review gate, adding ruff instructions to agent prompts, and strengthening SOLO bias in SmartPlanner.

**Architecture:** Three surgical changes. (1) ValidationGates gets an `auto_fix()` method that runs `ruff check --fix` + `ruff format` and git-commits any changes. Orchestrator calls it before `run_all()`. (2) Agent prompt template adds a ruff rule. (3) SmartPlanner classify prompt gets stronger SOLO bias.

**Tech Stack:** Python 3.12, asyncio, ruff, git, pytest

---

### Task 1: Add `auto_fix` method to ValidationGates

**Files:**
- Modify: `src/horse_fish/validation/gates.py`
- Test: `tests/test_validation.py`

**Step 1: Write the failing test**

Add to `tests/test_validation.py`:

```python
@pytest.mark.asyncio
async def test_auto_fix_runs_ruff_fix_and_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """auto_fix should run ruff check --fix and ruff format."""
    calls: list = []
    processes = [
        FakeProcess(returncode=0, stdout="1 fix applied"),  # ruff check --fix
        FakeProcess(returncode=0, stdout="1 file reformatted"),  # ruff format
    ]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.auto_fix(tmp_path)

    assert result.passed is True
    assert len(calls) == 2
    # First call: ruff check --fix
    assert calls[0][0][0] == "ruff"
    assert "check" in calls[0][0]
    assert "--fix" in calls[0][0]
    # Second call: ruff format
    assert calls[1][0][0] == "ruff"
    assert "format" in calls[1][0]
    assert "--check" not in calls[1][0]


@pytest.mark.asyncio
async def test_auto_fix_returns_failed_on_unfixable_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """auto_fix should return failed if ruff check --fix exits non-zero."""
    calls: list = []
    processes = [
        FakeProcess(returncode=1, stdout="src/foo.py:1:1: F841 local variable 'x' is assigned but never used"),
    ]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.auto_fix(tmp_path)

    assert result.passed is False
    assert "F841" in result.output
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_validation.py::test_auto_fix_runs_ruff_fix_and_format tests/test_validation.py::test_auto_fix_returns_failed_on_unfixable_errors -v`
Expected: FAIL with `AttributeError: 'ValidationGates' object has no attribute 'auto_fix'`

**Step 3: Implement `auto_fix` in ValidationGates**

Add to `src/horse_fish/validation/gates.py`, after `all_passed`:

```python
async def auto_fix(self, worktree_path: str | Path) -> GateResult:
    """Run ruff check --fix and ruff format to auto-fix lint issues.

    Returns a GateResult indicating whether all auto-fixes succeeded.
    """
    worktree_path = Path(worktree_path)
    start = time.monotonic()
    outputs: list[str] = []

    # Step 1: ruff check --fix
    proc = await asyncio.create_subprocess_exec(
        "ruff", "check", "--fix", "src/", "tests/",
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode().strip() or stderr.decode().strip()
    if output:
        outputs.append(output)
    if proc.returncode != 0:
        duration = time.monotonic() - start
        return GateResult(gate="auto-fix", passed=False, output="\n".join(outputs), duration_seconds=duration)

    # Step 2: ruff format
    proc = await asyncio.create_subprocess_exec(
        "ruff", "format", "src/", "tests/",
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode().strip() or stderr.decode().strip()
    if output:
        outputs.append(output)

    duration = time.monotonic() - start
    return GateResult(gate="auto-fix", passed=True, output="\n".join(outputs), duration_seconds=duration)
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_validation.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/horse_fish/validation/gates.py tests/test_validation.py
git commit -m "feat: add auto_fix method to ValidationGates"
```

---

### Task 2: Add `auto_fix_and_commit` to ValidationGates

**Files:**
- Modify: `src/horse_fish/validation/gates.py`
- Test: `tests/test_validation.py`

**Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_auto_fix_and_commit_commits_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """auto_fix_and_commit should git add + commit after successful auto-fix."""
    calls: list = []
    processes = [
        FakeProcess(returncode=0, stdout="1 fix applied"),   # ruff check --fix
        FakeProcess(returncode=0, stdout="1 file reformatted"),  # ruff format
        FakeProcess(returncode=0, stdout=""),   # git add
        FakeProcess(returncode=0, stdout=""),   # git diff --cached --quiet (has changes, rc=1 means changes)
    ]
    # git diff --cached --quiet returns 1 when there ARE staged changes
    processes[3] = FakeProcess(returncode=1, stdout="")
    processes.append(FakeProcess(returncode=0, stdout=""))  # git commit
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.auto_fix_and_commit(tmp_path)

    assert result.passed is True
    # Verify git commit was called
    git_calls = [c for c in calls if c[0][0] == "git"]
    assert len(git_calls) >= 2  # git add + git diff + git commit


@pytest.mark.asyncio
async def test_auto_fix_and_commit_skips_commit_when_no_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """auto_fix_and_commit should skip git commit if nothing changed."""
    calls: list = []
    processes = [
        FakeProcess(returncode=0, stdout=""),  # ruff check --fix (nothing to fix)
        FakeProcess(returncode=0, stdout=""),  # ruff format (nothing to format)
        FakeProcess(returncode=0, stdout=""),  # git add
        FakeProcess(returncode=0, stdout=""),  # git diff --cached --quiet (no changes, rc=0)
    ]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.auto_fix_and_commit(tmp_path)

    assert result.passed is True
    # No git commit call (only git add + git diff)
    git_calls = [c for c in calls if c[0][0] == "git"]
    assert len(git_calls) == 2  # git add + git diff only
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_validation.py::test_auto_fix_and_commit_commits_changes tests/test_validation.py::test_auto_fix_and_commit_skips_commit_when_no_changes -v`
Expected: FAIL with `AttributeError`

**Step 3: Implement `auto_fix_and_commit`**

Add to `src/horse_fish/validation/gates.py`, after `auto_fix`:

```python
async def auto_fix_and_commit(self, worktree_path: str | Path) -> GateResult:
    """Run auto_fix, then git add + commit if anything changed."""
    worktree_path = Path(worktree_path)
    fix_result = await self.auto_fix(worktree_path)
    if not fix_result.passed:
        return fix_result

    start = time.monotonic()
    outputs = [fix_result.output] if fix_result.output else []

    # git add -A
    proc = await asyncio.create_subprocess_exec(
        "git", "add", "-A",
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()

    # Check if there are staged changes
    proc = await asyncio.create_subprocess_exec(
        "git", "diff", "--cached", "--quiet",
        cwd=str(worktree_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    has_changes = proc.returncode != 0

    if has_changes:
        proc = await asyncio.create_subprocess_exec(
            "git", "commit", "-m", "chore: auto-fix lint",
            cwd=str(worktree_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        commit_output = stdout.decode().strip() or stderr.decode().strip()
        if commit_output:
            outputs.append(commit_output)

    duration = fix_result.duration_seconds + (time.monotonic() - start)
    return GateResult(gate="auto-fix", passed=True, output="\n".join(outputs), duration_seconds=duration)
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_validation.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/horse_fish/validation/gates.py tests/test_validation.py
git commit -m "feat: add auto_fix_and_commit to ValidationGates"
```

---

### Task 3: Wire auto-fix into orchestrator `_review`

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py`
- Test: `tests/test_engine.py`

**Step 1: Write the failing test**

Add to `tests/test_engine.py`:

```python
@pytest.mark.asyncio
async def test_review_calls_auto_fix_before_gates(mock_pool, mock_planner, mock_gates):
    """Review should call auto_fix_and_commit before running validation gates."""
    orchestrator = Orchestrator(
        pool=mock_pool,
        planner=mock_planner,
        gates=mock_gates,
        runtime="claude",
    )

    subtask = Subtask.create("do something")
    subtask.state = SubtaskState.done
    subtask.agent = "agent-1"

    run = Run.create("test")
    run.subtasks = [subtask]
    run.state = RunState.reviewing

    slot = AgentSlot(
        id="agent-1",
        name="hf-test",
        runtime="claude",
        model="claude-sonnet-4.6",
        capability="builder",
        state=AgentState.busy,
        worktree_path="/tmp/worktree",
    )
    mock_pool._get_slot = MagicMock(return_value=slot)

    # Track call order
    call_order = []
    mock_gates.auto_fix_and_commit = AsyncMock(
        side_effect=lambda p: (call_order.append("auto_fix"), GateResult("auto-fix", True, "", 0.1))[-1]
    )
    mock_gates.run_all = AsyncMock(
        side_effect=lambda p: (call_order.append("run_all"), [])[-1]
    )
    mock_gates.all_passed = MagicMock(return_value=True)

    result = await orchestrator._review(run)

    assert call_order == ["auto_fix", "run_all"]
    assert result.state == RunState.merging
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_engine.py::test_review_calls_auto_fix_before_gates -v`
Expected: FAIL (auto_fix_and_commit not called)

**Step 3: Modify `_review` in engine.py**

Change `_review` (lines 367-392) to call `auto_fix_and_commit` before `run_all`:

```python
async def _review(self, run: Run) -> Run:
    """Run auto-fix then validation gates on each completed subtask's worktree."""
    all_passed = True
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
            if not self._gates.all_passed(results):
                subtask.state = SubtaskState.failed
                self._persist_subtask(subtask, run.id)
                all_passed = False
                gate_output = "; ".join(f"{r.gate}: {r.output}" for r in results if not r.passed)
                logger.warning("Subtask %s failed gates: %s", subtask.id, gate_output)
        except Exception as exc:
            logger.error("Review failed for subtask %s: %s", subtask.id, exc)
            subtask.state = SubtaskState.failed
            self._persist_subtask(subtask, run.id)
            all_passed = False

    run.state = RunState.merging if all_passed else RunState.failed
    return run
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_engine.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/horse_fish/orchestrator/engine.py tests/test_engine.py
git commit -m "feat: wire auto-fix into orchestrator review phase"
```

---

### Task 4: Add ruff instruction to agent prompt template

**Files:**
- Modify: `src/horse_fish/agents/prompt.py`
- Test: `tests/test_prompt.py`

**Step 1: Write the failing test**

Add to `tests/test_prompt.py`:

```python
def test_build_prompt_includes_ruff_instruction() -> None:
    """Verify ruff check --fix instruction appears in rules."""
    result = build_prompt(task="test", worktree_path="/tmp", branch="main")
    assert "ruff check --fix" in result
    assert "ruff format" in result
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_prompt.py::test_build_prompt_includes_ruff_instruction -v`
Expected: FAIL

**Step 3: Update PROMPT_TEMPLATE**

In `src/horse_fish/agents/prompt.py`, update the Rules section:

```python
PROMPT_TEMPLATE = """You are an agent in the horse-fish swarm working in an isolated git worktree.

## Worktree Information
- Worktree path: {worktree_path}
- Branch: {branch}

{project_context_section}
## Task Description
{task}

## Rules
1. Run pytest to verify your changes pass tests.
2. Run `ruff check --fix src/ tests/` and `ruff format src/ tests/` before committing.
3. Commit your changes when done.
4. Stay focused on the task at hand.
5. Do not modify files outside your assigned scope.
"""
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_prompt.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/horse_fish/agents/prompt.py tests/test_prompt.py
git commit -m "feat: add ruff lint instruction to agent prompt template"
```

---

### Task 5: Strengthen SOLO bias in SmartPlanner classify prompt

**Files:**
- Modify: `src/horse_fish/planner/smart.py`
- Test: `tests/test_smart_planner.py`

**Step 1: Write the failing test**

Add to `tests/test_smart_planner.py`:

```python
def test_classify_prompt_contains_solo_bias():
    """Classify prompt should instruct to default to SOLO."""
    from horse_fish.planner.smart import _CLASSIFY_PROMPT
    assert "default" in _CLASSIFY_PROMPT.lower() or "Default" in _CLASSIFY_PROMPT
    assert "SOLO" in _CLASSIFY_PROMPT
    # Should mention that single-feature tasks are SOLO
    assert "single" in _CLASSIFY_PROMPT.lower()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_smart_planner.py::test_classify_prompt_contains_solo_bias -v`
Expected: May pass partially (SOLO is already there), but the "default" assertion should fail

**Step 3: Update `_CLASSIFY_PROMPT` in smart.py**

Replace lines 15-27:

```python
_CLASSIFY_PROMPT = """\
Estimate the complexity of this coding task. Default to SOLO unless clearly wrong.

- SOLO: One feature, one bug fix, one refactor. Even if it touches 2-3 files in the same component. \
A single agent handles everything. Most tasks are SOLO.
- TRIO: Truly independent changes across 2-3 separate components that benefit from parallel work. \
NOT just "multiple files" — only if the changes have zero coupling.
- SQUAD: 5+ independent components, large-scale refactor across the entire codebase.

When in doubt, choose SOLO. Over-decomposition wastes more time than under-decomposition.

{lessons}

Task: {task}
Context: {context}

Reply with ONLY one word: SOLO, TRIO, or SQUAD.
"""
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_smart_planner.py -v`
Expected: All pass

**Step 5: Commit**

```bash
git add src/horse_fish/planner/smart.py tests/test_smart_planner.py
git commit -m "feat: strengthen SOLO bias in SmartPlanner classify prompt"
```

---

### Task 6: Run full test suite and verify

**Step 1: Run all tests**

Run: `pytest tests/ -v`
Expected: All 397+ tests pass

**Step 2: Run ruff**

Run: `ruff check src/ tests/ && ruff format --check src/ tests/`
Expected: Clean

**Step 3: Final commit if needed**

Any remaining fixes.
