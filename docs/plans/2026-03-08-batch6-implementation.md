# Batch 6: Self-Hosting Agent-Facing Improvements — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Close the gap between horse-fish and overstory by adding ready detection, prompt injection, and Pi runtime fixes — enabling horse-fish to drive its own development.

**Architecture:** Three independent tasks touching the agent-facing layer: RuntimeAdapter gets ready detection, a new prompt module wraps tasks with project context, and PiRuntime gets env/command fixes. AgentPool wires everything together.

**Tech Stack:** Python 3.12, pytest, pytest-asyncio, asyncio, re

---

### Task 1: Ready Detection

Add `ready_pattern` and `ready_timeout_seconds` to each RuntimeAdapter. Add `_wait_for_ready()` to AgentPool that polls `capture_pane` until the pattern matches.

**Files:**
- Modify: `src/horse_fish/agents/runtime.py`
- Modify: `src/horse_fish/agents/pool.py`
- Test: `tests/test_pool.py`

**Step 1: Write failing tests for ready detection**

Add to `tests/test_pool.py`:

```python
import re


@pytest.mark.asyncio
async def test_spawn_waits_for_ready_pattern() -> None:
    """Test that spawn polls capture_pane until ready pattern matches."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=1234)
    # First call: not ready, second call: ready prompt visible
    tmux.capture_pane = AsyncMock(side_effect=["Loading...\n", "Loading...\n❯ \n"])
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder")

    assert slot.state == AgentState.idle
    # capture_pane should have been called at least twice (polling)
    assert tmux.capture_pane.await_count >= 2


@pytest.mark.asyncio
async def test_spawn_raises_on_ready_timeout() -> None:
    """Test that spawn raises if ready pattern never matches."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=1234)
    tmux.capture_pane = AsyncMock(return_value="Loading...\n")  # Never shows prompt
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))
    worktrees.remove = AsyncMock()

    pool = make_pool(store, tmux, worktrees)
    # Override timeout to 2s for fast test
    from horse_fish.agents import runtime as rt
    original = rt.RUNTIME_REGISTRY["claude"].ready_timeout_seconds
    rt.RUNTIME_REGISTRY["claude"].ready_timeout_seconds = 2

    try:
        with pytest.raises(RuntimeError, match="ready"):
            await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder")
    finally:
        rt.RUNTIME_REGISTRY["claude"].ready_timeout_seconds = original


@pytest.mark.asyncio
async def test_spawn_works_with_pi_ready_pattern() -> None:
    """Test that spawn detects Pi's ready pattern."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=5)
    tmux.capture_pane = AsyncMock(return_value="Welcome to Pi\n> \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "pi", "qwen3.5-plus", "builder")

    assert slot.state == AgentState.idle
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pool.py::test_spawn_waits_for_ready_pattern tests/test_pool.py::test_spawn_raises_on_ready_timeout tests/test_pool.py::test_spawn_works_with_pi_ready_pattern -v`
Expected: FAIL (no ready_pattern attribute, no _wait_for_ready)

**Step 3: Add ready_pattern and ready_timeout_seconds to RuntimeAdapter**

Replace `src/horse_fish/agents/runtime.py` with:

