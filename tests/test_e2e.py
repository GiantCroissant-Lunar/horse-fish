"""End-to-end integration tests with real tmux."""

import asyncio
import subprocess
from pathlib import Path

import pytest

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.store.db import Store


def _tmux_available() -> bool:
    """Check if tmux is available on the system."""
    try:
        subprocess.run(["tmux", "-V"], capture_output=True, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


pytestmark = pytest.mark.skipif(not _tmux_available(), reason="tmux not available")


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Create a temporary git repository with a main branch."""
    repo_path = tmp_path / "test_repo"
    repo_path.mkdir()
    subprocess.run(["git", "init", "--initial-branch=main"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo_path,
        check=True,
        capture_output=True,
    )
    # Create initial commit with README.md
    readme = repo_path / "README.md"
    readme.write_text("# Test Repository\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo_path, check=True, capture_output=True)
    return repo_path


@pytest.fixture
def store(tmp_path: Path) -> Store:
    """Create a temporary SQLite store."""
    db_path = tmp_path / "test.db"
    store = Store(str(db_path))
    store.migrate()
    return store


@pytest.fixture
def tmux() -> TmuxManager:
    """Create a TmuxManager instance."""
    return TmuxManager()


@pytest.fixture
def worktrees(tmp_repo: Path) -> WorktreeManager:
    """Create a WorktreeManager instance."""
    return WorktreeManager(str(tmp_repo))


@pytest.fixture
def pool(store: Store, tmux: TmuxManager, worktrees: WorktreeManager) -> AgentPool:
    """Create an AgentPool instance."""
    return AgentPool(store, tmux, worktrees)


@pytest.mark.asyncio
async def test_e2e_single_subtask_creates_file(pool: AgentPool) -> None:
    """Test that a single subtask can create a file via tmux."""
    # Spawn an agent with unique name to avoid duplicate session errors
    slot = await pool.spawn(name="hf-e2e-single", runtime="claude", model="test", capability="builder")

    try:
        # Send shell command to create a file, git add, and commit
        worktree_path = slot.worktree_path
        commands = [
            f"cd {worktree_path}",
            "echo 'test content' > test_file.txt",
            "git add test_file.txt",
            'git commit -m "Add test file"',
        ]
        for cmd in commands:
            await pool._tmux.send_keys(slot.tmux_session, cmd)
            await asyncio.sleep(0.5)

        # Poll for result
        result = None
        for _ in range(10):
            await asyncio.sleep(1)
            result = await pool.collect_result(slot.id)
            if result is not None:
                break

        # Assert result
        assert result is not None, "No result collected from agent"
        assert result.diff is not None, "Expected diff in result"
        assert result.success is True, f"Expected success, got: {result}"

    finally:
        # Cleanup
        await pool.release(slot.id)


@pytest.mark.asyncio
async def test_e2e_worktree_isolation(pool: AgentPool, tmp_repo: Path) -> None:
    """Test that worktree isolation is properly maintained."""
    # Spawn an agent with unique name
    slot = await pool.spawn(name="hf-e2e-isolation", runtime="claude", model="test", capability="builder")

    try:
        # Assert worktree isolation
        assert slot.worktree_path is not None, "Expected worktree_path to be set"
        assert slot.worktree_path != str(tmp_repo), "Worktree should differ from main repo"
        assert Path(slot.worktree_path).exists(), f"Worktree path does not exist: {slot.worktree_path}"

        # Assert main repo still has README.md
        main_readme = tmp_repo / "README.md"
        assert main_readme.exists(), "Main repo README.md should still exist"

    finally:
        # Cleanup
        await pool.release(slot.id)
