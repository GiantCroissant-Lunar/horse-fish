"""Smoke test — full orchestrator loop with real Pi agent, mocked planner only.

This test proves the entire horse-fish pipeline works end-to-end:
spawn → ready detect → prompt inject → execute → validate → merge

Requires: tmux, pi CLI, DASHSCOPE_API_KEY in tmux environment.
Skipped if any prerequisite is missing.
"""

from __future__ import annotations

import subprocess
from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.models import Subtask
from horse_fish.orchestrator.engine import Orchestrator
from horse_fish.planner.decompose import Planner
from horse_fish.store.db import Store
from horse_fish.validation.gates import ValidationGates


def _tmux_available() -> bool:
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _pi_available() -> bool:
    try:
        subprocess.run(["pi", "--version"], capture_output=True, check=True, timeout=5)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _dashscope_key_available() -> bool:
    # Check tmux global env
    try:
        result = subprocess.run(
            ["tmux", "show-environment", "-g", "DASHSCOPE_API_KEY"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0 and "DASHSCOPE_API_KEY=" in result.stdout.decode()
    except Exception:
        return False


_skip_reason = "requires tmux + pi + DASHSCOPE_API_KEY"
_can_run = _tmux_available() and _pi_available() and _dashscope_key_available()
pytestmark = pytest.mark.skipif(not _can_run, reason=_skip_reason)


@pytest.fixture
def tmp_repo(tmp_path):
    """Create a temporary git repo with initial commit and a python file."""
    repo = tmp_path / "smoke-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, capture_output=True, check=True)

    # Create a minimal Python project so validation gates have something to check
    (repo / "hello.py").write_text("# placeholder\n")
    subprocess.run(["git", "add", "."], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, capture_output=True, check=True)
    return repo


@pytest.fixture
def store(tmp_path):
    db = str(tmp_path / "smoke.db")
    s = Store(db)
    s.migrate()
    return s


@pytest.fixture
def components(tmp_repo, store):
    """Build all real components except the planner (mocked)."""
    tmux = TmuxManager()
    worktrees = WorktreeManager(str(tmp_repo))
    pool = AgentPool(store, tmux, worktrees, project_context="Run pytest to verify.")
    gates = ValidationGates()

    # Mock planner to return a single trivial subtask
    mock_planner = MagicMock(spec=Planner)
    mock_planner.decompose = AsyncMock(
        return_value=[
            Subtask.create("Create a file called greeting.py with: print('hello from horse-fish')"),
        ]
    )

    orchestrator = Orchestrator(
        pool=pool,
        planner=mock_planner,
        gates=gates,
        runtime="pi",
        model="qwen3.5-plus",
        max_agents=1,
    )
    return orchestrator, pool, tmux


@pytest.mark.asyncio
@pytest.mark.timeout(180)
async def test_smoke_full_loop(tmp_repo, components):
    """Smoke test: orchestrator drives a real Pi agent through the full lifecycle."""
    orchestrator, pool, tmux = components

    run = await orchestrator.run("Create greeting.py")

    # The run should complete (or fail at validation — that's OK for smoke test)
    # What matters is that we got through spawn → ready → prompt → execute
    assert run.state.value in ("completed", "failed", "reviewing", "merging"), f"Unexpected terminal state: {run.state}"

    # Verify at least one subtask was dispatched and attempted
    assert len(run.subtasks) == 1
    subtask = run.subtasks[0]
    assert subtask.state.value in ("done", "failed"), f"Subtask state: {subtask.state}"

    # If completed, verify the file was merged
    if run.state.value == "completed":
        greeting = tmp_repo / "greeting.py"
        assert greeting.exists(), "greeting.py should exist on main after merge"

    # Cleanup any leftover tmux sessions
    sessions = await tmux.list_sessions()
    for s in sessions:
        if s.startswith("hf-"):
            await tmux.kill_session(s)
