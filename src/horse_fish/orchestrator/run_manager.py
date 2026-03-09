"""RunManager for managing queued and active runs."""

from __future__ import annotations

import uuid

from horse_fish.store.db import Store


class RunManager:
    """Manages run submission and cancellation for queued runs."""

    def __init__(
        self,
        db_path: str,
        runtime: str = "claude",
        model: str | None = None,
        max_agents: int = 3,
        planner_runtime: str | None = None,
    ) -> None:
        self._db_path = db_path
        self._runtime = runtime
        self._model = model
        self._max_agents = max_agents
        self._planner_runtime = planner_runtime

    async def submit(self, task: str) -> str:
        """Submit a task as a queued run. Returns the run ID."""
        store = Store(self._db_path)
        store.migrate()
        try:
            run_id = str(uuid.uuid4())
            store.insert_queued_run(run_id, task)
            return run_id
        finally:
            store.close()

    async def cancel(self, run_id: str) -> bool:
        """Cancel a queued or running run. Returns True if cancelled."""
        store = Store(self._db_path)
        store.migrate()
        try:
            run = store.fetch_run(run_id)
            if not run:
                return False

            # Can only cancel queued or active runs
            if run["state"] in ("queued", "planning", "executing", "reviewing", "merging"):
                store.update_run_state(run["id"], "cancelled")
                return True
            return False
        finally:
            store.close()
