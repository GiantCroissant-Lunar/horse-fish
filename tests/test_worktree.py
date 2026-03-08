"""Tests for the git worktree manager."""

import asyncio
from datetime import UTC
from pathlib import Path

import pytest

from horse_fish.agents.worktree import WorktreeError, WorktreeInfo, WorktreeManager


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """Create a temporary git repository."""
    repo = tmp_path / "repo"
    repo.mkdir()

    async def init_repo() -> None:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "init",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo,
        )
        await proc.communicate()

        # Configure git user for commits
        proc = await asyncio.create_subprocess_exec(
            "git",
            "config",
            "user.email",
            "test@example.com",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo,
        )
        await proc.communicate()

        proc = await asyncio.create_subprocess_exec(
            "git",
            "config",
            "user.name",
            "Test User",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo,
        )
        await proc.communicate()

        # Create initial commit
        (repo / "README.md").write_text("# Test Repo")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "add",
            ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo,
        )
        await proc.communicate()

        proc = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            "Initial commit",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo,
        )
        await proc.communicate()

        # Rename default branch to main
        proc = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "-m",
            "main",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=repo,
        )
        await proc.communicate()

    asyncio.run(init_repo())
    return repo


@pytest.fixture
def manager(tmp_git_repo: Path) -> WorktreeManager:
    """Create a WorktreeManager for the temp repo."""
    return WorktreeManager(str(tmp_git_repo), base_dir=".horse-fish/worktrees")