```python
"""Runtime adapters for supported agent CLIs."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field
from typing import ClassVar, Protocol


class RuntimeAdapter(Protocol):
    """Protocol for agent runtime command builders."""

    runtime_id: str
    ready_pattern: str
    ready_timeout_seconds: int

    def build_spawn_command(self, model: str) -> str:
        """Build the CLI command used to launch a runtime."""

    def build_env(self) -> dict[str, str]:
        """Build environment variables required by the runtime."""


@dataclass(slots=True)
class ClaudeRuntime:
    """Adapter for the Claude Code CLI."""

    runtime_id: ClassVar[str] = "claude"
    ready_pattern: str = r"[❯>]\s*$"
    ready_timeout_seconds: int = 30

    def build_spawn_command(self, model: str) -> str:
        if model:
            return f"claude --model {shlex.quote(model)}"
        return "claude"

    def build_env(self) -> dict[str, str]:
        return {}


@dataclass(slots=True)
class CopilotRuntime:
    """Adapter for the GitHub Copilot CLI."""

    runtime_id: ClassVar[str] = "copilot"
    ready_pattern: str = r"[>]\s*$"
    ready_timeout_seconds: int = 60

    def build_spawn_command(self, model: str) -> str:
        return f"copilot --model {shlex.quote(model)} --allow-all-tools"

    def build_env(self) -> dict[str, str]:
        return {}


@dataclass(slots=True)
class PiRuntime:
    """Adapter for the Pi CLI."""

    runtime_id: ClassVar[str] = "pi"
    ready_pattern: str = r"[>›]\s*$"
    ready_timeout_seconds: int = 30

    def build_spawn_command(self, model: str) -> str:
        return f"pi --model {shlex.quote(model)}"

    def build_env(self) -> dict[str, str]:
        key = os.environ.get("DASHSCOPE_API_KEY", "")
        if key:
            return {"DASHSCOPE_API_KEY": key}
        return {}


@dataclass(slots=True)
class OpenCodeRuntime:
    """Adapter for the OpenCode CLI."""

    runtime_id: ClassVar[str] = "opencode"
    ready_pattern: str = r"[>]\s*$"
    ready_timeout_seconds: int = 30

    def build_spawn_command(self, model: str) -> str:
        return f"opencode -m {shlex.quote(model)}"

    def build_env(self) -> dict[str, str]:
        return {}


RUNTIME_REGISTRY: dict[str, RuntimeAdapter] = {
    ClaudeRuntime.runtime_id: ClaudeRuntime(),
    CopilotRuntime.runtime_id: CopilotRuntime(),
    PiRuntime.runtime_id: PiRuntime(),
    OpenCodeRuntime.runtime_id: OpenCodeRuntime(),
}
```

Key changes:
- Removed `frozen=True` from dataclasses (so tests can override `ready_timeout_seconds`)
- Added `ready_pattern` and `ready_timeout_seconds` to each runtime
- PiRuntime.build_env() now passes DASHSCOPE_API_KEY from environment

**Step 4: Add `_wait_for_ready` to AgentPool**

In `src/horse_fish/agents/pool.py`, add these imports at the top:

```python
import asyncio
import re
```

Add this method to the `AgentPool` class (after `spawn`, before `send_task`):

```python
async def _wait_for_ready(self, slot: AgentSlot) -> None:
    """Poll tmux pane until the runtime's ready pattern appears or timeout."""
    adapter = RUNTIME_REGISTRY[slot.runtime]
    pattern = re.compile(adapter.ready_pattern, re.MULTILINE)
    timeout = adapter.ready_timeout_seconds
    elapsed = 0.0
    interval = 1.0

    while elapsed < timeout:
        output = await self._tmux.capture_pane(slot.tmux_session)
        if output and pattern.search(output):
            return
        await asyncio.sleep(interval)
        elapsed += interval

    # Timeout — clean up and raise
    await self._tmux.kill_session(slot.tmux_session)
    await self._worktrees.remove(slot.name)
    raise RuntimeError(
        f"Agent {slot.name!r} ({slot.runtime}) did not become ready "
        f"within {timeout}s"
    )
```

**Step 5: Wire `_wait_for_ready` into `spawn`**

In `pool.py`, in the `spawn` method, add the call after creating the slot and before the INSERT:

```python
        pid = await self._tmux.spawn(name=tmux_session, command=command, cwd=worktree.path, env=env)

        slot = AgentSlot(
            id=str(uuid.uuid4()),
            name=name,
            runtime=runtime,
            model=model,
            capability=capability,
            state=AgentState.idle,
            pid=pid,
            tmux_session=tmux_session,
            worktree_path=worktree.path,
            branch=worktree.branch,
            started_at=datetime.now(UTC),
        )

        # Wait for runtime to be ready before accepting tasks
        await self._wait_for_ready(slot)

        self._store.execute(
```

**Step 6: Patch asyncio.sleep in existing pool tests**

The existing spawn tests don't mock `capture_pane`, so `_wait_for_ready` will hang. Update the existing `test_spawn_creates_worktree_and_tmux_session_and_persists_slot` test to also mock capture_pane:

