"""Tests for AgentPool lifecycle management."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.agents.pool import AgentPool
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


def make_pool(store: Store, tmux: MagicMock, worktrees: MagicMock) -> AgentPool:
    return AgentPool(store=store, tmux=tmux, worktrees=worktrees)


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_creates_worktree_and_tmux_session_and_persists_slot() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=1234)
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


# ---------------------------------------------------------------------------
# send_task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_task_sends_keys_and_marks_agent_busy() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=9)
    tmux.send_keys = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "copilot", "gpt-5.4", "builder")

    await pool.send_task(slot.id, "implement feature X")

    tmux.send_keys.assert_awaited_once_with("hf-agent-1", "implement feature X")
    agents = pool.list_agents()
    assert agents[0].state == AgentState.busy


@pytest.mark.asyncio
async def test_send_task_raises_for_missing_agent() -> None:
    pool = make_pool(make_store(), MagicMock(), MagicMock())
    with pytest.raises(KeyError, match="not found"):
        await pool.send_task("no-such-id", "prompt")


# ---------------------------------------------------------------------------
# check_status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_status_returns_idle_when_alive() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=5)
    tmux.is_alive = AsyncMock(return_value=True)
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")

    state = await pool.check_status(slot.id)
    assert state == AgentState.idle


@pytest.mark.asyncio
async def test_check_status_marks_dead_when_session_gone() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=5)
    tmux.is_alive = AsyncMock(return_value=False)
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
    tmux.spawn = AsyncMock(return_value=7)
    tmux.capture_pane = AsyncMock(return_value="build success\n")
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
async def test_collect_result_marks_not_successful_when_pane_empty() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=7)
    tmux.capture_pane = AsyncMock(return_value=None)
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
    tmux.spawn = AsyncMock(return_value=3)
    tmux.kill_session = AsyncMock()
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
    tmux.spawn = AsyncMock(side_effect=[1, 2])
    worktrees = MagicMock()
    worktrees.create = AsyncMock(
        side_effect=[make_worktree_info("agent-1"), make_worktree_info("agent-2")]
    )

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
    tmux.spawn = AsyncMock(side_effect=[1, 2])
    tmux.send_keys = AsyncMock()
    tmux.is_alive = AsyncMock(return_value=False)
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(
        side_effect=[make_worktree_info("agent-1"), make_worktree_info("agent-2")]
    )
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
async def test_cleanup_skips_busy_agents() -> None:
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=1)
    tmux.send_keys = AsyncMock()
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info())
    worktrees.remove = AsyncMock()
    worktrees.cleanup = AsyncMock(return_value=0)

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "model", "builder")
    # Mark busy directly in store
    store.execute("UPDATE agents SET state = ? WHERE id = ?", (AgentState.busy, slot.id))

    count = await pool.cleanup()

    assert count == 0
    tmux.kill_session.assert_not_awaited()
