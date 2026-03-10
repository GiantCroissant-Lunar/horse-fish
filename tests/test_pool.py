"""Tests for AgentPool lifecycle management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import SpawnResult
from horse_fish.agents.worktree import WorktreeInfo
from horse_fish.models import AgentSlot, AgentState, SubtaskResult
from horse_fish.store.db import Store

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_store() -> Store:
    store = Store(":memory:")
    store.migrate()
    return store


def make_worktree_info(name: str = "agent-1") -> WorktreeInfo:
    return WorktreeInfo(path=f"/tmp/worktrees/{name}", branch=f"horse-fish/{name}", name=name)


def make_pool(store: Store, tmux: MagicMock, worktrees: MagicMock, tracer: MagicMock | None = None) -> AgentPool:
    return AgentPool(store=store, tmux=tmux, worktrees=worktrees, tracer=tracer)


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_creates_worktree_and_tmux_session_and_persists_slot() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
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

    agents = pool.list_agents()
    assert len(agents) == 1
    assert agents[0].id == slot.id


@pytest.mark.asyncio
async def test_spawn_raises_for_unknown_runtime() -> None:
    store = make_store()
    tmux = MagicMock()
    worktrees = MagicMock()

    pool = make_pool(store, tmux, worktrees)
    with pytest.raises(ValueError, match="unknown runtime"):
        await pool.spawn("agent-1", "nonexistent", "model-x", "builder")


@pytest.mark.asyncio
async def test_spawn_waits_for_ready_pattern() -> None:
    """Test that spawn waits for the ready pattern before proceeding."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(side_effect=["Loading...\n", "Loading...\n❯ \n"])
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder")

    # Verify capture_pane was called at least twice (polling)
    assert tmux.capture_pane.call_count >= 2
    assert isinstance(slot, AgentSlot)
    assert slot.name == "agent-1"


@pytest.mark.asyncio
async def test_spawn_traces_spawn_and_ready_spans() -> None:
    """spawn should emit dedicated spans for agent startup and readiness."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(side_effect=["Loading...\n", "Loading...\n❯ \n"])
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))
    tracer = MagicMock()
    tracer.span.side_effect = [MagicMock(name="spawn-span"), MagicMock(name="ready-span")]

    pool = make_pool(store, tmux, worktrees, tracer=tracer)
    await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder")

    span_names = [call.args[1] for call in tracer.span.call_args_list]
    assert span_names == ["agent.spawn", "agent.wait_for_ready"]
    assert tracer.end_span.call_count == 2


@pytest.mark.asyncio
async def test_spawn_raises_on_ready_timeout() -> None:
    """Test that spawn raises RuntimeError when ready timeout is exceeded."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(return_value="Loading...\n")
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))
    worktrees.remove = AsyncMock()

    pool = make_pool(store, tmux, worktrees)

    # Patch the runtime registry to use a short timeout for testing
    from horse_fish.agents import runtime

    original_registry_entry = runtime.RUNTIME_REGISTRY["claude"]

    # Create a modified runtime with short timeout
    class FastTimeoutClaudeRuntime:
        runtime_id = "claude"
        ready_pattern = r"[❯>]\s*$"
        ready_timeout_seconds = 2
        dismiss_patterns: list[tuple[str, str]] = []

        def build_spawn_command(self, model: str) -> str:
            return "claude"

        def build_env(self) -> dict[str, str]:
            return {}

        def post_ready_commands(self, model: str) -> list[str]:
            return []

    try:
        runtime.RUNTIME_REGISTRY["claude"] = FastTimeoutClaudeRuntime()
        with pytest.raises(RuntimeError, match="ready"):
            await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder")
    finally:
        runtime.RUNTIME_REGISTRY["claude"] = original_registry_entry


@pytest.mark.asyncio
async def test_respawn_traces_respawn_and_ready_spans() -> None:
    """respawn should emit respawn and readiness spans."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=9, pgid=9))
    tmux.send_keys = AsyncMock()
    tmux.capture_pane = AsyncMock(side_effect=["Ready\n❯ \n", "Ready\n❯ \n"])
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    tracer = MagicMock()
    tracer.span.side_effect = [
        MagicMock(name="spawn-span"),
        MagicMock(name="spawn-ready-span"),
        MagicMock(name="respawn-span"),
        MagicMock(name="respawn-ready-span"),
    ]

    pool = make_pool(store, tmux, worktrees, tracer=tracer)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")

    await pool.respawn(slot.id)

    span_names = [call.args[1] for call in tracer.span.call_args_list]
    assert span_names == ["agent.spawn", "agent.wait_for_ready", "agent.respawn", "agent.wait_for_ready"]
    assert tracer.end_span.call_count == 4


@pytest.mark.asyncio
async def test_spawn_works_with_pi_ready_pattern() -> None:
    """Test that spawn works with Pi's ready pattern."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(return_value="pi v0.55.1\n0.0%/1.0M (auto)\n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "pi", "kimi-for-coding", "builder")

    assert isinstance(slot, AgentSlot)
    assert slot.name == "agent-1"
    assert slot.runtime == "pi"


