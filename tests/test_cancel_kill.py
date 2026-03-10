"""Tests for cancel command and process killing functionality."""

from __future__ import annotations

import signal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import SpawnResult
from horse_fish.agents.worktree import WorktreeInfo
from horse_fish.models import AgentState
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
# AgentPool.spawn with run_id tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_spawn_stores_run_id_and_pgid() -> None:
    """spawn() should store run_id and pgid in the database."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    run_id = "test-run-123"
    slot = await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder", run_id=run_id)

    assert slot.run_id == run_id
    assert slot.pgid == 1234
    assert slot.pid == 1234

    # Verify stored in database
    agents = pool.list_agents()
    assert len(agents) == 1
    assert agents[0].run_id == run_id
    assert agents[0].pgid == 1234


@pytest.mark.asyncio
async def test_spawn_without_run_id() -> None:
    """spawn() should work without run_id (backward compatibility)."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    slot = await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder")

    assert slot.run_id is None
    assert slot.pgid == 1234


# ---------------------------------------------------------------------------
# AgentPool.kill_agents_for_run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_kill_agents_for_run_sends_sigterm_then_sigkill() -> None:
    """kill_agents_for_run should send SIGTERM, wait, then SIGKILL."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    run_id = "test-run-123"
    await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder", run_id=run_id)

    with patch("os.killpg") as mock_killpg:
        # First call (SIGTERM) succeeds, second call (signal 0) shows process gone
        mock_killpg.side_effect = [None, ProcessLookupError("No such process")]

        result = await pool.kill_agents_for_run(run_id, sigterm_timeout=0.1)

        assert result["killed"] == 1
        assert result["failed"] == 0
        assert result["timed_out"] == 0

        # Should send SIGTERM (signal 15)
        mock_killpg.assert_any_call(1234, signal.SIGTERM)
        # Should check if alive with signal 0
        mock_killpg.assert_any_call(1234, 0)

    # Verify tmux session was killed
    tmux.kill_session.assert_awaited_once_with("hf-agent-1")

    # Verify agent state updated to dead
    agents = pool.list_agents()
    assert agents[0].state == AgentState.dead


@pytest.mark.asyncio
async def test_kill_agents_for_run_sends_sigkill_if_process_still_alive() -> None:
    """kill_agents_for_run should send SIGKILL if process doesn't terminate after SIGTERM."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    run_id = "test-run-123"
    await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder", run_id=run_id)

    with patch("os.killpg") as mock_killpg:
        # SIGTERM succeeds, signal 0 shows still alive, SIGKILL succeeds
        mock_killpg.side_effect = [None, None, None]

        result = await pool.kill_agents_for_run(run_id, sigterm_timeout=0.01)

        assert result["killed"] == 0
        assert result["timed_out"] == 1
        assert result["failed"] == 0

        # Should send SIGTERM
        mock_killpg.assert_any_call(1234, signal.SIGTERM)
        # Should check if alive with signal 0
        mock_killpg.assert_any_call(1234, 0)
        # Should send SIGKILL
        mock_killpg.assert_any_call(1234, signal.SIGKILL)


