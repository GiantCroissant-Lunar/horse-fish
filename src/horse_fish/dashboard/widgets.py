"""TUI dashboard widgets for horse-fish."""

from textual.message import Message
from textual.reactive import reactive
from textual.widgets import DataTable, RichLog, Static


class PipelineBar(Static):
    """Shows the orchestrator pipeline phase as a horizontal indicator."""

    run_state: reactive[str] = reactive("idle")
    run_id: reactive[str] = reactive("")

    def render(self) -> str:
        phases = ["planning", "executing", "reviewing", "merging"]
        parts = []
        for phase in phases:
            if phase == self.run_state:
                parts.append(f"[bold green][{phase.upper()}][/]")
            else:
                parts.append(phase)
        pipeline = " → ".join(parts)
        if self.run_state == "queued":
            pipeline = "[bold dim]QUEUED[/]"
        elif self.run_state == "completed":
            pipeline += " → [bold green]COMPLETED[/]"
        elif self.run_state == "failed":
            pipeline += " → [bold red]FAILED[/]"
        elif self.run_state == "cancelled":
            pipeline = "[bold red]CANCELLED[/]"
        run_info = f"  Run: {self.run_id[:12]}" if self.run_id else ""
        return pipeline + run_info


class AgentTable(DataTable):
    """Shows all agent slots from SQLite."""

    class AgentSelected(Message):
        """Message emitted when an agent is selected."""

        def __init__(self, tmux_session: str) -> None:
            self.tmux_session = tmux_session
            super().__init__()

    def on_mount(self) -> None:
        self.add_columns("Name", "Runtime", "State", "Task")

    def update_agents(self, agents: list[dict]) -> None:
        """Refresh table data from SQLite agent rows."""
        self.clear()
        for agent in agents:
            state = agent.get("state", "")
            style = self.agent_state_style(state)
            self.add_row(
                agent.get("name", ""),
                agent.get("runtime", ""),
                f"[{style}]{state}[/]" if style else state,
                agent.get("task_id", "") or "-",
                key=agent.get("id", ""),
            )

    @staticmethod
    def agent_state_style(state: str) -> str:
        """Return Textual style for an agent state."""
        styles = {
            "idle": "green",
            "busy": "yellow",
            "dead": "red",
            "cancelled": "grey",
        }
        return styles.get(state, "")

    def on_data_table_cursor_changed(self, event) -> None:
        """Handle cursor change to emit AgentSelected message."""
        if self.cursor_row is not None and self.cursor_row < self.row_count:
            row_key = self.get_row_at(self.cursor_row)
            if row_key:
                # The tmux_session is stored in the row data
                row_data = self.get_row(row_key)  # type: ignore[arg-type]
                if row_data and len(row_data) > 0:
                    tmux_session = str(row_data[0])  # name column contains tmux session
                    self.post_message(self.AgentSelected(tmux_session))


class SubtaskTable(DataTable):
    """Shows all subtasks from SQLite."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

    def on_mount(self) -> None:
        self.add_columns("Description", "State", "Agent")

    def update_subtasks(self, subtasks: list[dict]) -> None:
        """Refresh table data from SQLite subtask rows."""
        self.clear()
        for st in subtasks:
            state = st.get("state", "")
            style = self.state_style(state)
            desc = st.get("description", "")[:40]
            self.add_row(
                desc,
                f"[{style}]{state}[/]" if style else state,
                st.get("agent_id", "") or "-",
                key=st.get("id", ""),
            )

    @staticmethod
    def state_style(state: str) -> str:
        """Return Textual style for a given state."""
        styles = {
            "done": "green",
            "running": "yellow",
            "pending": "dim",
            "failed": "red",
            "cancelled": "grey",
        }
        return styles.get(state, "")

    def format_state(self, state: str) -> str:
        """Format state with appropriate styling."""
        style = self.state_style(state)
        if style:
            return f"[{style}]{state}[/{style}]"
        return state


class AgentLog(RichLog):
    """Shows live tmux capture output for the selected agent."""

    def update_log(self, output: str) -> None:
        """Update the log with new output."""
        self.clear()
        self.write(output)
