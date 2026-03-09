"""Tests for the horse-fish TUI dashboard app."""

from __future__ import annotations

import pytest

from horse_fish.dashboard.app import DashApp
from horse_fish.dashboard.widgets import AgentLog, AgentTable, PipelineBar, SubtaskTable
from horse_fish.store.db import Store


def test_dash_app_instantiation(tmp_path):
    """Test DashApp can be instantiated with a db path."""
    db_path = str(tmp_path / "test.db")
    app = DashApp(db_path=db_path)
    assert app is not None
    assert app._db_path == db_path


async def test_dash_app_renders(tmp_path):
    """Test DashApp compose yields expected widgets."""
    db_path = str(tmp_path / "test.db")
    async with DashApp(db_path=db_path).run_test() as pilot:
        assert pilot.app.query_one(PipelineBar) is not None
        assert pilot.app.query_one(AgentTable) is not None
        assert pilot.app.query_one(SubtaskTable) is not None
        assert pilot.app.query_one(AgentLog) is not None


async def test_poll_updates_pipeline_bar(tmp_path):
    """Test poll updates pipeline bar from SQLite run data."""
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.migrate()
    # Insert a test run
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, datetime('now'))",
        ("test-run-123", "test task", "executing"),
    )
    store.close()

    async with DashApp(db_path=db_path).run_test() as pilot:
        # Wait for the initial poll
        await pilot.pause()
        pipeline_bar = pilot.app.query_one("#pipeline-bar", PipelineBar)
        assert pipeline_bar.run_state == "executing"
        assert pipeline_bar.run_id == "test-run-123"


async def test_poll_updates_agent_table(tmp_path):
    """Test poll updates agent table from SQLite agent data."""
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.migrate()
    # Insert a test agent
    store.execute(
        "INSERT INTO agents (id, name, runtime, state, task_id) VALUES (?, ?, ?, ?, ?)",
        ("agent-1", "hf-agent-1", "claude", "idle", "task-123"),
    )
    store.close()

    async with DashApp(db_path=db_path).run_test() as pilot:
        # Wait for the initial poll
        await pilot.pause()
        agent_table = pilot.app.query_one("#agent-table", AgentTable)
        assert agent_table.row_count == 1
