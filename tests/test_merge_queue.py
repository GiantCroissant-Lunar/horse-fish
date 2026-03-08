"""Tests for the merge queue module."""

from __future__ import annotations

import asyncio
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
    worktrees.merge = AsyncMock(return_value=True)
    return worktrees


class TestMergeResult:
    """Tests for MergeResult dataclass."""

    def test_success_result(self) -> None:
        """Test creating a successful merge result."""
        result = MergeResult(
            subtask_id="task-123",
            branch="horse-fish/agent-1",
            success=True,
        )
        assert result.subtask_id == "task-123"
        assert result.branch == "horse-fish/agent-1"
        assert result.success is True
        assert result.conflict_files == []

    def test_conflict_result(self) -> None:
        """Test creating a merge result with conflicts."""
        result = MergeResult(
            subtask_id="task-456",
            branch="horse-fish/agent-2",
            success=False,
            conflict_files=["src/main.py", "tests/test_main.py"],
        )
        assert result.success is False
        assert len(result.conflict_files) == 2
        assert "src/main.py" in result.conflict_files


class TestMergeQueue:
    """Tests for MergeQueue."""

    @pytest.fixture
    def queue(self, temp_store: Store, mock_worktrees: MagicMock) -> MergeQueue:
        """Create a MergeQueue instance for testing."""
        return MergeQueue(worktrees=mock_worktrees, store=temp_store)

    @pytest.mark.asyncio
    async def test_enqueue(self, queue: MergeQueue) -> None:
        """Test enqueueing a merge request."""
        await queue.enqueue(
            subtask_id="task-001",
            agent_name="agent-1",
            branch="horse-fish/agent-1",
            priority=0,
        )

        pending = await queue.pending()
        assert len(pending) == 1
        assert pending[0]["subtask_id"] == "task-001"
        assert pending[0]["agent_name"] == "agent-1"
        assert pending[0]["branch"] == "horse-fish/agent-1"
        assert pending[0]["priority"] == 0

    @pytest.mark.asyncio
    async def test_enqueue_with_priority(self, queue: MergeQueue) -> None:
        """Test enqueueing with different priorities."""
        await queue.enqueue("task-low", "agent-low", "horse-fish/low", priority=1)
        await queue.enqueue("task-high", "agent-high", "horse-fish/high", priority=5)
        await queue.enqueue("task-med", "agent-med", "horse-fish/med", priority=3)

        pending = await queue.pending()
        assert len(pending) == 3
        # Should be ordered by priority descending
        assert pending[0]["subtask_id"] == "task-high"
        assert pending[1]["subtask_id"] == "task-med"
        assert pending[2]["subtask_id"] == "task-low"

    @pytest.mark.asyncio
    async def test_process_success(self, queue: MergeQueue, mock_worktrees: MagicMock) -> None:
        """Test processing queue with successful merges."""
        mock_worktrees.merge = AsyncMock(return_value=True)

        await queue.enqueue("task-001", "agent-1", "horse-fish/agent-1")
        await queue.enqueue("task-002", "agent-2", "horse-fish/agent-2")

        results = await queue.process()

        assert len(results) == 2
        assert all(r.success for r in results)

        # Queue should be empty after processing
        pending = await queue.pending()
        assert len(pending) == 0

        # Verify merge was called for each agent
        assert mock_worktrees.merge.call_count == 2
        mock_worktrees.merge.assert_any_call("agent-1")
        mock_worktrees.merge.assert_any_call("agent-2")

    @pytest.mark.asyncio
    async def test_process_with_conflict(self, queue: MergeQueue, mock_worktrees: MagicMock) -> None:
        """Test processing queue when a merge has conflicts."""
        # First merge succeeds, second has conflict
        call_count = 0

        async def mock_merge(name: str) -> bool:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return True
            return False

        mock_worktrees.merge = AsyncMock(side_effect=mock_merge)

        await queue.enqueue("task-001", "agent-1", "horse-fish/agent-1")
        await queue.enqueue("task-002", "agent-2", "horse-fish/agent-2")

        results = await queue.process()

        assert len(results) == 2
        assert results[0].success is True
        assert results[1].success is False
        assert results[1].conflict_files == []

        # First should be removed, second should remain with conflict status
        pending = await queue.pending()
        assert len(pending) == 0  # Both processed, conflict one marked as 'conflict' not 'pending'

    @pytest.mark.asyncio
    async def test_pending_list(self, queue: MergeQueue) -> None:
        """Test getting pending queue entries."""
        await queue.enqueue("task-001", "agent-1", "horse-fish/agent-1", priority=2)
        await queue.enqueue("task-002", "agent-2", "horse-fish/agent-2", priority=1)

        pending = await queue.pending()
        assert len(pending) == 2

        # Verify ordering by priority
        assert pending[0]["subtask_id"] == "task-001"
        assert pending[0]["priority"] == 2
        assert pending[1]["subtask_id"] == "task-002"
        assert pending[1]["priority"] == 1

    @pytest.mark.asyncio
    async def test_clear(self, queue: MergeQueue) -> None:
        """Test clearing the merge queue."""
        await queue.enqueue("task-001", "agent-1", "horse-fish/agent-1")
        await queue.enqueue("task-002", "agent-2", "horse-fish/agent-2")
        await queue.enqueue("task-003", "agent-3", "horse-fish/agent-3")

        pending_before = await queue.pending()
        assert len(pending_before) == 3

        await queue.clear()

        pending_after = await queue.pending()
        assert len(pending_after) == 0

    @pytest.mark.asyncio
    async def test_process_fifo_order_same_priority(self, queue: MergeQueue) -> None:
        """Test that entries with same priority are processed in FIFO order."""
        # Enqueue in specific order
        await queue.enqueue("task-first", "agent-1", "horse-fish/agent-1", priority=1)
        await asyncio.sleep(0.01)  # Ensure different timestamps
        await queue.enqueue("task-second", "agent-2", "horse-fish/agent-2", priority=1)
        await asyncio.sleep(0.01)
        await queue.enqueue("task-third", "agent-3", "horse-fish/agent-3", priority=1)

        pending = await queue.pending()
        assert pending[0]["subtask_id"] == "task-first"
        assert pending[1]["subtask_id"] == "task-second"
        assert pending[2]["subtask_id"] == "task-third"

    @pytest.mark.asyncio
    async def test_process_returns_results_in_order(self, queue: MergeQueue, mock_worktrees: MagicMock) -> None:
        """Test that process returns results in the order they were processed."""
        mock_worktrees.merge = AsyncMock(return_value=True)

        await queue.enqueue("task-a", "agent-a", "horse-fish/agent-a", priority=2)
        await queue.enqueue("task-b", "agent-b", "horse-fish/agent-b", priority=5)
        await queue.enqueue("task-c", "agent-c", "horse-fish/agent-c", priority=2)

        results = await queue.process()

        # Should be processed in priority order: task-b (5), task-a (2), task-c (2)
        assert len(results) == 3
        assert results[0].subtask_id == "task-b"
        assert results[1].subtask_id == "task-a"
        assert results[2].subtask_id == "task-c"
