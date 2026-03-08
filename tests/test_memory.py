"""Tests for memory module with mocked memvid."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from horse_fish.memory.store import MemoryHit, MemoryStore
from horse_fish.models import Run, Subtask, SubtaskResult


class MockMemvidClient:
    """Mock memvid client for testing."""

    def __init__(self, path: str):
        self.path = path
        self._chunks: dict[str, dict] = {}
        self._counter = 0

    def add(self, content: str, metadata: dict | None = None) -> str:
        """Mock add method."""
        chunk_id = f"chunk_{self._counter}"
        self._counter += 1
        self._chunks[chunk_id] = {
            "content": content,
            "metadata": metadata or {},
        }
        return chunk_id

    def search(self, query: str, top_k: int = 5) -> list:
        """Mock search method."""
        # Return mock results based on query
        results = []
        for chunk_id, data in self._chunks.items():
            # Simple mock: return all chunks with decreasing scores
            score = 0.9 - len(results) * 0.1
            results.append(
                MagicMock(
                    id=chunk_id,
                    content=data["content"],
                    score=score,
                    metadata=data["metadata"],
                )
            )
        return sorted(results, key=lambda x: x.score, reverse=True)[:top_k]

    def close(self) -> None:
        """Mock close method."""
        pass


@pytest.fixture
def temp_data_dir(tmp_path: Path) -> Path:
    """Create a temporary data directory."""
    return tmp_path / "memory"


@pytest.fixture
def mock_memvid_module():
    """Create and inject a mock memvid module."""
    # Create mock module
    mock_memvid = MagicMock()
    mock_memvid.Memvid = MockMemvidClient

    # Inject into sys.modules before importing store
    sys.modules["memvid"] = mock_memvid

    yield mock_memvid

    # Cleanup
    if "memvid" in sys.modules:
        del sys.modules["memvid"]


@pytest.fixture
def sample_run() -> Run:
    """Create a sample Run object."""
    run = Run.create(task="Build a memory module")
    run.subtasks = [
        Subtask.create("Create store.py"),
        Subtask.create("Write tests"),
    ]
    run.state = "completed"
    run.completed_at = datetime.now(UTC)
    return run


@pytest.fixture
def sample_subtask_results() -> list[SubtaskResult]:
    """Create sample subtask results."""
    return [
        SubtaskResult(
            subtask_id="task-1",
            success=True,
            output="Created store.py successfully",
            diff="diff content here",
            duration_seconds=10.5,
        ),
        SubtaskResult(
            subtask_id="task-2",
            success=True,
            output="Tests passing",
            diff="",
            duration_seconds=5.2,
        ),
    ]


class TestMemoryHit:
    """Tests for MemoryHit model."""

    def test_memory_hit_creation(self):
        """Test creating a MemoryHit."""
        hit = MemoryHit(
            chunk_id="test-123",
            content="test content",
            score=0.95,
            metadata={"key": "value"},
        )
        assert hit.chunk_id == "test-123"
        assert hit.content == "test content"
        assert hit.score == 0.95
        assert hit.metadata == {"key": "value"}

    def test_memory_hit_empty_metadata(self):
        """Test MemoryHit with empty metadata."""
        hit = MemoryHit(
            chunk_id="test-456",
            content="more content",
            score=0.85,
            metadata={},
        )
        assert hit.metadata == {}


class TestMemoryStore:
    """Tests for MemoryStore class."""

    @pytest.mark.asyncio
    async def test_store_initialization(self, temp_data_dir: Path, mock_memvid_module):
        """Test MemoryStore initialization."""
        store = MemoryStore(data_dir=temp_data_dir)
        assert store._data_dir == temp_data_dir
        assert store._db_path == temp_data_dir / "knowledge.mv2"
        assert store._client is None

    @pytest.mark.asyncio
    async def test_store_default_data_dir(self, mock_memvid_module):
        """Test MemoryStore with default data directory."""
        store = MemoryStore()
        expected_dir = Path.home() / ".horse-fish" / "memory"
        assert store._data_dir == expected_dir
        assert store._db_path == expected_dir / "knowledge.mv2"

    @pytest.mark.asyncio
    async def test_store_content(self, temp_data_dir: Path, mock_memvid_module):
        """Test storing content."""
        store = MemoryStore(data_dir=temp_data_dir)
        chunk_id = await store.store("test content", {"key": "value"})

        assert chunk_id == "chunk_0"
        assert store._client is not None

    @pytest.mark.asyncio
    async def test_store_content_no_metadata(self, temp_data_dir: Path, mock_memvid_module):
        """Test storing content without metadata."""
        store = MemoryStore(data_dir=temp_data_dir)
        chunk_id = await store.store("test content")

        assert chunk_id == "chunk_0"

    @pytest.mark.asyncio
    async def test_search(self, temp_data_dir: Path, mock_memvid_module):
        """Test semantic search."""
        store = MemoryStore(data_dir=temp_data_dir)
        await store.store("content 1", {"type": "test"})
        await store.store("content 2", {"type": "test"})

        hits = await store.search("query", top_k=5)

        assert len(hits) == 2
        assert isinstance(hits[0], MemoryHit)
        assert hits[0].chunk_id == "chunk_0"
        assert hits[0].content == "content 1"
        assert hits[0].score > hits[1].score

    @pytest.mark.asyncio
    async def test_search_top_k(self, temp_data_dir: Path, mock_memvid_module):
        """Test search with top_k limit."""
        store = MemoryStore(data_dir=temp_data_dir)
        for i in range(10):
            await store.store(f"content {i}")

        hits = await store.search("query", top_k=3)

        assert len(hits) == 3

    @pytest.mark.asyncio
    async def test_store_run_result(
        self, temp_data_dir: Path, mock_memvid_module, sample_run: Run, sample_subtask_results: list[SubtaskResult]
    ):
        """Test storing run results."""
        store = MemoryStore(data_dir=temp_data_dir)
        await store.store_run_result(sample_run, sample_subtask_results)

        # Should store 1 run summary + 2 subtask results = 3 chunks
        assert store._client is not None

    @pytest.mark.asyncio
    async def test_find_similar_tasks(self, temp_data_dir: Path, mock_memvid_module):
        """Test finding similar tasks."""
        store = MemoryStore(data_dir=temp_data_dir)

        # Store some run results
        await store.store("Run: Build a memory module", {"type": "run_result", "run_id": "run-1"})
        await store.store("Run: Fix validation gates", {"type": "run_result", "run_id": "run-2"})
        await store.store("Run: Add CLI commands", {"type": "run_result", "run_id": "run-3"})

        hits = await store.find_similar_tasks("memory module", top_k=2)

        assert len(hits) <= 2
        for hit in hits:
            assert hit.metadata.get("type") == "run_result"

    @pytest.mark.asyncio
    async def test_find_similar_tasks_deduplication(self, temp_data_dir: Path, mock_memvid_module):
        """Test that find_similar_tasks deduplicates by run_id."""
        store = MemoryStore(data_dir=temp_data_dir)

        # Store multiple chunks for the same run
        await store.store("Run: Test task", {"type": "run_result", "run_id": "run-1"})
        await store.store("Subtask for run-1", {"type": "subtask_result", "run_id": "run-1"})
        await store.store("More about run-1", {"type": "run_result", "run_id": "run-1"})

        hits = await store.find_similar_tasks("test", top_k=5)

        # Should only return unique run_ids
        run_ids = [hit.metadata.get("run_id") for hit in hits]
        assert len(run_ids) == len(set(run_ids))

    @pytest.mark.asyncio
    async def test_close(self, temp_data_dir: Path, mock_memvid_module):
        """Test closing the store."""
        store = MemoryStore(data_dir=temp_data_dir)
        await store.store("test content")

        await store.close()

        assert store._client is None

    @pytest.mark.asyncio
    async def test_close_idempotent(self, temp_data_dir: Path, mock_memvid_module):
        """Test that close can be called multiple times."""
        store = MemoryStore(data_dir=temp_data_dir)

        await store.close()  # No-op when client is None
        await store.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_data_dir_creation(self, temp_data_dir: Path, mock_memvid_module):
        """Test that data directory is created on first use."""
        store = MemoryStore(data_dir=temp_data_dir)
        assert not temp_data_dir.exists()

        await store.store("test content")

        assert temp_data_dir.exists()


class TestMemoryStoreIntegration:
    """Integration-style tests for MemoryStore."""

    @pytest.mark.asyncio
    async def test_full_workflow(self, temp_data_dir: Path, mock_memvid_module):
        """Test complete workflow: store, search, close."""
        store = MemoryStore(data_dir=temp_data_dir)

        # Store some content
        chunk_id = await store.store("Python memory module implementation", {"language": "python"})
        assert chunk_id == "chunk_0"

        # Search for it
        hits = await store.search("Python memory", top_k=5)
        assert len(hits) >= 1
        assert "Python" in hits[0].content

        # Close
        await store.close()
        assert store._client is None
