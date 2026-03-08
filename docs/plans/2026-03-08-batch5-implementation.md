# Batch 5 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make horse-fish DAG execution robust (ID-based deps), add observability (hf logs), and prove the system works end-to-end.

**Architecture:** Three independent tasks that can be swarmed in parallel. Task 1 touches orchestrator + planner. Task 2 touches CLI. Task 3 is a new test file.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, click, tmux

---

### Task 1: ID-based deps for blocker-aware dispatch

Currently `_deps_met()` matches deps by description strings — fragile if planner wording varies. Switch to ID-based deps with a resolution step after planning.

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py:336-341` (`_deps_met` method)
- Modify: `src/horse_fish/orchestrator/engine.py:157-173` (`_plan` method)
- Modify: `src/horse_fish/planner/decompose.py:105-118` (`_parse_response` method)
- Test: `tests/test_orchestrator.py`

**Step 1: Write failing tests for ID-based deps**

Add these tests to `tests/test_orchestrator.py`:

```python
def test_deps_met_by_id():
    """Test _deps_met matches deps by subtask ID, not description."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="aaa", description="Build foundation", state=SubtaskState.done),
        Subtask(id="bbb", description="Add API layer", deps=["aaa"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[1]) is True


def test_deps_met_by_id_not_done():
    """Test _deps_met returns False when dep ID is not done."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="aaa", description="Build foundation", state=SubtaskState.pending),
        Subtask(id="bbb", description="Add API layer", deps=["aaa"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[1]) is False


def test_deps_met_by_id_multiple():
    """Test _deps_met with multiple ID deps, one not done."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="aaa", description="Task A", state=SubtaskState.done),
        Subtask(id="bbb", description="Task B", state=SubtaskState.pending),
        Subtask(id="ccc", description="Task C", deps=["aaa", "bbb"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[2]) is False
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_orchestrator.py::test_deps_met_by_id tests/test_orchestrator.py::test_deps_met_by_id_not_done tests/test_orchestrator.py::test_deps_met_by_id_multiple -v`
Expected: FAIL (current `_deps_met` matches by description, not ID)

**Step 3: Update `_deps_met` to match by ID**

In `src/horse_fish/orchestrator/engine.py`, replace the `_deps_met` method:

```python
@staticmethod
def _deps_met(run: Run, subtask) -> bool:
    """Check if all dependencies of a subtask are done (deps are subtask IDs)."""
    if not subtask.deps:
        return True
    done_ids = {s.id for s in run.subtasks if s.state == SubtaskState.done}
    return all(dep in done_ids for dep in subtask.deps)
```

**Step 4: Add `_resolve_deps` to convert description-based deps to ID-based deps**

Add this method to `Orchestrator` class in `engine.py`:

```python
@staticmethod
def _resolve_deps(subtasks: list) -> None:
    """Convert description-based deps (from planner) to ID-based deps.

    The planner emits deps as description strings. This resolves them to
    subtask IDs for stable matching during execution.
    """
    desc_to_id = {s.description: s.id for s in subtasks}
    for subtask in subtasks:
        resolved = []
        for dep in subtask.deps:
            if dep in desc_to_id:
                resolved.append(desc_to_id[dep])
            else:
                # Keep as-is (might already be an ID or unresolvable)
                resolved.append(dep)
        subtask.deps = resolved
```

**Step 5: Call `_resolve_deps` in `_plan`**

In `_plan` method, add the call after setting subtasks:

```python
async def _plan(self, run: Run) -> Run:
    """Decompose the task into subtasks via the Planner."""
    try:
        subtasks = await self._planner.decompose(run.task)
    except Exception as exc:
        logger.error("Planning failed: %s", exc)
        run.state = RunState.failed
        return run

    if not subtasks:
        logger.error("Planner returned no subtasks")
        run.state = RunState.failed
        return run

    self._resolve_deps(subtasks)
    run.subtasks = subtasks
    run.state = RunState.executing
    return run
```

**Step 6: Write test for `_resolve_deps`**

```python
def test_resolve_deps_converts_descriptions_to_ids():
    """Test _resolve_deps converts description-based deps to IDs."""
    subtasks = [
        Subtask(id="aaa", description="Build foundation"),
        Subtask(id="bbb", description="Add API layer", deps=["Build foundation"]),
        Subtask(id="ccc", description="Add tests", deps=["Build foundation", "Add API layer"]),
    ]
    Orchestrator._resolve_deps(subtasks)
    assert subtasks[0].deps == []
    assert subtasks[1].deps == ["aaa"]
    assert subtasks[2].deps == ["aaa", "bbb"]


def test_resolve_deps_keeps_unknown_deps():
    """Test _resolve_deps keeps unresolvable deps as-is."""
    subtasks = [
        Subtask(id="aaa", description="Task A", deps=["nonexistent"]),
    ]
    Orchestrator._resolve_deps(subtasks)
    assert subtasks[0].deps == ["nonexistent"]
```

**Step 7: Update existing dep tests to use IDs**

The existing tests (`test_deps_met_all_done`, `test_deps_met_not_done`, `test_deps_met_partial`) use description-based deps. Update them to use ID-based deps:

```python
def test_deps_met_all_done():
    """Test _deps_met returns True when all deps are done."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="s1", description="Task 1", state=SubtaskState.done),
        Subtask(id="s2", description="Task 2", deps=["s1"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[1]) is True


def test_deps_met_not_done():
    """Test _deps_met returns False when deps are not done."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="s1", description="Task 1", state=SubtaskState.pending),
        Subtask(id="s2", description="Task 2", deps=["s1"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[1]) is False


def test_deps_met_partial():
    """Test _deps_met returns False when some deps are not done."""
    run = Run.create("Task")
    run.subtasks = [
        Subtask(id="s1", description="Task 1", state=SubtaskState.done),
        Subtask(id="s2", description="Task 2", state=SubtaskState.pending),
        Subtask(id="s3", description="Task 3", deps=["s1", "s2"]),
    ]
    assert Orchestrator._deps_met(run, run.subtasks[2]) is False
```

Also update `sample_subtasks` fixture and `test_execute_respects_dag_deps`:

```python
@pytest.fixture
def sample_subtasks():
    """Sample subtasks for testing."""
    return [
        Subtask(id="subtask-1", description="Implement user model"),
        Subtask(id="subtask-2", description="Create API endpoints", deps=["subtask-1"]),
    ]
```

```python
@pytest.mark.asyncio
async def test_execute_respects_dag_deps(orchestrator, mock_pool):
    """Test _execute respects DAG deps (blocked subtasks wait)."""
    subtask1 = Subtask(id="subtask-1", description="Implement base")
    subtask2 = Subtask(id="subtask-2", description="Build on base", deps=["subtask-1"])
    # ... rest unchanged
```

**Step 8: Run all tests**

Run: `pytest tests/test_orchestrator.py -v`
Expected: ALL PASS

**Step 9: Commit**

```bash
git add src/horse_fish/orchestrator/engine.py tests/test_orchestrator.py
git commit -m "feat: switch deps to ID-based matching with _resolve_deps"
```

---

### Task 2: CLI `hf logs` command

**Files:**
- Modify: `src/horse_fish/cli.py`
- Modify: `tests/test_cli.py`

**Step 1: Write failing tests**

Add to `tests/test_cli.py`:

```python
@patch("horse_fish.cli.TmuxManager")
def test_logs_lists_all_sessions(mock_tmux_class, runner):
    """Test 'hf logs' lists all tmux sessions with pane output."""
    mock_tmux = MagicMock()
    mock_tmux.list_sessions = AsyncMock(return_value=["hf-agent-1", "hf-agent-2", "unrelated-session"])
    mock_tmux.capture_pane = AsyncMock(side_effect=[
        "line1\nline2\nline3",
        "output from agent 2",
    ])
    mock_tmux_class.return_value = mock_tmux

    result = runner.invoke(main, ["logs"])

    assert result.exit_code == 0
    assert "hf-agent-1" in result.output
    assert "hf-agent-2" in result.output
    # Should only show hf- prefixed sessions
    assert "unrelated-session" not in result.output


@patch("horse_fish.cli.TmuxManager")
def test_logs_single_agent(mock_tmux_class, runner):
    """Test 'hf logs --agent NAME' shows output for specific agent."""
    mock_tmux = MagicMock()
    mock_tmux.capture_pane = AsyncMock(return_value="full output\nfrom agent")
    mock_tmux_class.return_value = mock_tmux

    result = runner.invoke(main, ["logs", "--agent", "hf-agent-1"])

    assert result.exit_code == 0
    assert "full output" in result.output
    assert "from agent" in result.output
    mock_tmux.capture_pane.assert_called_once_with("hf-agent-1")


@patch("horse_fish.cli.TmuxManager")
def test_logs_agent_not_found(mock_tmux_class, runner):
    """Test 'hf logs --agent NAME' when session doesn't exist."""
    mock_tmux = MagicMock()
    mock_tmux.capture_pane = AsyncMock(return_value=None)
    mock_tmux_class.return_value = mock_tmux

    result = runner.invoke(main, ["logs", "--agent", "nonexistent"])

    assert result.exit_code == 0
    assert "not found" in result.output.lower() or "no output" in result.output.lower()


@patch("horse_fish.cli.TmuxManager")
def test_logs_no_sessions(mock_tmux_class, runner):
    """Test 'hf logs' with no active hf sessions."""
    mock_tmux = MagicMock()
    mock_tmux.list_sessions = AsyncMock(return_value=["unrelated-session"])
    mock_tmux_class.return_value = mock_tmux

    result = runner.invoke(main, ["logs"])

    assert result.exit_code == 0
    assert "No active" in result.output
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cli.py::test_logs_lists_all_sessions tests/test_cli.py::test_logs_single_agent tests/test_cli.py::test_logs_agent_not_found tests/test_cli.py::test_logs_no_sessions -v`
Expected: FAIL

**Step 3: Implement `hf logs` command**

Add to `src/horse_fish/cli.py` after the `merge` command:

```python
@main.command()
@click.option("--agent", default=None, help="Show logs for a specific agent")
@click.option("--lines", default=20, type=int, help="Number of lines to show per agent")
def logs(agent: str | None, lines: int):
    """View agent tmux output."""
    tmux = TmuxManager()

    if agent:
        # Show specific agent
        output = asyncio.run(tmux.capture_pane(agent))
        if output is None:
            click.echo(f"Agent '{agent}' not found or no output available.")
            return
        tail = "\n".join(output.splitlines()[-lines:]) if output.strip() else "(empty)"
        click.echo(f"--- {agent} ---")
        click.echo(tail)
    else:
        # List all hf- sessions
        sessions = asyncio.run(tmux.list_sessions())
        hf_sessions = [s for s in sessions if s.startswith("hf-")]
        if not hf_sessions:
            click.echo("No active horse-fish agents.")
            return
        for session in hf_sessions:
            output = asyncio.run(tmux.capture_pane(session))
            if output is None:
                continue
            tail = "\n".join(output.splitlines()[-lines:]) if output.strip() else "(empty)"
            click.echo(f"--- {session} ---")
            click.echo(tail)
            click.echo()
```

**Step 4: Run tests**

Run: `pytest tests/test_cli.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add src/horse_fish/cli.py tests/test_cli.py
git commit -m "feat: add hf logs command to view agent tmux output"
```

---

### Task 3: End-to-end integration test

Real subprocess test proving tmux + worktree + merge flow works.

**Files:**
- Create: `tests/test_e2e.py`

**Step 1: Write the e2e test**

```python
"""End-to-end tests with real tmux + worktree (no mocked subprocesses).

Requires: tmux installed and available on PATH.
Skip if tmux not available.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.models import Subtask, SubtaskState
from horse_fish.orchestrator.engine import Orchestrator
from horse_fish.planner.decompose import Planner
from horse_fish.store.db import Store
from horse_fish.validation.gates import ValidationGates


def _tmux_available() -> bool:
    """Check if tmux is available."""
    try:
        result = subprocess.run(["tmux", "-V"], capture_output=True, timeout=5)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(not _tmux_available(), reason="tmux not available")


@pytest.fixture
def tmp_repo(tmp_path):
    """Create a temporary git repo with an initial commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)
    # Initial commit
    (repo / "README.md").write_text("# Test Repo")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, check=True)
    return repo