# ---------------------------------------------------------------------------
# send_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_task_sends_keys_and_marks_agent_busy() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=9, pgid=9))
    tmux.send_keys = AsyncMock()
    tmux.capture_pane = AsyncMock(return_value="Ready\n> \n")
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


@pytest.mark.asyncio
async def test_send_task_raw_skips_prompt_wrapping() -> None:
    """Test that send_task with raw=True sends prompt as-is."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=9, pgid=9))
    tmux.send_keys = AsyncMock()
    tmux.capture_pane = AsyncMock(return_value="Ready\n> \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "copilot", "gpt-5.4", "builder")

    await pool.send_task(slot.id, "fix this lint error", raw=True)

    tmux.send_keys.assert_awaited_once()
    sent_text = tmux.send_keys.call_args[0][1]
    assert sent_text == "fix this lint error"
    assert "## Worktree Information" not in sent_text


@pytest.mark.asyncio
async def test_send_task_raises_for_missing_agent() -> None:
    pool = make_pool(make_store(), MagicMock(), MagicMock())
    with pytest.raises(KeyError, match="not found"):
        await pool.send_task("no-such-id", "prompt")


@pytest.mark.asyncio
async def test_send_task_wraps_prompt_with_context() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=9, pgid=9))
    tmux.capture_pane = AsyncMock(return_value="Ready\n> \n")
    tmux.send_keys = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())

    pool = AgentPool(
        store=store,
        tmux=tmux,
        worktrees=worktrees,
        project_context="Use ruff.",
    )
    slot = await pool.spawn("agent-1", "copilot", "gpt-5.4", "builder")

    await pool.send_task(slot.id, "implement feature X")

    tmux.send_keys.assert_awaited_once()
    sent_prompt = tmux.send_keys.call_args[0][1]
    assert "implement feature X" in sent_prompt
    assert "Use ruff." in sent_prompt


@pytest.mark.asyncio
async def test_send_task_persists_task_id() -> None:
    """Test that send_task persists the task_id in the database."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=9, pgid=9))
    tmux.send_keys = AsyncMock()
    tmux.capture_pane = AsyncMock(return_value="Ready\n> \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "copilot", "gpt-5.4", "builder")

    task_id = "test-subtask-123"
    await pool.send_task(slot.id, "implement feature X", task_id=task_id)

    # Verify task_id was persisted in the database
    agents = pool.list_agents()
    assert agents[0].task_id == task_id
    assert agents[0].state == AgentState.busy


@pytest.mark.asyncio
async def test_send_task_traces_agent_prompt_generation() -> None:
    """Task prompts should emit a Langfuse generation when tracer is configured."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=9, pgid=9))
    tmux.send_keys = AsyncMock()
    tmux.capture_pane = AsyncMock(return_value="Ready\n> \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    tracer = MagicMock()
    tracer.get_prompt.return_value = None
    tracer.generation.return_value = MagicMock()

    pool = make_pool(store, tmux, worktrees, tracer=tracer)
    slot = await pool.spawn("agent-1", "copilot", "gpt-5.4", "builder")

    await pool.send_task(slot.id, "implement feature X", task_id="subtask-1")

    tracer.generation.assert_called_once()
    assert tracer.generation.call_args.args[1] == "agent.task_prompt"
    generation_end_calls = [
        call for call in tracer.end_span.call_args_list if call.args[0] is tracer.generation.return_value
    ]
    assert len(generation_end_calls) == 1


@pytest.mark.asyncio
async def test_send_task_fix_prompt_uses_fix_template() -> None:
    """Fix prompts should resolve through the Langfuse-managed fix prompt path."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=9, pgid=9))
    tmux.send_keys = AsyncMock()
    tmux.capture_pane = AsyncMock(return_value="Ready\n> \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    tracer = MagicMock()
    tracer.get_prompt.return_value = None
    tracer.generation.return_value = MagicMock()

    pool = make_pool(store, tmux, worktrees, tracer=tracer)
    slot = await pool.spawn("agent-1", "copilot", "gpt-5.4", "builder")

    await pool.send_task(slot.id, "ruff-check: F401 unused import", prompt_kind="fix")

    sent_prompt = tmux.send_keys.call_args[0][1]
    assert "Your previous changes failed the following quality gates" in sent_prompt
    assert "F401 unused import" in sent_prompt
    assert tracer.generation.call_args.args[1] == "agent.fix_prompt"


@pytest.mark.asyncio
async def test_collect_result_returns_correct_subtask_id() -> None:
    """Test that collect_result returns the correct subtask_id when task_id is set."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=7, pgid=7))
    tmux.capture_pane = AsyncMock(side_effect=["pi v0.55.1\n0.0%/1.0M (auto)\n", "build success\n"])
    tmux.send_keys = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.get_diff = AsyncMock(return_value="diff --git a/foo.py")

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "pi", "kimi-for-coding", "builder")

    # Set task_id via send_task
    task_id = "test-subtask-456"
    await pool.send_task(slot.id, "build something", task_id=task_id)

    result = await pool.collect_result(slot.id)

    assert result.subtask_id == task_id


@pytest.mark.asyncio
async def test_collect_result_falls_back_to_agent_id_when_no_task_id() -> None:
    """Test that collect_result falls back to agent_id when task_id is not set."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=7, pgid=7))
    tmux.capture_pane = AsyncMock(side_effect=["Ready\n❯ \n", "output\n"])
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.get_diff = AsyncMock(return_value="")

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")

    # Don't call send_task, so task_id remains None
    result = await pool.collect_result(slot.id)

    assert result.subtask_id == slot.id


# ---------------------------------------------------------------------------
# check_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_status_returns_idle_when_alive() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=5, pgid=5))
    tmux.is_alive = AsyncMock(return_value=True)
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")

    state = await pool.check_status(slot.id)
    assert state == AgentState.idle


@pytest.mark.asyncio
async def test_check_status_traces_agent_probe() -> None:
    """check_status should emit an agent-level status probe span."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=5, pgid=5))
    tmux.is_alive = AsyncMock(return_value=True)
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    tracer = MagicMock()
    tracer.span.side_effect = [
        MagicMock(name="spawn-span"),
        MagicMock(name="ready-span"),
        MagicMock(name="status-span"),
    ]

    pool = make_pool(store, tmux, worktrees, tracer=tracer)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")

    state = await pool.check_status(slot.id)

    assert state == AgentState.idle
    assert tracer.span.call_args_list[-1].args[1] == "agent.check_status"
    assert tracer.end_span.call_args_list[-1].args[1] == {"alive": True, "state": "idle"}


@pytest.mark.asyncio
async def test_check_status_marks_dead_when_session_gone() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=5, pgid=5))
    tmux.is_alive = AsyncMock(return_value=False)
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")

    state = await pool.check_status(slot.id)
    assert state == AgentState.dead

    agents = pool.list_agents()
    assert agents[0].state == AgentState.dead


# ---------------------------------------------------------------------------
# collect_result
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_collect_result_returns_subtask_result_with_output_and_diff() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=7, pgid=7))
    tmux.capture_pane = AsyncMock(side_effect=["pi v0.55.1\n0.0%/1.0M (auto)\n", "build success\n"])
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.get_diff = AsyncMock(return_value="diff --git a/foo.py")

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "pi", "kimi-for-coding", "builder")

    result = await pool.collect_result(slot.id)

    assert isinstance(result, SubtaskResult)
    assert result.success is True
    assert result.output == "build success\n"
    assert result.diff == "diff --git a/foo.py"
    assert result.duration_seconds >= 0


@pytest.mark.asyncio
async def test_collect_result_traces_execution_probe() -> None:
    """collect_result should emit output and diff metadata for execution probes."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=7, pgid=7))
    tmux.capture_pane = AsyncMock(side_effect=["Ready\n❯ \n", "build success\n"])
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.get_diff = AsyncMock(return_value="diff --git a/foo.py")
    tracer = MagicMock()
    tracer.span.side_effect = [
        MagicMock(name="spawn-span"),
        MagicMock(name="ready-span"),
        MagicMock(name="result-span"),
    ]

    pool = make_pool(store, tmux, worktrees, tracer=tracer)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")

    result = await pool.collect_result(slot.id)

    assert result.success is True
    assert tracer.span.call_args_list[-1].args[1] == "agent.collect_result"
    end_call = tracer.end_span.call_args_list[-1]
    assert end_call.args[1] == {"success": True, "has_diff": True, "has_output": True}
    assert end_call.kwargs["metadata"]["output_chars"] == len("build success\n")
    assert end_call.kwargs["metadata"]["diff_chars"] == len("diff --git a/foo.py")


@pytest.mark.asyncio
async def test_collect_result_runtime_observations_include_subtask_context() -> None:
    """Runtime output observations should carry the active orchestrator subtask context."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=7, pgid=7))
    tmux.send_keys = AsyncMock()
    tmux.capture_pane = AsyncMock(
        side_effect=[
            "Ready\n❯ \n",
            "⏺ Bash(git status --short)\nConfirm to bypass permissions?\n",
        ]
    )
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.get_diff = AsyncMock(return_value="")
    tracer = MagicMock()
    tracer.span.side_effect = [
        MagicMock(name="spawn-span"),
        MagicMock(name="ready-span"),
        MagicMock(name="result-span"),
        MagicMock(name="runtime-tool-span"),
        MagicMock(name="runtime-prompt-span"),
    ]

    pool = make_pool(store, tmux, worktrees, tracer=tracer)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")
    await pool.send_task(
        slot.id,
        "implement feature X",
        task_id="subtask-1",
        run_id="run-1",
        subtask_description="Implement feature X",
    )

    await pool.collect_result(slot.id)

    runtime_calls = [call for call in tracer.span.call_args_list if call.args[1].startswith("agent.runtime_")]
    assert runtime_calls
    for call in runtime_calls:
        metadata = call.args[2]
        assert metadata["run_id"] == "run-1"
        assert metadata["subtask_id"] == "subtask-1"
        assert metadata["subtask_description"] == "Implement feature X"
        assert metadata["prompt_kind"] == "task"


@pytest.mark.asyncio
async def test_runtime_observation_summary_counts_deduped_events() -> None:
    """runtime_observation_summary should aggregate deduped tool/prompt events by run."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=7, pgid=7))
    tmux.send_keys = AsyncMock()
    tmux.capture_pane = AsyncMock(
        side_effect=[
            "Ready\n❯ \n",
            "⏺ Bash(git status --short)\nConfirm to bypass permissions?\n",
            "⏺ Bash(git status --short)\nConfirm to bypass permissions?\n",
        ]
    )
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.get_diff = AsyncMock(return_value="")
    tracer = MagicMock()

    pool = make_pool(store, tmux, worktrees, tracer=tracer)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")
    await pool.send_task(
        slot.id,
        "implement feature X",
        task_id="subtask-1",
        run_id="run-1",
        subtask_description="Implement feature X",
    )

    await pool.collect_result(slot.id)
    await pool.collect_result(slot.id)

    summary = pool.runtime_observation_summary("run-1")
    assert summary["total_count"] == 2
    assert summary["tool_count"] == 1
    assert summary["prompt_count"] == 1
    assert summary["first_observed_at"] is not None
    assert summary["last_observed_at"] is not None
    assert summary["subtasks_with_runtime_observations"] == 1
    assert summary["subtask_ids"] == ["subtask-1"]
    assert summary["subtask_breakdown"] == [
        {
            "subtask_id": "subtask-1",
            "count": 2,
            "tool_count": 1,
            "prompt_count": 1,
            "subtask_description": "Implement feature X",
            "prompt_kinds": {"task": 2},
            "observation_names": {"Bash": 1, "permission_prompt": 1},
            "first_observed_at": summary["subtask_breakdown"][0]["first_observed_at"],
            "last_observed_at": summary["subtask_breakdown"][0]["last_observed_at"],
            "latest_excerpt": "Confirm to bypass permissions?",
        }
    ]
    assert summary["subtask_breakdown"][0]["first_observed_at"] is not None
    assert summary["subtask_breakdown"][0]["last_observed_at"] is not None
    assert summary["runtimes"] == {"claude": 2}
    assert summary["observation_names"]["Bash"] == 1
    assert summary["observation_names"]["permission_prompt"] == 1
    assert len(summary["recent_observations"]) == 2
    assert summary["recent_observations"][-1]["observation_name"] == "permission_prompt"
    assert summary["recent_observations"][-1]["excerpt"] == "Confirm to bypass permissions?"
    assert summary["recent_observations"][-1]["observed_at"] is not None


@pytest.mark.asyncio
async def test_collect_result_emits_runtime_tool_and_prompt_spans() -> None:
    """collect_result should emit runtime-derived tool and prompt observations once."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=7, pgid=7))
    tmux.capture_pane = AsyncMock(
        side_effect=[
            "Ready\n❯ \n",
            "⏺ Bash(git status --short)\nConfirm to bypass permissions?\n",
            "⏺ Bash(git status --short)\nConfirm to bypass permissions?\n",
        ]
    )
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.get_diff = AsyncMock(return_value="")
    tracer = MagicMock()
    tracer.span.side_effect = [
        MagicMock(name="spawn-span"),
        MagicMock(name="ready-span"),
        MagicMock(name="result-span-1"),
        MagicMock(name="runtime-tool-span"),
        MagicMock(name="runtime-prompt-span"),
        MagicMock(name="result-span-2"),
    ]

    pool = make_pool(store, tmux, worktrees, tracer=tracer)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")

    await pool.collect_result(slot.id)
    await pool.collect_result(slot.id)

    span_names = [call.args[1] for call in tracer.span.call_args_list]
    assert span_names.count("agent.runtime_tool") == 1
    assert span_names.count("agent.runtime_prompt") == 1
    assert span_names.count("agent.collect_result") == 2


@pytest.mark.asyncio
async def test_collect_result_marks_not_successful_when_pane_empty() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=7, pgid=7))
    tmux.capture_pane = AsyncMock(side_effect=["OpenCode ready\n> \n", None])
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.get_diff = AsyncMock(return_value="")

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "opencode", "qwen3.5-plus", "builder")

    result = await pool.collect_result(slot.id)
    assert result.success is False
    assert result.output == ""


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_kills_session_removes_worktree_marks_dead() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=3, pgid=3))
    tmux.kill_session = AsyncMock()
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.remove = AsyncMock()

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")

    await pool.release(slot.id)

    tmux.kill_session.assert_awaited_once_with("hf-agent-1")
    worktrees.remove.assert_awaited_once_with("agent-1")

    agents = pool.list_agents()
    assert agents[0].state == AgentState.dead


# ---------------------------------------------------------------------------
# list_agents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_agents_returns_all_slots() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(side_effect=[SpawnResult(pid=1, pgid=1), SpawnResult(pid=2, pgid=2)])
    # Return appropriate ready pattern for each runtime (claude uses ❯, copilot uses >)
    tmux.capture_pane = AsyncMock(side_effect=["Ready\n❯ \n", "Ready\n> \n"])
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(side_effect=[make_worktree_info("agent-1"), make_worktree_info("agent-2")])

    pool = make_pool(store, tmux, worktrees)
    await pool.spawn("agent-1", "claude", "model", "builder")
    await pool.spawn("agent-2", "copilot", "gpt-5.4", "scout")

    agents = pool.list_agents()
    assert len(agents) == 2
    names = {a.name for a in agents}
    assert names == {"agent-1", "agent-2"}


# ---------------------------------------------------------------------------
# cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_releases_dead_and_idle_agents() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(side_effect=[SpawnResult(pid=1, pgid=1), SpawnResult(pid=2, pgid=2)])
    tmux.send_keys = AsyncMock()
    tmux.is_alive = AsyncMock(return_value=False)
    tmux.kill_session = AsyncMock()
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(side_effect=[make_worktree_info("agent-1"), make_worktree_info("agent-2")])
    worktrees.remove = AsyncMock()
    worktrees.cleanup = AsyncMock(return_value=0)

    pool = make_pool(store, tmux, worktrees)
    await pool.spawn("agent-1", "claude", "model", "builder")
    await pool.spawn("agent-2", "claude", "model", "builder")

    count = await pool.cleanup()

    assert count == 2
    worktrees.cleanup.assert_awaited_once()
    assert tmux.kill_session.await_count == 2


@pytest.mark.asyncio
async def test_cleanup_releases_busy_agents() -> None:
    """Test that cleanup() also releases busy agents."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1, pgid=1))
    tmux.send_keys = AsyncMock()
    tmux.kill_session = AsyncMock()
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.remove = AsyncMock()
    worktrees.cleanup = AsyncMock(return_value=0)

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")
    # Mark busy directly in store
    store.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.busy, slot.id))

    count = await pool.cleanup()

    assert count == 1
    tmux.kill_session.assert_awaited_once_with("hf-agent-1")
    worktrees.remove.assert_awaited_once_with("agent-1")

    agents = pool.list_agents()
    assert agents[0].state == AgentState.dead
