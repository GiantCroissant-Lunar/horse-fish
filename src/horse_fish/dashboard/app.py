"""Horse-fish TUI dashboard — read-only observer of agent swarm state."""

from __future__ import annotations

from textual.app import App

from horse_fish.agents.tmux import TmuxManager
from horse_fish.dashboard.screens import QueueScreen  # noqa: F401
from horse_fish.store.db import Store


class DashApp(App):
    """Live TUI dashboard for horse-fish agent swarm."""

    CSS = """
    QueueScreen {
        layout: vertical;
    }

    #queue-summary {
        height: 1;
        text-align: center;
        color: $text-muted;
    }

    #run-table {
        height: 1fr;
        border: solid green;
    }

    RunDetailScreen {
        layout: vertical;
    }

    #pipeline-bar {
        height: 3;
        border: solid green;
    }

    #tables {
        height: 1fr;
    }

    #agent-table {
        width: 1fr;
        border: solid blue;
    }

    #subtask-table {
        width: 1fr;
        border: solid cyan;
    }

    #agent-log {
        height: 1fr;
        border: solid yellow;
        min-height: 8;
    }
    """

    def __init__(
        self,
        db_path: str,
        max_concurrent_runs: int = 2,
        runtime: str = "claude",
        model: str | None = None,
        max_agents: int = 3,
    ) -> None:
        super().__init__()
        self._db_path = db_path
        self._max_concurrent_runs = max_concurrent_runs
        self._runtime = runtime
        self._model = model
        self._max_agents = max_agents
        self._store: Store | None = None
        self._tmux = TmuxManager()

    def on_mount(self) -> None:
        self._store = Store(self._db_path)
        self._store.migrate()
        self.push_screen("queue")

    @property
    def store(self) -> Store | None:
        return self._store

    @property
    def tmux(self) -> TmuxManager:
        return self._tmux