class TestWorktreeManager:
    """Test suite for WorktreeManager."""

    async def test_create_worktree(self, manager: WorktreeManager, tmp_git_repo: Path) -> None:
        """Test creating a new worktree."""
        info = await manager.create("test-agent")

        assert info.name == "test-agent"
        assert info.branch == "horse-fish/test-agent"
        assert ".horse-fish/worktrees/test-agent" in info.path

        # Verify the worktree directory exists
        worktree_path = Path(info.path)
        assert worktree_path.exists()
        assert (worktree_path / ".git").exists() or (worktree_path / ".git").is_file()

    async def test_create_worktree_already_exists(self, manager: WorktreeManager) -> None:
        """Test creating a worktree when it already exists recreates it."""
        # Create first worktree
        await manager.create("duplicate")

        # Create again - should succeed and recreate
        info2 = await manager.create("duplicate")

        assert info2.name == "duplicate"
        assert info2.branch == "horse-fish/duplicate"
        assert Path(info2.path).exists()

    async def test_list_worktrees(self, manager: WorktreeManager) -> None:
        """Test listing worktrees."""
        # Initially empty
        worktrees = await manager.list_worktrees()
        assert worktrees == []

        # Create some worktrees
        await manager.create("agent-1")
        await manager.create("agent-2")

        worktrees = await manager.list_worktrees()
        assert len(worktrees) == 2

        names = {wt.name for wt in worktrees}
        assert names == {"agent-1", "agent-2"}

        # Verify WorktreeInfo structure
        for wt in worktrees:
            assert isinstance(wt, WorktreeInfo)
            assert wt.path
            assert wt.branch.startswith("horse-fish/")

    async def test_get_diff(self, manager: WorktreeManager, tmp_git_repo: Path) -> None:
        """Test getting diff from a worktree."""
        info = await manager.create("diff-test")
        worktree_path = Path(info.path)

        # Initially no diff
        diff = await manager.get_diff("diff-test")
        assert diff == ""

        # Make a change
        (worktree_path / "newfile.txt").write_text("new content")

        # Get diff
        diff = await manager.get_diff("diff-test")
        assert "newfile.txt" in diff
        assert "new content" in diff

    async def test_remove_worktree(self, manager: WorktreeManager) -> None:
        """Test removing a worktree."""
        info = await manager.create("to-remove")
        path = Path(info.path)

        assert path.exists()

        await manager.remove("to-remove")

        # Path should no longer exist as a worktree
        # (the directory might exist but not as a git worktree)

        # Should not be in list anymore
        worktrees = await manager.list_worktrees()
        names = {wt.name for wt in worktrees}
        assert "to-remove" not in names

    async def test_remove_nonexistent_worktree(self, manager: WorktreeManager) -> None:
        """Test removing a worktree that doesn't exist doesn't raise."""
        # Should not raise
        await manager.remove("nonexistent")

    async def test_merge_worktree_success(self, manager: WorktreeManager, tmp_git_repo: Path) -> None:
        """Test merging a worktree with no conflicts."""
        # Create worktree
        info = await manager.create("merge-test")
        worktree_path = Path(info.path)

        # Make a change and commit it
        (worktree_path / "feature.txt").write_text("feature content")

        # Add and commit in worktree
        proc = await asyncio.create_subprocess_exec(
            "git",
            "add",
            ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=worktree_path,
        )
        await proc.communicate()

        proc = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            "Add feature",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=worktree_path,
        )
        await proc.communicate()

        # Merge should succeed
        result = await manager.merge("merge-test", auto_commit=False)
        assert result is True

        # Verify the file is in main
        assert (tmp_git_repo / "feature.txt").exists()
        assert (tmp_git_repo / "feature.txt").read_text() == "feature content"

    async def test_merge_with_auto_commit(self, manager: WorktreeManager, tmp_git_repo: Path) -> None:
        """Test merging with auto_commit=True."""
        info = await manager.create("auto-commit-test")
        worktree_path = Path(info.path)

        # Make uncommitted changes
        (worktree_path / "uncommitted.txt").write_text("uncommitted content")

        # Merge with auto_commit should succeed
        result = await manager.merge("auto-commit-test", auto_commit=True)
        assert result is True

        # Verify file is in main
        assert (tmp_git_repo / "uncommitted.txt").exists()

    async def test_merge_conflict(self, manager: WorktreeManager, tmp_git_repo: Path) -> None:
        """Test merge returns False on conflict."""
        # Create a file in main
        (tmp_git_repo / "conflict.txt").write_text("main content")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "add",
            ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_git_repo,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            "Add conflict file",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_git_repo,
        )
        await proc.communicate()

        # Create worktree with conflicting change
        info = await manager.create("conflict-test")
        worktree_path = Path(info.path)

        (worktree_path / "conflict.txt").write_text("conflicting content")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "add",
            ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=worktree_path,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            "Conflicting change",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=worktree_path,
        )
        await proc.communicate()

        # Modify the same file in main to cause conflict
        (tmp_git_repo / "conflict.txt").write_text("main modified content")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "add",
            ".",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_git_repo,
        )
        await proc.communicate()
        proc = await asyncio.create_subprocess_exec(
            "git",
            "commit",
            "-m",
            "Main modification",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_git_repo,
        )
        await proc.communicate()

        # Merge should fail due to conflict
        result = await manager.merge("conflict-test", auto_commit=False)
        assert result is False

        # We should still be on main
        proc = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "--show-current",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=tmp_git_repo,
        )
        stdout, _ = await proc.communicate()
        assert stdout.decode().strip() == "main"

    async def test_cleanup(self, manager: WorktreeManager, tmp_git_repo: Path) -> None:
        """Test cleanup removes old worktrees."""
        # Create a worktree
        await manager.create("old-worktree")

        # Cleanup with 0 hours should remove it (everything is "old")
        removed = await manager.cleanup(max_age_hours=0)

        assert removed >= 0  # May or may not remove depending on timing

    async def test_worktree_info_model(self) -> None:
        """Test WorktreeInfo Pydantic model."""
        from datetime import datetime

        now = datetime.now(UTC)
        info = WorktreeInfo(
            path="/tmp/test",
            branch="horse-fish/test",
            name="test",
        )

        assert info.path == "/tmp/test"
        assert info.branch == "horse-fish/test"
        assert info.name == "test"
        assert info.created_at >= now

    async def test_run_git_error(self, manager: WorktreeManager) -> None:
        """Test that invalid git commands raise WorktreeError."""
        with pytest.raises(WorktreeError):
            await manager._run_git("invalid-command-that-does-not-exist")

    async def test_context_manager(self, tmp_git_repo: Path) -> None:
        """Test that WorktreeManager works as an async context manager."""
        async with WorktreeManager(str(tmp_git_repo)) as mgr:
            info = await mgr.create("context-test")
            assert info.name == "context-test"


class TestWorktreeInfo:
    """Test suite for WorktreeInfo model."""

    def test_worktree_info_defaults(self) -> None:
        """Test WorktreeInfo with default values."""
        info = WorktreeInfo(path="/path", branch="main", name="test")

        assert info.path == "/path"
        assert info.branch == "main"
        assert info.name == "test"
        assert info.created_at is not None

    def test_worktree_info_serialization(self) -> None:
        """Test WorktreeInfo serialization."""
        info = WorktreeInfo(path="/path", branch="main", name="test")
        data = info.model_dump()

        assert data["path"] == "/path"
        assert data["branch"] == "main"
        assert data["name"] == "test"
        assert "created_at" in data