@pytest.fixture
def store(tmp_path):
    """Create a temporary SQLite store."""
    db_path = str(tmp_path / "test.db")
    s = Store(db_path)
    s.migrate()
    return s


@pytest.fixture
def tmux():
    """Real TmuxManager."""
    return TmuxManager()


@pytest.fixture
def worktrees(tmp_repo):
    """Real WorktreeManager."""
    return WorktreeManager(str(tmp_repo))


@pytest.fixture
def pool(store, tmux, worktrees):
    """Real AgentPool."""
    return AgentPool(store, tmux, worktrees)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_e2e_single_subtask_creates_file(tmp_repo, pool):
    """E2E: spawn agent that creates a file, verify it completes."""
    # Spawn an agent using a simple shell command that creates a file and commits
    slot = await pool.spawn(
        name="hf-e2e-test",
        runtime="claude",  # doesn't matter — we'll send raw shell commands
        model="test",
        capability="builder",
    )

    # Send a shell command that creates a file and commits it
    script = (
        f"cd {slot.worktree_path} && "
        "echo 'hello from e2e' > e2e_output.txt && "
        "git add e2e_output.txt && "
        "git commit -m 'e2e: create output file'"
    )
    await pool._tmux.send_keys(slot.tmux_session, script)

    # Wait for the commit to appear
    for _ in range(10):
        await asyncio.sleep(1)
        result = await pool.collect_result(slot.id)
        if result.diff:
            break

    assert result.diff, "Expected agent to produce a diff (commit)"
    assert result.success is True

    # Cleanup
    await pool.release(slot.id)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_e2e_worktree_isolation(tmp_repo, pool):
    """E2E: verify agents work in isolated worktrees, not the main repo."""
    slot = await pool.spawn(
        name="hf-e2e-isolation",
        runtime="claude",
        model="test",
        capability="builder",
    )

    # Verify worktree is separate from main repo
    assert slot.worktree_path is not None
    assert slot.worktree_path != str(tmp_repo)
    assert Path(slot.worktree_path).exists()

    # Verify main repo is untouched
    main_files = set(os.listdir(tmp_repo))
    assert "README.md" in main_files

    # Cleanup
    await pool.release(slot.id)
```

**Step 2: Run tests**

Run: `pytest tests/test_e2e.py -v --timeout=30`
Expected: PASS (if tmux available), SKIP (if not)

**Step 3: Commit**

```bash
git add tests/test_e2e.py
git commit -m "test: add e2e tests with real tmux + worktree"
```

---

### Task 4: .gitignore housekeeping (manual, not swarmed)

**Step 1: Create/update .gitignore**

```
__pycache__/
*.pyc
.env
.horse-fish/
*.egg-info/
dist/
build/
.pytest_cache/
```

**Step 2: Commit**

```bash
git add .gitignore
git commit -m "chore: add .gitignore for pycache, env, state dir"
```

---

## Swarm Assignment

| Task | Agent Name | Runtime | Independent? |
|------|-----------|---------|-------------|
| Task 1: ID-based deps | blocker-dispatch | Pi/qwen3.5-plus | Yes |
| Task 2: CLI logs | cli-logs | Pi/qwen3.5-plus | Yes |
| Task 3: E2e test | e2e-test | Pi/qwen3.5-plus | Yes |
| Task 4: .gitignore | (manual) | — | Yes |

All tasks are fully independent — safe for parallel execution.