@pytest.mark.asyncio
async def test_kill_agents_for_run_force_mode() -> None:
    """kill_agents_for_run with force=True should skip SIGTERM and send SIGKILL immediately."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    run_id = "test-run-123"
    await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder", run_id=run_id)

    with patch("os.killpg") as mock_killpg:
        # SIGKILL succeeds, signal 0 shows process gone
        mock_killpg.side_effect = [None, ProcessLookupError("No such process")]

        result = await pool.kill_agents_for_run(run_id, force=True)

        assert result["killed"] == 1
        assert result["failed"] == 0
        assert result["timed_out"] == 0

        # Should NOT send SIGTERM
        for call in mock_killpg.call_args_list:
            assert call.args[1] != signal.SIGTERM, "Should not send SIGTERM in force mode"


@pytest.mark.asyncio
async def test_kill_agents_for_run_no_agents() -> None:
    """kill_agents_for_run should return empty result if no agents for run."""
    store = make_store()
    tmux = MagicMock()
    worktrees = MagicMock()

    pool = make_pool(store, tmux, worktrees)
    result = await pool.kill_agents_for_run("nonexistent-run")

    assert result["killed"] == 0
    assert result["failed"] == 0
    assert result["timed_out"] == 0
    assert result["agents"] == []


@pytest.mark.asyncio
async def test_kill_agents_for_run_only_targets_specific_run() -> None:
    """kill_agents_for_run should only kill agents for the specified run."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(
        side_effect=[
            SpawnResult(pid=1000, pgid=1000),
            SpawnResult(pid=2000, pgid=2000),
        ]
    )
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(
        side_effect=[
            make_worktree_info("agent-1"),
            make_worktree_info("agent-2"),
        ]
    )

    pool = make_pool(store, tmux, worktrees)
    run_id_1 = "test-run-1"
    run_id_2 = "test-run-2"

    await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder", run_id=run_id_1)
    await pool.spawn("agent-2", "claude", "claude-sonnet-4-6", "builder", run_id=run_id_2)

    with patch("os.killpg") as mock_killpg:
        mock_killpg.side_effect = [None, ProcessLookupError("No such process")]

        result = await pool.kill_agents_for_run(run_id_1, sigterm_timeout=0.01)

        assert result["killed"] == 1

        # Should only kill pgid 1000, not 2000
        mock_killpg.assert_any_call(1000, signal.SIGTERM)
        assert mock_killpg.call_count == 2  # SIGTERM and signal 0 check

    # Verify only agent-1 is marked dead
    agents = pool.list_agents()
    agent_states = {a.name: a.state for a in agents}
    assert agent_states["agent-1"] == AgentState.dead
    assert agent_states["agent-2"] != AgentState.dead


@pytest.mark.asyncio
async def test_kill_agents_for_run_handles_already_dead_process() -> None:
    """kill_agents_for_run should handle processes that are already gone."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=1234))
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    run_id = "test-run-123"
    await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder", run_id=run_id)

    with patch("os.killpg") as mock_killpg:
        # ProcessLookupError on SIGTERM (process already gone)
        mock_killpg.side_effect = ProcessLookupError("No such process")

        result = await pool.kill_agents_for_run(run_id, sigterm_timeout=0.01)

        assert result["killed"] == 1  # Counted as killed since it's gone
        assert result["failed"] == 0


@pytest.mark.asyncio
async def test_kill_agents_for_run_handles_missing_pgid() -> None:
    """kill_agents_for_run should handle agents with no pgid gracefully."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(return_value=SpawnResult(pid=1234, pgid=None))  # type: ignore[arg-type]
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    worktrees = MagicMock()
    worktrees.create = AsyncMock(return_value=make_worktree_info("agent-1"))

    pool = make_pool(store, tmux, worktrees)
    run_id = "test-run-123"
    await pool.spawn("agent-1", "claude", "claude-sonnet-4-6", "builder", run_id=run_id)

    result = await pool.kill_agents_for_run(run_id)

    # Should not crash, just return empty result for that agent
    assert result["killed"] == 0
    assert result["failed"] == 0


@pytest.mark.asyncio
async def test_kill_agents_for_run_multiple_agents() -> None:
    """kill_agents_for_run should kill multiple agents for the same run."""
    store = make_store()
    tmux = MagicMock()
    tmux.spawn = AsyncMock(
        side_effect=[
            SpawnResult(pid=1000, pgid=1000),
            SpawnResult(pid=2000, pgid=2000),
            SpawnResult(pid=3000, pgid=3000),
        ]
    )
    tmux.capture_pane = AsyncMock(return_value="Ready\n❯ \n")
    tmux.kill_session = AsyncMock()
    worktrees = MagicMock()
    worktrees.create = AsyncMock(
        side_effect=[
            make_worktree_info("agent-1"),
            make_worktree_info("agent-2"),
            make_worktree_info("agent-3"),
        ]
    )

    pool = make_pool(store, tmux, worktrees)
    run_id = "test-run-123"

    await pool.spawn("agent-1", "claude", "model", "builder", run_id=run_id)
    await pool.spawn("agent-2", "claude", "model", "builder", run_id=run_id)
    await pool.spawn("agent-3", "claude", "model", "builder", run_id=run_id)

    # Get the agents to determine their order in the database
    agents = pool.list_agents()
    assert len(agents) == 3

    def mock_killpg(pgid, sig):
        # First pass: SIGTERM (signal 15) - succeeds
        # Second pass: signal 0 check - raises ProcessLookupError (process gone)
        if sig == 0:
            raise ProcessLookupError("No such process")
        return None

    with patch("os.killpg", side_effect=mock_killpg):
        result = await pool.kill_agents_for_run(run_id, sigterm_timeout=0.01)

        assert result["killed"] == 3
        assert result["timed_out"] == 0
        assert result["failed"] == 0
        # Verify all agents are reported
        agent_names = {a["name"] for a in result["agents"]}
        assert agent_names == {"agent-1", "agent-2", "agent-3"}
        for agent in result["agents"]:
            assert agent["status"] in ("sigterm_ok", "already_gone")


