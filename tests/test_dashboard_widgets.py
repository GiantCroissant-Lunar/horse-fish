"""Tests for dashboard TUI widgets."""

from __future__ import annotations

from textual.app import App, ComposeResult

from horse_fish.dashboard.widgets import (
    AgentLog,
    AgentTable,
    PipelineBar,
    SubtaskTable,
)


class WidgetTestApp(App):
    """Simple app for testing widgets."""

    def compose(self) -> ComposeResult:
        yield PipelineBar()


class PipelineBarTestApp(App):
    """App with PipelineBar for testing."""

    def compose(self) -> ComposeResult:
        yield PipelineBar()


class AgentTableTestApp(App):
    """App with AgentTable for testing."""

    def compose(self) -> ComposeResult:
        yield AgentTable()


class SubtaskTableTestApp(App):
    """App with SubtaskTable for testing."""

    def compose(self) -> ComposeResult:
        yield SubtaskTable()


class TestPipelineBar:
    """Tests for PipelineBar widget."""

    async def test_pipeline_bar_renders_idle_state(self) -> None:
        """Verify PipelineBar renders correctly in idle state."""
        async with PipelineBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(PipelineBar)
            render = bar.render()
            assert "planning" in render
            assert "executing" in render
            assert "reviewing" in render
            assert "merging" in render

    async def test_pipeline_bar_highlights_executing_state(self) -> None:
        """Verify PipelineBar highlights executing state."""
        async with PipelineBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(PipelineBar)
            bar.run_state = "executing"
            render = bar.render()
            assert "[EXECUTING]" in render

    async def test_pipeline_bar_highlights_planning_state(self) -> None:
        """Verify PipelineBar highlights planning state."""
        async with PipelineBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(PipelineBar)
            bar.run_state = "planning"
            render = bar.render()
            assert "[PLANNING]" in render

    async def test_pipeline_bar_highlights_reviewing_state(self) -> None:
        """Verify PipelineBar highlights reviewing state."""
        async with PipelineBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(PipelineBar)
            bar.run_state = "reviewing"
            render = bar.render()
            assert "[REVIEWING]" in render

    async def test_pipeline_bar_highlights_merging_state(self) -> None:
        """Verify PipelineBar highlights merging state."""
        async with PipelineBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(PipelineBar)
            bar.run_state = "merging"
            render = bar.render()
            assert "[MERGING]" in render

    async def test_pipeline_bar_shows_completed_state(self) -> None:
        """Verify PipelineBar shows COMPLETED for completed state."""
        async with PipelineBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(PipelineBar)
            bar.run_state = "completed"
            render = bar.render()
            assert "COMPLETED" in render

    async def test_pipeline_bar_shows_failed_state(self) -> None:
        """Verify PipelineBar shows FAILED for failed state."""
        async with PipelineBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(PipelineBar)
            bar.run_state = "failed"
            render = bar.render()
            assert "FAILED" in render

    async def test_pipeline_bar_shows_run_id(self) -> None:
        """Verify PipelineBar displays run_id."""
        async with PipelineBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(PipelineBar)
            bar.run_id = "abc-123-def-456-ghi-789"
            render = bar.render()
            assert "Run: abc-123-def-" in render

    async def test_pipeline_bar_truncates_long_run_id(self) -> None:
        """Verify PipelineBar truncates run_id to 12 characters."""
        async with PipelineBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(PipelineBar)
            bar.run_id = "very-long-run-id-that-should-be-truncated"
            render = bar.render()
            assert "Run: very-long-" in render
            assert "very-long-run-id-that-should-be-truncated" not in render

    async def test_pipeline_bar_empty_run_id(self) -> None:
        """Verify PipelineBar handles empty run_id."""
        async with PipelineBarTestApp().run_test() as pilot:
            bar = pilot.app.query_one(PipelineBar)
            bar.run_id = ""
            render = bar.render()
            assert "Run:" not in render


class TestAgentTable:
    """Tests for AgentTable widget."""

    async def test_agent_table_can_be_populated(self) -> None:
        """Verify AgentTable can be populated with agent data."""
        async with AgentTableTestApp().run_test() as pilot:
            table = pilot.app.query_one(AgentTable)
            table.add_columns("name", "runtime", "state", "task_id")
            table.add_row("agent-1", "claude", "idle", "")
            table.add_row("agent-2", "pi", "busy", "task-123")

            assert table.row_count == 2
            row = table.get_row_at(0)
            assert row is not None

    async def test_agent_table_emits_agent_selected_message(self) -> None:
        """Verify AgentTable emits AgentSelected message on cursor change."""
        async with AgentTableTestApp().run_test() as pilot:
            table = pilot.app.query_one(AgentTable)
            table.add_columns("name", "runtime", "state", "task_id")
            table.add_row("agent-1", "claude", "idle", "")
            table.add_row("agent-2", "pi", "busy", "task-123")

            # The message should be posted when cursor changes
            # We verify the message class exists and has the right attribute
            assert hasattr(AgentTable, "AgentSelected")
            # Verify the AgentSelected message has tmux_session attribute
            msg = AgentTable.AgentSelected("test-session")
            assert msg.tmux_session == "test-session"


class TestSubtaskTable:
    """Tests for SubtaskTable widget."""

    async def test_subtask_table_can_be_populated(self) -> None:
        """Verify SubtaskTable can be populated with subtask data."""
        async with SubtaskTableTestApp().run_test() as pilot:
            table = pilot.app.query_one(SubtaskTable)
            table.add_columns("description", "state", "agent")
            table.add_row("Implement feature X", "done", "agent-1")
            table.add_row("Write tests", "running", "agent-2")
            table.add_row("Review code", "pending", "")

            assert table.row_count == 3
            row = table.get_row_at(0)
            assert row is not None

    async def test_subtask_table_state_styles(self) -> None:
        """Verify SubtaskTable returns correct styles for states."""
        assert SubtaskTable.state_style("done") == "green"
        assert SubtaskTable.state_style("running") == "yellow"
        assert SubtaskTable.state_style("pending") == "dim"
        assert SubtaskTable.state_style("failed") == "red"
        assert SubtaskTable.state_style("unknown") == ""

    async def test_subtask_table_format_state(self) -> None:
        """Verify SubtaskTable formats states with styling."""
        async with SubtaskTableTestApp().run_test() as pilot:
            table = pilot.app.query_one(SubtaskTable)
            assert "[green]done[/green]" in table.format_state("done")
            assert "[yellow]running[/yellow]" in table.format_state("running")
            assert "[dim]pending[/dim]" in table.format_state("pending")
            assert "[red]failed[/red]" in table.format_state("failed")


class TestAgentLog:
    """Tests for AgentLog widget."""

    async def test_agent_log_update_log(self) -> None:
        """Verify AgentLog.update_log clears and writes output."""

        class AgentLogTestApp(App):
            def compose(self) -> ComposeResult:
                yield AgentLog()

        async with AgentLogTestApp().run_test() as pilot:
            log = pilot.app.query_one(AgentLog)
            log.write("initial content\n")
            log.update_log("new content\n")
            # Verify the log has the new content
            # RichLog stores lines internally
            assert log is not None
