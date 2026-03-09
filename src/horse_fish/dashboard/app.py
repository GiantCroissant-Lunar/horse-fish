"""Horse-fish TUI dashboard — read-only observer of agent swarm state."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import Footer, Header

from horse_fish.agents.tmux import TmuxManager
from horse_fish.dashboard.widgets import AgentLog, AgentTable, PipelineBar, SubtaskTable
from horse_fish.store.db import Store

POLL_INTERVAL = 2.0


class DashApp(App):
    """Live TUI dashboard for horse-fish agent swarm."""

    CSS = """
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

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, db_path: str) -> None:
        super().__init__()
        self._db_path = db_path
        self._store: Store | None = None
        self._tmux = TmuxManager()
        self._selected_tmux_session: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield PipelineBar(id="pipeline-bar")
        with Horizontal(id="tables"):
            yield AgentTable(id="agent-table")
            yield SubtaskTable(id="subtask-table")
        yield AgentLog(id="agent-log")
        yield Footer()

    def on_mount(self) -> None:
        self._store = Store(self._db_path)
        self._store.migrate()
        self.set_interval(POLL_INTERVAL, self._poll)
        # Do an immediate first poll
        self.call_later(self._poll)

    async def _poll(self) -> None:
        if not self._store:
            return

        # Update pipeline bar with latest run
        runs = self._store.fetchall("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1")
        pipeline_bar = self.query_one("#pipeline-bar", PipelineBar)
        if runs:
            run = runs[0]
            pipeline_bar.run_state = run["state"]
            pipeline_bar.run_id = run["id"]
        else:
            pipeline_bar.run_state = "idle"
            pipeline_bar.run_id = ""

        # Update agent table
        agents = self._store.fetchall("SELECT * FROM agents")
        agent_table = self.query_one("#agent-table", AgentTable)
        agent_table.update_agents(agents)

        # Update subtask table
        subtasks = self._store.fetchall("SELECT * FROM subtasks ORDER BY created_at")
        subtask_table = self.query_one("#subtask-table", SubtaskTable)
        subtask_table.update_subtasks(subtasks)

        # Update agent log if an agent is selected
        if self._selected_tmux_session:
            try:
                output = await self._tmux.capture_pane(self._selected_tmux_session)
                agent_log = self.query_one("#agent-log", AgentLog)
                agent_log.update_log(output or "(no output)")
            except Exception:
                pass

    def on_agent_table_agent_selected(self, event: AgentTable.AgentSelected) -> None:
        self._selected_tmux_session = event.tmux_session

    def action_refresh(self) -> None:
        self.call_later(self._poll)
