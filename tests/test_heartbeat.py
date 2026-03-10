"""Tests for agent heartbeat detection in AgentPool."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import SpawnResult
from horse_fish.models import AgentState


@pytest.fixture
def mock_store():
    store = MagicMock()
    store.fetchone = MagicMock()
    store.fetchall = MagicMock(return_value=[])
    store.execute = MagicMock()
    return store


@pytest.fixture
def mock_tmux():
    tmux = AsyncMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=123, pgid=123))
    tmux.is_alive = AsyncMock(return_value=True)
    tmux.capture_pane = AsyncMock(return_value="some output")
    tmux.kill_session = AsyncMock()
    return tmux


@pytest.fixture
def mock_worktrees():
    wt = AsyncMock()
    wt.create = AsyncMock()
    wt.remove = AsyncMock()
    return wt


@pytest.fixture
def pool(mock_store, mock_tmux, mock_worktrees):
    return AgentPool(store=mock_store, tmux=mock_tmux, worktrees=mock_worktrees)


def _make_slot(agent_id: str = "agent-1", state: AgentState = AgentState.busy) -> dict:
    """Return a DB row dict for an agent."""
    return {
        "id": agent_id,
        "name": "test-agent",
        "runtime": "pi",
        "model": "qwen3.5-plus",
        "capability": "builder",
        "state": state.value,
        "pid": 123,
        "pgid": 123,
        "tmux_session": "hf-test-agent",
        "worktree_path": "/tmp/wt",
        "branch": "test-branch",
        "task_id": "task-1",
        "run_id": "run-1",
        "started_at": "2026-01-01T00:00:00+00:00",
        "idle_since": None,
    }


@pytest.mark.asyncio
async def test_heartbeat_first_check_returns_true(pool, mock_store, mock_tmux):
    """First heartbeat check should return True (no prior hash to compare)."""
    mock_store.fetchone.return_value = _make_slot()
    mock_tmux.capture_pane.return_value = "agent is working"

    result = await pool.check_heartbeat("agent-1")
    assert result is True


@pytest.mark.asyncio
async def test_heartbeat_same_output_returns_false(pool, mock_store, mock_tmux):
    """Same output on consecutive checks should return False (stalled)."""
    mock_store.fetchone.return_value = _make_slot()
    mock_tmux.capture_pane.return_value = "same output"

    await pool.check_heartbeat("agent-1")  # first call sets baseline
    result = await pool.check_heartbeat("agent-1")  # same output
    assert result is False


@pytest.mark.asyncio
async def test_heartbeat_changed_output_returns_true(pool, mock_store, mock_tmux):
    """Changed output on consecutive checks should return True (alive)."""
    mock_store.fetchone.return_value = _make_slot()

    mock_tmux.capture_pane.return_value = "output v1"
    await pool.check_heartbeat("agent-1")

    mock_tmux.capture_pane.return_value = "output v2"
    result = await pool.check_heartbeat("agent-1")
    assert result is True


@pytest.mark.asyncio
async def test_heartbeat_dead_agent_returns_false(pool, mock_store, mock_tmux):
    """Dead agents should return False."""
    mock_store.fetchone.return_value = _make_slot(state=AgentState.dead)

    result = await pool.check_heartbeat("agent-1")
    assert result is False


@pytest.mark.asyncio
async def test_heartbeat_capture_error_returns_false(pool, mock_store, mock_tmux):
    """If capture_pane raises, heartbeat should return False."""
    mock_store.fetchone.return_value = _make_slot()
    mock_tmux.capture_pane.side_effect = RuntimeError("tmux error")

    result = await pool.check_heartbeat("agent-1")
    assert result is False


@pytest.mark.asyncio
async def test_heartbeat_tracks_multiple_agents(pool, mock_store, mock_tmux):
    """Heartbeat hashes are tracked independently per agent."""
    mock_store.fetchone.side_effect = lambda *args, **kwargs: _make_slot(agent_id=args[1][0])

    # Agent 1: changing output
    mock_tmux.capture_pane.return_value = "agent1 v1"
    await pool.check_heartbeat("agent-1")
    mock_tmux.capture_pane.return_value = "agent1 v2"
    result1 = await pool.check_heartbeat("agent-1")

    # Agent 2: same output
    mock_tmux.capture_pane.return_value = "agent2 v1"
    await pool.check_heartbeat("agent-2")
    mock_tmux.capture_pane.return_value = "agent2 v1"
    result2 = await pool.check_heartbeat("agent-2")

    assert result1 is True  # agent-1 changed
    assert result2 is False  # agent-2 stalled


@pytest.mark.asyncio
async def test_heartbeat_empty_output(pool, mock_store, mock_tmux):
    """Empty pane output should still work (agent may not have started yet)."""
    mock_store.fetchone.return_value = _make_slot()
    mock_tmux.capture_pane.return_value = ""

    result = await pool.check_heartbeat("agent-1")
    assert result is True  # first check

    result = await pool.check_heartbeat("agent-1")
    assert result is False  # same empty output


@pytest.mark.asyncio
async def test_heartbeat_hash_cleared_on_release(pool, mock_store, mock_tmux):
    """Releasing an agent should clear its heartbeat hash."""
    mock_store.fetchone.return_value = _make_slot()
    mock_tmux.capture_pane.return_value = "some output"

    await pool.check_heartbeat("agent-1")
    assert "agent-1" in pool._last_output_hash

    await pool.release("agent-1")
    assert "agent-1" not in pool._last_output_hash
