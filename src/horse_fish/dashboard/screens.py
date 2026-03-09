"""TUI dashboard screens for horse-fish."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from horse_fish.dashboard.widgets import AgentLog, AgentTable, PipelineBar, SubtaskTable


class QueueSummary(Static):
    """Shows counts of runs by state."""

    def update_counts(self, counts: dict[str, int]) -> None:
        """Update the summary with new counts."""
        active = (
            counts.get("planning", 0)
            + counts.get("executing", 0)
            + counts.get("reviewing", 0)
            + counts.get("merging", 0)
        )
        queued = counts.get("queued", 0)
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0)
        cancelled = counts.get("cancelled", 0)
        self.update(
            f"{active} active | {queued} queued | {completed} completed | {failed} failed | {cancelled} cancelled"
        )


class QueueScreen(Screen):
    """Main screen showing all runs in a table."""

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh", "Refresh"),
        ("enter", "view_run", "View Run"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield QueueSummary(id="queue-summary")
        yield DataTable(id="run-table", cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#run-table", DataTable)
        table.add_columns("ID", "Task", "State", "Agents", "Duration")
        self.set_interval(2.0, self._poll)
        self.call_later(self._poll)

    async def _poll(self) -> None:
        store = self.app.store  # type: ignore[attr-defined]
        if not store:
            return

        # Fetch recent runs
        runs = store.fetch_recent_runs(limit=50)

        # Update DataTable
        table = self.query_one("#run-table", DataTable)
        table.clear()

        # Count states for summary
        counts: dict[str, int] = {}

        for run in runs:
            state = run.get("state", "unknown")
            counts[state] = counts.get(state, 0) + 1

            # Format ID (first 8 chars)
            run_id = run.get("id", "")
            short_id = run_id[:8] if run_id else ""

            # Format task (truncate to 30 chars)
            task = run.get("task", "")
            short_task = task[:30] + "..." if len(task) > 30 else task

            # Format state with color
            state_style = self._state_style(state)
            styled_state = f"[{state_style}]{state}[/]" if state_style else state

            # Format agents (placeholder for now - would need to query agents for this run)
            agents = "-"

            # Format duration (relative time since created_at)
            created_at = run.get("created_at", "")
            duration = self._format_duration(created_at) if created_at else "-"

            table.add_row(short_id, short_task, styled_state, agents, duration, key=run_id)

        # Update summary
        summary = self.query_one("#queue-summary", QueueSummary)
        summary.update_counts(counts)

    def _state_style(self, state: str) -> str:
        """Return Textual style for a given state."""
        styles = {
            "completed": "green",
            "executing": "yellow",
            "planning": "yellow",
            "reviewing": "yellow",
            "merging": "yellow",
            "queued": "dim",
            "failed": "red",
            "cancelled": "grey",
        }
        return styles.get(state, "")

    def _format_duration(self, created_at: str) -> str:
        """Format duration since created_at."""
        try:
            from datetime import UTC, datetime

            # Parse ISO format timestamp
            if created_at.endswith("Z"):
                created_at = created_at[:-1] + "+00:00"
            dt = datetime.fromisoformat(created_at)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            now = datetime.now(UTC)
            diff = now - dt
            total_seconds = int(diff.total_seconds())

            if total_seconds < 60:
                return f"{total_seconds}s"
            elif total_seconds < 3600:
                return f"{total_seconds // 60}m"
            elif total_seconds < 86400:
                return f"{total_seconds // 3600}h"
            else:
                return f"{total_seconds // 86400}d"
        except Exception:
            return "-"

    def action_view_run(self) -> None:
        """Push RunDetailScreen for selected run."""
        table = self.query_one("#run-table", DataTable)
        if table.cursor_row is not None and table.cursor_row < table.row_count:
            row_key = table.get_row_at(table.cursor_row)
            if row_key:
                run_id = str(row_key)
                self.app.push_screen(RunDetailScreen(run_id))  # type: ignore[attr-defined]

    def action_refresh(self) -> None:
        self.call_later(self._poll)


class RunDetailScreen(Screen):
    """Detail view for a single run."""

    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("r", "refresh", "Refresh"),
    ]

    def __init__(self, run_id: str) -> None:
        super().__init__()
        self.run_id = run_id

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield PipelineBar(id="pipeline-bar")
        with Static(id="tables"):
            yield AgentTable(id="agent-table")
            yield SubtaskTable(id="subtask-table")
        yield AgentLog(id="agent-log")
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(2.0, self._poll)
        self.call_later(self._poll)

    async def _poll(self) -> None:
        store = self.app.store  # type: ignore[attr-defined]
        if not store:
            return

        # Fetch this specific run
        run = store.fetch_run(self.run_id)
        if not run:
            return

        # Update pipeline bar
        pipeline_bar = self.query_one("#pipeline-bar", PipelineBar)
        pipeline_bar.run_state = run.get("state", "unknown")
        pipeline_bar.run_id = run.get("id", "")

        # Fetch subtasks for this run
        subtasks = store.fetch_subtasks(self.run_id)
        subtask_table = self.query_one("#subtask-table", SubtaskTable)
        subtask_table.update_subtasks(subtasks)

        # Fetch agents for this run's subtasks
        agent_ids = {st.get("agent_id") for st in subtasks if st.get("agent_id")}
        agents = []
        for agent_id in agent_ids:
            agent = store.fetchone("SELECT * FROM agents WHERE id = ?", (agent_id,))
            if agent:
                agents.append(agent)

        agent_table = self.query_one("#agent-table", AgentTable)
        agent_table.update_agents(agents)

    def on_agent_table_agent_selected(self, event: AgentTable.AgentSelected) -> None:
        """Capture tmux pane output for selected agent."""
        # This would need access to tmux manager from app
        pass

    def action_go_back(self) -> None:
        self.app.pop_screen()  # type: ignore[attr-defined]

    def action_refresh(self) -> None:
        self.call_later(self._poll)