```python
@pytest.mark.asyncio
async def test_spawn_creates_worktree_and_tmux_session_and_persists_slot() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=1234)
    tmux.capture_pane = AsyncMock(return_value="Welcome\n❯ \n")  # Ready immediately
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder")

    worktrees.create.assert_awaited_once_with("agent-1")
    tmux.spawn.assert_awaited_once()
    spawn_kwargs = tmux.spawn.call_args
    assert spawn_kwargs.kwargs["name"] == "hf-agent-1"
    assert "claude" in spawn_kwargs.kwargs["command"]
    assert spawn_kwargs.kwargs["cwd"] == "/tmp/worktrees/agent-1"

    assert isinstance(slot, AgentSlot)
    assert slot.name == "agent-1"
    assert slot.runtime == "claude"
    assert slot.model == "claude-sonnet-4-6"
    assert slot.capability == "builder"
    assert slot.state == AgentState.idle
    assert slot.pid == 1234
    assert slot.tmux_session == "hf-agent-1"
    assert slot.worktree_path == "/tmp/worktrees/agent-1"
```

Do the same for ALL other tests in test_pool.py that call `pool.spawn()` — add `tmux.capture_pane = AsyncMock(return_value="...\n❯ \n")` (for claude runtime) or `return_value="...\n> \n"` (for other runtimes) right after `tmux.spawn = AsyncMock(...)`.

Here are the specific tests and the ready output to mock:

| Test | Runtime | Add after tmux.spawn line |
|------|---------|--------------------------|
| test_spawn_raises_for_unknown_runtime | N/A | No change needed (raises before spawn) |
| test_send_task_sends_keys_and_marks_agent_busy | copilot | `tmux.capture_pane = AsyncMock(return_value="Ready\n> \n")` |
| test_check_status_returns_idle_when_alive | claude | `tmux.capture_pane = AsyncMock(return_value="❯ \n")` |
| test_check_status_marks_dead_when_session_gone | claude | `tmux.capture_pane = AsyncMock(return_value="❯ \n")` |
| test_collect_result_returns_subtask_result | pi | Already has capture_pane mock |
| test_collect_result_marks_not_successful | opencode | Already has capture_pane mock |
| test_release_kills_session | claude | `tmux.capture_pane = AsyncMock(return_value="❯ \n")` |
| test_list_agents_returns_all_slots | claude/copilot | `tmux.capture_pane = AsyncMock(return_value="❯ \n")` |
| test_cleanup_releases_dead_and_idle | claude | `tmux.capture_pane = AsyncMock(return_value="❯ \n")` |
| test_cleanup_skips_busy_agents | claude | `tmux.capture_pane = AsyncMock(return_value="❯ \n")` |

For tests that already have `capture_pane` mocked (collect_result tests), make sure the mock returns output that matches the ready pattern on the FIRST call (for _wait_for_ready) and the actual test value on subsequent calls. Use `side_effect` list:

```python
# test_collect_result_returns_subtask_result_with_output_and_diff
tmux.capture_pane = AsyncMock(side_effect=["Welcome\n> \n", "build success\n"])

# test_collect_result_marks_not_successful_when_pane_empty
tmux.capture_pane = AsyncMock(side_effect=["Welcome\n> \n", None])
```

**Step 7: Run all pool tests**

Run: `pytest tests/test_pool.py -v`
Expected: ALL PASS

**Step 8: Run full suite**

Run: `pytest tests/ -v --tb=short`
Expected: ALL PASS (237+ tests)

**Step 9: Commit**

```bash
git add src/horse_fish/agents/runtime.py src/horse_fish/agents/pool.py tests/test_pool.py
git commit -m "feat: add ready detection — poll tmux for runtime prompt before sending tasks"
```

---

### Task 2: Agent Prompt Template

New module that wraps task descriptions with project context before sending to agents.

**Files:**
- Create: `src/horse_fish/agents/prompt.py`
- Modify: `src/horse_fish/agents/pool.py`
- Test: `tests/test_prompt.py`

**Step 1: Write failing tests**

