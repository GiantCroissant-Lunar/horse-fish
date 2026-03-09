"""Tests for TUI dashboard widgets and screens."""

from __future__ import annotations

from horse_fish.dashboard.screens import QueueSummary
from horse_fish.dashboard.widgets import AgentTable, PipelineBar, SubtaskTable


def test_pipeline_bar_renders_queued() -> None:
    """PipelineBar should show QUEUED for queued state."""
    bar = PipelineBar()
    bar.run_state = "queued"
    rendered = bar.render()
    assert "QUEUED" in rendered


def test_pipeline_bar_renders_cancelled() -> None:
    """PipelineBar should show CANCELLED for cancelled state."""
    bar = PipelineBar()
    bar.run_state = "cancelled"
    rendered = bar.render()
    assert "CANCELLED" in rendered


def test_pipeline_bar_renders_completed() -> None:
    """PipelineBar should show COMPLETED for completed state."""
    bar = PipelineBar()
    bar.run_state = "completed"
    rendered = bar.render()
    assert "COMPLETED" in rendered


def test_pipeline_bar_renders_failed() -> None:
    """PipelineBar should show FAILED for failed state."""
    bar = PipelineBar()
    bar.run_state = "failed"
    rendered = bar.render()
    assert "FAILED" in rendered


def test_pipeline_bar_renders_executing() -> None:
    """PipelineBar should highlight EXECUTING phase."""
    bar = PipelineBar()
    bar.run_state = "executing"
    rendered = bar.render()
    assert "[EXECUTING]" in rendered


def test_queue_summary_counts() -> None:
    """QueueSummary should format counts correctly."""
    summary = QueueSummary()
    counts = {
        "planning": 1,
        "executing": 1,
        "queued": 2,
        "completed": 5,
        "failed": 1,
        "cancelled": 0,
    }
    summary.update_counts(counts)
    # Should show "2 active | 2 queued | 5 completed | 1 failed | 0 cancelled"
    rendered = str(summary.render())
    assert "2 active" in rendered
    assert "2 queued" in rendered
    assert "5 completed" in rendered
    assert "1 failed" in rendered


def test_agent_table_cancelled_style() -> None:
    """AgentTable should use grey style for cancelled state."""
    style = AgentTable.agent_state_style("cancelled")
    assert style == "grey"


def test_agent_table_idle_style() -> None:
    """AgentTable should use green style for idle state."""
    style = AgentTable.agent_state_style("idle")
    assert style == "green"


def test_agent_table_busy_style() -> None:
    """AgentTable should use yellow style for busy state."""
    style = AgentTable.agent_state_style("busy")
    assert style == "yellow"


def test_subtask_table_cancelled_style() -> None:
    """SubtaskTable should use grey style for cancelled state."""
    style = SubtaskTable.state_style("cancelled")
    assert style == "grey"


def test_subtask_table_done_style() -> None:
    """SubtaskTable should use green style for done state."""
    style = SubtaskTable.state_style("done")
    assert style == "green"


def test_subtask_table_pending_style() -> None:
    """SubtaskTable should use dim style for pending state."""
    style = SubtaskTable.state_style("pending")
    assert style == "dim"


def test_subtask_table_format_state_cancelled() -> None:
    """SubtaskTable.format_state should handle cancelled state."""
    table = SubtaskTable()
    formatted = table.format_state("cancelled")
    assert "grey" in formatted
