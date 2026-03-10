"""Tests for improved merge conflict handling."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.merge.queue import MergeQueue, MergeResult
from horse_fish.store.db import Store


@pytest.fixture
def temp_store() -> Store:
    """Create a temporary SQLite store for testing."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    store = Store(db_path)
    store.migrate()
    yield store
    store.close()
    Path(db_path).unlink(missing_ok=True)


@pytest.fixture
def mock_worktrees() -> MagicMock:
    """Create a mock WorktreeManager."""
    worktrees = MagicMock()
    worktrees.merge = AsyncMock(return_value=(True, []))
    return worktrees


class TestWorktreeMergeConflictFiles:
    """Tests for worktree merge returning conflict file information."""

    @pytest.mark.asyncio
    async def test_merge_returns_conflict_files(self) -> None:
        """Worktree merge returns a list of conflicting file names on failure."""
        worktrees = MagicMock()
        conflict_files = ["src/main.py", "tests/test_main.py", "README.md"]
        worktrees.merge = AsyncMock(return_value=(False, conflict_files))

        success, files = await worktrees.merge("test-agent")

        assert success is False
        assert files == ["src/main.py", "tests/test_main.py", "README.md"]
        assert len(files) == 3

    @pytest.mark.asyncio
    async def test_merge_success_returns_empty_conflicts(self) -> None:
        """Successful merge returns an empty conflict file list."""
        worktrees = MagicMock()
        worktrees.merge = AsyncMock(return_value=(True, []))

        success, files = await worktrees.merge("test-agent")

        assert success is True
        assert files == []


class TestMergeQueueConflictDetails:
    """Tests for merge queue including conflict details in results."""

    @pytest.fixture
    def queue(self, temp_store: Store, mock_worktrees: MagicMock) -> MergeQueue:
        """Create a MergeQueue instance for testing."""
        return MergeQueue(worktrees=mock_worktrees, store=temp_store)

    @pytest.mark.asyncio
    async def test_merge_queue_includes_conflict_details(self, queue: MergeQueue, mock_worktrees: MagicMock) -> None:
        """Queue result includes conflict file names when merge fails."""
        conflict_files = ["src/app.py", "src/utils.py"]
        mock_worktrees.merge = AsyncMock(return_value=(False, conflict_files))

        await queue.enqueue("task-conflict", "agent-x", "horse-fish/agent-x")
        results = await queue.process()

        assert len(results) == 1
        result = results[0]
        assert result.success is False
        assert result.conflict_files == ["src/app.py", "src/utils.py"]
        assert result.subtask_id == "task-conflict"

    @pytest.mark.asyncio
    async def test_conflict_resolution_hint(self, queue: MergeQueue, mock_worktrees: MagicMock) -> None:
        """Result includes a resolution hint describing conflicting files."""
        conflict_files = ["src/models.py", "src/views.py"]
        mock_worktrees.merge = AsyncMock(return_value=(False, conflict_files))

        await queue.enqueue("task-hint", "agent-y", "horse-fish/agent-y")
        results = await queue.process()

        result = results[0]
        assert result.resolution_hint != ""
        assert "src/models.py" in result.resolution_hint
        assert "src/views.py" in result.resolution_hint
        assert "2 file(s)" in result.resolution_hint

    @pytest.mark.asyncio
    async def test_conflict_resolution_hint_empty_files(self, queue: MergeQueue, mock_worktrees: MagicMock) -> None:
        """Resolution hint handles case where no specific files are identified."""
        mock_worktrees.merge = AsyncMock(return_value=(False, []))

        await queue.enqueue("task-empty", "agent-z", "horse-fish/agent-z")
        results = await queue.process()

        result = results[0]
        assert result.success is False
        assert result.conflict_files == []
        assert "no specific files" in result.resolution_hint.lower()

    @pytest.mark.asyncio
    async def test_success_result_has_no_hint(self, queue: MergeQueue, mock_worktrees: MagicMock) -> None:
        """Successful merge result has empty resolution hint."""
        mock_worktrees.merge = AsyncMock(return_value=(True, []))

        await queue.enqueue("task-ok", "agent-ok", "horse-fish/agent-ok")
        results = await queue.process()

        result = results[0]
        assert result.success is True
        assert result.conflict_files == []
        assert result.resolution_hint == ""

    @pytest.mark.asyncio
    async def test_merge_result_dataclass_with_hint(self) -> None:
        """MergeResult stores resolution_hint field correctly."""
        result = MergeResult(
            subtask_id="task-1",
            branch="horse-fish/agent-1",
            success=False,
            conflict_files=["a.py", "b.py"],
            resolution_hint="Merge conflict in 2 file(s): a.py, b.py. Resolve conflicts and re-enqueue.",
        )
        assert result.resolution_hint.startswith("Merge conflict")
        assert "a.py" in result.resolution_hint
        assert "b.py" in result.resolution_hint

    @pytest.mark.asyncio
    async def test_mixed_success_and_conflict(self, queue: MergeQueue, mock_worktrees: MagicMock) -> None:
        """Queue with mixed success/conflict results populates conflict details only for failures."""
        call_count = 0

        async def mock_merge(name: str) -> tuple[bool, list[str]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return True, []
            return False, ["src/shared.py"]

        mock_worktrees.merge = AsyncMock(side_effect=mock_merge)

        await queue.enqueue("task-ok", "agent-a", "horse-fish/agent-a")
        await queue.enqueue("task-fail", "agent-b", "horse-fish/agent-b")

        results = await queue.process()

        assert results[0].success is True
        assert results[0].conflict_files == []
        assert results[0].resolution_hint == ""

        assert results[1].success is False
        assert results[1].conflict_files == ["src/shared.py"]
        assert "src/shared.py" in results[1].resolution_hint
