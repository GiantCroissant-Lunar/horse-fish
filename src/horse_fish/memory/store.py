"""Memory store using memvid-sdk for cross-session learning."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from horse_fish.models import Run, SubtaskResult


class MemoryHit(BaseModel):
    """A search result from memory."""

    chunk_id: str
    content: str
    score: float
    metadata: dict[str, Any]


class MemoryStore:
    """Cross-session memory store using memvid (video-based AI memory).

    Stores task results, agent performance, and solutions for semantic retrieval.
    Uses memvid Python SDK with .mv2 file storage.
    """

    def __init__(self, data_dir: Path | str | None = None):
        """Initialize memvid memory at data_dir/knowledge.mv2.

        Args:
            data_dir: Directory to store .mv2 file. Defaults to .horse-fish/memory/
        """
        if data_dir is None:
            data_dir = Path.home() / ".horse-fish" / "memory"
        else:
            data_dir = Path(data_dir)

        self._data_dir = data_dir
        self._db_path = data_dir / "knowledge.mv2"
        self._client: Any | None = None

    async def _ensure_client(self) -> Any:
        """Lazy initialization of memvid client."""
        if self._client is None:
            from memvid import Memvid

            self._data_dir.mkdir(parents=True, exist_ok=True)
            self._client = Memvid(str(self._db_path))
        return self._client

    async def store(self, content: str, metadata: dict[str, Any] | None = None) -> str:
        """Store a text chunk with metadata.

        Args:
            content: Text content to store.
            metadata: Optional metadata dict.

        Returns:
            chunk_id: Unique identifier for the stored chunk.
        """
        client = await self._ensure_client()
        metadata = metadata or {}
        chunk_id = client.add(content, metadata=metadata)
        return chunk_id

    async def search(self, query: str, top_k: int = 5) -> list[MemoryHit]:
        """Semantic search over stored content.

        Args:
            query: Search query string.
            top_k: Number of results to return.

        Returns:
            List of MemoryHit objects ranked by relevance score.
        """
        client = await self._ensure_client()
        results = client.search(query, top_k=top_k)

        hits = []
        for result in results:
            hits.append(
                MemoryHit(
                    chunk_id=result.id,
                    content=result.content,
                    score=result.score,
                    metadata=result.metadata or {},
                )
            )
        return hits

    async def store_run_result(self, run: Run, subtask_results: list[SubtaskResult]) -> None:
        """Store a completed run's results for future learning.

        Args:
            run: The completed Run object.
            subtask_results: List of SubtaskResult objects from the run.
        """
        # Store run-level summary
        run_metadata = {
            "type": "run_result",
            "run_id": run.id,
            "task": run.task,
            "state": run.state,
            "created_at": run.created_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "subtask_count": len(run.subtasks),
        }

        run_content = f"Run: {run.task}\nState: {run.state}\nSubtasks: {len(run.subtasks)}"
        await self.store(run_content, run_metadata)

        # Store each subtask result
        for result in subtask_results:
            subtask_metadata = {
                "type": "subtask_result",
                "run_id": run.id,
                "subtask_id": result.subtask_id,
                "success": result.success,
                "duration_seconds": result.duration_seconds,
            }

            subtask_content = f"Subtask {result.subtask_id}:\nSuccess: {result.success}\nOutput: {result.output}"
            if result.diff:
                subtask_content += f"\nDiff: {result.diff}"

            await self.store(subtask_content, subtask_metadata)

    async def find_similar_tasks(self, task_description: str, top_k: int = 3) -> list[MemoryHit]:
        """Find past tasks similar to a new one.

        Args:
            task_description: Description of the new task.
            top_k: Number of similar tasks to return.

        Returns:
            List of MemoryHit objects for similar past tasks.
        """
        client = await self._ensure_client()
        # Search for run results with similar task descriptions
        results = client.search(f"Run: {task_description}", top_k=top_k * 2)

        # Filter to only run_result type and deduplicate by run_id
        seen_run_ids: set[str] = set()
        hits: list[MemoryHit] = []

        for result in results:
            metadata = result.metadata or {}
            if metadata.get("type") == "run_result":
                run_id = metadata.get("run_id")
                if run_id and run_id not in seen_run_ids:
                    seen_run_ids.add(run_id)
                    hits.append(
                        MemoryHit(
                            chunk_id=result.id,
                            content=result.content,
                            score=result.score,
                            metadata=metadata,
                        )
                    )
                    if len(hits) >= top_k:
                        break

        return hits

    async def close(self) -> None:
        """Flush and close memvid file."""
        if self._client is not None:
            self._client.close()
            self._client = None