Create `tests/test_prompt.py`:

```python
"""Tests for agent prompt template."""

from __future__ import annotations

import pytest

from horse_fish.agents.prompt import build_prompt


def test_build_prompt_includes_task():
    """Test that build_prompt includes the task description."""
    result = build_prompt(
        task="Implement user authentication",
        worktree_path="/tmp/worktrees/agent-1",
        branch="horse-fish/agent-1",
    )
    assert "Implement user authentication" in result


def test_build_prompt_includes_worktree_info():
    """Test that build_prompt includes worktree path and branch."""
    result = build_prompt(
        task="Fix bug",
        worktree_path="/tmp/worktrees/agent-1",
        branch="horse-fish/agent-1",
    )
    assert "/tmp/worktrees/agent-1" in result
    assert "horse-fish/agent-1" in result


def test_build_prompt_includes_project_context():
    """Test that build_prompt includes CLAUDE.md content when provided."""
    result = build_prompt(
        task="Fix bug",
        worktree_path="/tmp/wt",
        branch="horse-fish/x",
        project_context="Use ruff for linting. Run pytest.",
    )
    assert "Use ruff for linting" in result


def test_build_prompt_works_without_project_context():
    """Test that build_prompt works when no project context is available."""
    result = build_prompt(
        task="Fix bug",
        worktree_path="/tmp/wt",
        branch="horse-fish/x",
    )
    assert "Fix bug" in result
    # Should not crash or include None


def test_build_prompt_includes_rules():
    """Test that build_prompt includes standard agent rules."""
    result = build_prompt(
        task="Fix bug",
        worktree_path="/tmp/wt",
        branch="horse-fish/x",
    )
    assert "pytest" in result.lower()
    assert "commit" in result.lower()
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_prompt.py -v`
Expected: FAIL (module not found)

**Step 3: Create prompt module**

Create `src/horse_fish/agents/prompt.py`:

```python
"""Agent prompt template — wraps tasks with project context."""

from __future__ import annotations

_TEMPLATE = """\
You are an agent in the horse-fish swarm working in an isolated git worktree.

Worktree: {worktree_path}
Branch: {branch}
{context_section}
## Your Task

{task}

## Rules

- Run `pytest` to verify your changes before committing
- Commit your work with a descriptive message when done
- Stay focused on your assigned task only
- Do not modify files outside the scope of your task
"""

_CONTEXT_SECTION = """
## Project Conventions

{project_context}
"""


def build_prompt(
    task: str,
    worktree_path: str,
    branch: str,
    project_context: str | None = None,
) -> str:
    """Build a complete prompt for an agent.

    Args:
        task: The task description to send to the agent.
        worktree_path: Path to the agent's worktree.
        branch: Git branch name for the agent's work.
        project_context: Optional CLAUDE.md or project conventions content.

    Returns:
        Formatted prompt string.
    """
    context_section = ""
    if project_context:
        context_section = _CONTEXT_SECTION.format(project_context=project_context)

    return _TEMPLATE.format(
        worktree_path=worktree_path,
        branch=branch,
        task=task,
        context_section=context_section,
    )
```

**Step 4: Run tests**

Run: `pytest tests/test_prompt.py -v`
Expected: ALL PASS

**Step 5: Wire prompt into AgentPool.send_task**

In `src/horse_fish/agents/pool.py`, add import at top:

```python
from horse_fish.agents.prompt import build_prompt
```

Modify `AgentPool.__init__` to accept optional project context:

```python
def __init__(self, store: Store, tmux: TmuxManager, worktrees: WorktreeManager, project_context: str | None = None) -> None:
    self._store = store
    self._tmux = tmux
    self._worktrees = worktrees
    self._project_context = project_context
```

Modify `send_task` to wrap the prompt:

```python
async def send_task(self, agent_id: str, prompt: str) -> None:
    """Send a prompt to the agent's tmux session and mark it busy."""
    slot = self._get_slot(agent_id)
    full_prompt = build_prompt(
        task=prompt,
        worktree_path=slot.worktree_path or "",
        branch=slot.branch or "",
        project_context=self._project_context,
    )
    await self._tmux.send_keys(slot.tmux_session, full_prompt)
    self._store.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.busy, agent_id))
```

