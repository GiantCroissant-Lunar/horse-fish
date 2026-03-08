"""FIFO merge queue for managing ordered merging of completed subtask worktrees."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from horse_fish.agents.worktree import WorktreeManager
from horse_fish.store.db import Store


@dataclass
class MergeResult:
    """Result of a merge operation."""

    subtask_id: str
    branch: str
    success: bool
    conflict_files: list[str] = field(default_factory=list)


class MergeQueue:
    """Manages a FIFO queue of merge operations with priority support.

    The merge queue stores pending merges in SQLite and processes them in
    priority/FIFO order. Higher priority entries are merged first; within
    the same priority, entries are processed in FIFO order.
    """

    def __init__(self, worktrees: WorktreeManager, store: Store) -> None:
        """Initialize the merge queue.

        Args:
            worktrees: WorktreeManager for performing merge operations.
            store: Store for persisting queue state.
        """
        self.worktrees = worktrees
        self.store = store
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Ensure the merge_queue table exists."""
        self.store.execute("""
            CREATE TABLE IF NOT EXISTS merge_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subtask_id TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                branch TEXT NOT NULL,
                priority INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
            )
        """)

    async def enqueue(
        self,
        subtask_id: str,
        agent_name: str,
        branch: str,
        priority: int = 0,
    ) -> None:
        """Add a subtask to the merge queue.

        Args:
            subtask_id: ID of the completed subtask.
            agent_name: Name of the agent that completed the subtask.
            branch: Branch name to merge from.
            priority: Priority level (higher = merge first). Defaults to 0.
        """
        created_at = datetime.now(UTC).isoformat()
        self.store.execute(
            """
            INSERT INTO merge_queue (subtask_id, agent_name, branch, priority, created_at, status)
            VALUES (?, ?, ?, ?, ?, 'pending')
            """,
            (subtask_id, agent_name, branch, priority, created_at),
        )

    async def process(self) -> list[MergeResult]:
        """Process the merge queue in priority/FIFO order.

        For each queued entry:
        1. Attempt to merge via worktrees.merge()
        2. Record the result (success or conflict)
        3. Remove from queue

        Returns:
            List of MergeResult for all processed entries.
        """
        # Fetch pending entries ordered by priority (desc) then created_at (asc)
        rows = self.store.fetchall(
            """
            SELECT id, subtask_id, agent_name, branch, priority
            FROM merge_queue
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            """,
        )

        results: list[MergeResult] = []

        for row in rows:
            queue_id = row["id"]
            subtask_id = row["subtask_id"]
            agent_name = row["agent_name"]
            branch = row["branch"]

            # Attempt the merge
            # Note: WorktreeManager.merge() takes a worktree name, not branch
            # We derive the worktree name from the agent_name
            success = await self.worktrees.merge(agent_name)

            if success:
                result = MergeResult(
                    subtask_id=subtask_id,
                    branch=branch,
                    success=True,
                    conflict_files=[],
                )
                # Remove from queue on success
                self.store.execute(
                    "DELETE FROM merge_queue WHERE id = ?",
                    (queue_id,),
                )
            else:
                # Merge had conflicts
                # For now, we return empty conflict_files since worktrees.merge()
                # doesn't provide detailed conflict info
                result = MergeResult(
                    subtask_id=subtask_id,
                    branch=branch,
                    success=False,
                    conflict_files=[],
                )
                # Update status to reflect conflict (keep in queue for manual resolution)
                self.store.execute(
                    "UPDATE merge_queue SET status = 'conflict' WHERE id = ?",
                    (queue_id,),
                )

            results.append(result)

        return results

    async def pending(self) -> list[dict]:
        """Return all pending queue entries.

        Returns:
            List of dicts with keys: subtask_id, agent_name, branch, priority, created_at
        """
        rows = self.store.fetchall(
            """
            SELECT subtask_id, agent_name, branch, priority, created_at
            FROM merge_queue
            WHERE status = 'pending'
            ORDER BY priority DESC, created_at ASC
            """,
        )
        return [dict(row) for row in rows]

    async def clear(self) -> None:
        """Clear all pending entries from the queue."""
        self.store.execute("DELETE FROM merge_queue WHERE status = 'pending'")