# ---------------------------------------------------------------------------
# RunManager integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_manager_cancel_kills_agents(tmp_path) -> None:
    """RunManager.cancel should kill agents associated with the run."""
    from horse_fish.orchestrator.run_manager import RunManager

    db_path = str(tmp_path / "test.db")
    manager = RunManager(db_path=db_path, max_concurrent_runs=1)

    # Submit a run
    run_id = await manager.submit("Test task")

    # Mock _kill_agents_for_run to verify it's called
    with patch.object(manager, "_kill_agents_for_run") as mock_kill:
        mock_kill.return_value = None

        # Cancel the queued run (won't call _kill_agents_for_run for queued)
        result = await manager.cancel(run_id)
        assert result is True

        # For queued runs, _kill_agents_for_run shouldn't be called
        mock_kill.assert_not_called()

    manager._store.close()


@pytest.mark.asyncio
async def test_run_manager_cancel_calls_kill_for_active_run(tmp_path) -> None:
    """RunManager.cancel should call _kill_agents_for_run for active runs."""
    from horse_fish.orchestrator.run_manager import RunManager

    db_path = str(tmp_path / "test.db")
    manager = RunManager(db_path=db_path, max_concurrent_runs=1)

    # Insert a run as 'planning' (active state)
    manager._store.insert_queued_run("run-123", "Test task")
    manager._store.update_run_state("run-123", "planning")

    # Mock _kill_agents_for_run
    with patch.object(manager, "_kill_agents_for_run") as mock_kill:
        mock_kill.return_value = None

        # Create a mock active task
        async def mock_task():
            await asyncio.sleep(10)
            return None

        import asyncio

        task = asyncio.create_task(mock_task())
        manager._active_tasks["run-123"] = task

        # Cancel should call _kill_agents_for_run
        result = await manager.cancel("run-123")
        assert result is True

        mock_kill.assert_awaited_once_with("run-123")

        # Clean up
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    manager._store.close()


# ---------------------------------------------------------------------------
# TmuxManager SpawnResult
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tmux_manager_returns_spawn_result() -> None:
    """TmuxManager.spawn should return SpawnResult with pid and pgid."""
    from horse_fish.agents.tmux import TmuxManager

    tmux = TmuxManager()

    with patch.object(tmux, "_run_tmux") as mock_run:
        # Mock successful tmux new-session
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # new-session
            MagicMock(returncode=0, stdout="12345\n", stderr=""),  # list-panes
        ]

        with patch.object(tmux, "_get_pgid") as mock_get_pgid:
            mock_get_pgid.return_value = 12345

            result = await tmux.spawn("test-session", "echo hello", "/tmp")

            assert result.pid == 12345
            assert result.pgid == 12345


@pytest.mark.asyncio
async def test_tmux_manager_get_pgid() -> None:
    """TmuxManager._get_pgid should return the process group ID."""
    from horse_fish.agents.tmux import TmuxManager

    tmux = TmuxManager()

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"12345\n", b""))
        mock_exec.return_value = mock_proc

        pgid = await tmux._get_pgid(9999)

        assert pgid == 12345
        mock_exec.assert_called_once()
        call_args = mock_exec.call_args
        assert call_args[0][0] == "ps"
        assert call_args[0][1] == "-o"
        assert call_args[0][2] == "pgid="
        assert call_args[0][3] == "-p"
        assert call_args[0][4] == "9999"


@pytest.mark.asyncio
async def test_tmux_manager_get_pgid_fallback() -> None:
    """TmuxManager._get_pgid should fallback to PID if ps fails."""
    from horse_fish.agents.tmux import TmuxManager

    tmux = TmuxManager()

    with patch("asyncio.create_subprocess_exec") as mock_exec:
        mock_proc = MagicMock()
        mock_proc.returncode = 1  # ps failed
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
        mock_exec.return_value = mock_proc

        pgid = await tmux._get_pgid(9999)

        # Should fallback to the PID itself
        assert pgid == 9999