**Step 6: Write test for prompt wiring in pool**

Add to `tests/test_pool.py`:

```python
@pytest.mark.asyncio
async def test_send_task_wraps_prompt_with_context() -> None:
    """Test that send_task wraps the raw prompt with project context."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=9)
    tmux.capture_pane = AsyncMock(return_value="Ready\n> \n")
    tmux.send_keys = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())

    pool = AgentPool(store, tmux, worktrees, project_context="Use ruff. Run pytest.")
    slot = await pool.spawn("agent-1", "copilot", "gpt-5.4", "builder")

    await pool.send_task(slot.id, "implement feature X")

    # The prompt sent to tmux should contain both the task and the context
    sent_prompt = tmux.send_keys.call_args[0][1]
    assert "implement feature X" in sent_prompt
    assert "Use ruff" in sent_prompt
    assert "horse-fish/agent-1" in sent_prompt
```

**Step 7: Update existing send_task test assertion**

The existing `test_send_task_sends_keys_and_marks_agent_busy` asserts the exact prompt string. Update it to check the prompt contains the task:

```python
@pytest.mark.asyncio
async def test_send_task_sends_keys_and_marks_agent_busy() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=9)
    tmux.capture_pane = AsyncMock(return_value="Ready\n> \n")
    tmux.send_keys = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "copilot", "gpt-5.4", "builder")

    await pool.send_task(slot.id, "implement feature X")

    tmux.send_keys.assert_awaited_once()
    sent_prompt = tmux.send_keys.call_args[0][1]
    assert "implement feature X" in sent_prompt
    agents = pool.list_agents()
    assert agents[0].state == AgentState.busy
```

**Step 8: Update CLI to load CLAUDE.md**

In `src/horse_fish/cli.py`, modify `_init_components` to load CLAUDE.md:

```python
def _init_components(runtime: str, model: str | None, max_agents: int):
    """Initialize all components needed for orchestration."""
    repo_root = str(Path.cwd())
    store = Store(DB_PATH)
    store.migrate()
    tmux = TmuxManager()
    worktrees = WorktreeManager(repo_root)

    # Load project context from CLAUDE.md if available
    claude_md = Path.cwd() / "CLAUDE.md"
    project_context = claude_md.read_text() if claude_md.exists() else None

    pool = AgentPool(store, tmux, worktrees, project_context=project_context)
    planner = Planner(runtime=runtime, model=model)
    gates = ValidationGates()
    memory = MemoryStore()
    orchestrator = Orchestrator(
        pool=pool, planner=planner, gates=gates, runtime=runtime, model=model or "", max_agents=max_agents,
        memory=memory,
    )
    return orchestrator, store, pool
```

**Step 9: Run all tests**

Run: `pytest tests/test_prompt.py tests/test_pool.py tests/test_cli.py -v`
Expected: ALL PASS

**Step 10: Commit**

```bash
git add src/horse_fish/agents/prompt.py src/horse_fish/agents/pool.py src/horse_fish/cli.py tests/test_prompt.py tests/test_pool.py
git commit -m "feat: add agent prompt template — inject CLAUDE.md context into agent tasks"
```

---

### Task 3: Pi Runtime Environment Fix

PiRuntime needs to pass DASHSCOPE_API_KEY and the runtime test needs updating.

**Files:**
- Modify: `src/horse_fish/agents/runtime.py` (already done in Task 1 — PiRuntime.build_env)
- Test: `tests/test_tmux.py` (runtime registry tests)

**Note:** If Task 1 is done first, PiRuntime.build_env() already returns DASHSCOPE_API_KEY. This task only needs to add tests verifying the behavior.

**Step 1: Write tests for Pi env**

Add to a new file `tests/test_runtime.py`:

```python
"""Tests for runtime adapters."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from horse_fish.agents.runtime import (
    RUNTIME_REGISTRY,
    ClaudeRuntime,
    CopilotRuntime,
    OpenCodeRuntime,
    PiRuntime,
)


def test_all_runtimes_have_ready_pattern():
    """Every runtime in registry must have a ready_pattern."""
    for name, adapter in RUNTIME_REGISTRY.items():
        assert hasattr(adapter, "ready_pattern"), f"{name} missing ready_pattern"
        assert adapter.ready_pattern, f"{name} has empty ready_pattern"


def test_all_runtimes_have_ready_timeout():
    """Every runtime must have a positive ready_timeout_seconds."""
    for name, adapter in RUNTIME_REGISTRY.items():
        assert hasattr(adapter, "ready_timeout_seconds"), f"{name} missing ready_timeout_seconds"
        assert adapter.ready_timeout_seconds > 0, f"{name} has non-positive timeout"


def test_claude_spawn_command_includes_model():
    rt = ClaudeRuntime()
    cmd = rt.build_spawn_command("claude-sonnet-4-6")
    assert "claude" in cmd
    assert "claude-sonnet-4-6" in cmd


def test_claude_spawn_command_no_model():
    rt = ClaudeRuntime()
    cmd = rt.build_spawn_command("")
    assert cmd == "claude"


def test_pi_build_env_passes_dashscope_key():
    """PiRuntime.build_env() should pass DASHSCOPE_API_KEY from environment."""
    with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key-123"}):
        rt = PiRuntime()
        env = rt.build_env()
        assert env.get("DASHSCOPE_API_KEY") == "test-key-123"


def test_pi_build_env_empty_when_no_key():
    """PiRuntime.build_env() returns empty dict when no DASHSCOPE_API_KEY."""
    with patch.dict(os.environ, {}, clear=True):
        # Remove DASHSCOPE_API_KEY if present
        os.environ.pop("DASHSCOPE_API_KEY", None)
        rt = PiRuntime()
        env = rt.build_env()
        assert env == {}


def test_pi_spawn_command():
    rt = PiRuntime()
    cmd = rt.build_spawn_command("qwen3.5-plus")
    assert "pi" in cmd
    assert "qwen3.5-plus" in cmd


def test_copilot_spawn_command():
    rt = CopilotRuntime()
    cmd = rt.build_spawn_command("gpt-5.4")
    assert "copilot" in cmd
    assert "--allow-all-tools" in cmd


def test_opencode_spawn_command():
    rt = OpenCodeRuntime()
    cmd = rt.build_spawn_command("qwen3.5-plus")
    assert "opencode" in cmd
    assert "-m" in cmd


def test_claude_ready_pattern_matches_prompt():
    """Claude ready pattern should match ❯ and > prompts."""
    import re
    rt = ClaudeRuntime()
    pattern = re.compile(rt.ready_pattern, re.MULTILINE)
    assert pattern.search("Loading claude...\n❯ ")
    assert pattern.search("Welcome\n> ")
    assert not pattern.search("Loading...")


def test_pi_ready_pattern_matches_prompt():
    """Pi ready pattern should match > and › prompts."""
    import re
    rt = PiRuntime()
    pattern = re.compile(rt.ready_pattern, re.MULTILINE)
    assert pattern.search("Welcome to Pi\n> ")
    assert pattern.search("Pi ready\n› ")
    assert not pattern.search("Loading Pi...")
```

**Step 2: Run tests**

Run: `pytest tests/test_runtime.py -v`
Expected: ALL PASS (if Task 1 is already implemented; FAIL if not — runtime.py changes needed)

**Step 3: Commit**

```bash
git add tests/test_runtime.py
git commit -m "test: add runtime adapter tests — ready patterns, Pi env, spawn commands"
```

---

## Swarm Assignment

| Task | Agent Name | Runtime | Independent? |
|------|-----------|---------|-------------|
| Task 1: Ready detection | ready-detect | Pi/qwen3.5-plus | Yes |
| Task 2: Prompt template | prompt-template | Pi/qwen3.5-plus | Yes (but touches pool.py — merge Task 1 first) |
| Task 3: Runtime tests | runtime-tests | Pi/qwen3.5-plus | Yes |

**Merge order:** Task 1 → Task 3 → Task 2 (Task 2 modifies pool.py which Task 1 also modifies; Task 3 validates Task 1's runtime changes)
