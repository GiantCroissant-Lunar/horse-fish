"""Tests for the horse-fish TUI dashboard app."""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Header

from horse_fish.dashboard.app import DashApp
from horse_fish.dashboard.screens import QueueScreen, RunDetailScreen
from horse_fish.dashboard.widgets import AgentTable, PipelineBar
from horse_fish.store.db import Store


def test_dash_app_instantiation(tmp_path):
    """Test DashApp can be instantiated with a db path."""
    db_path = str(tmp_path / "test.db")
    app = DashApp(db_path=db_path)
    assert app is not None
    assert app._db_path == db_path


def test_dash_app_has_screens_registered():
    """Test DashApp has QueueScreen in SCREENS."""
    assert "queue" in DashApp.SCREENS


class _QueueTestApp(App):
    """Test wrapper that directly mounts QueueScreen."""

    def __init__(self, db_path: str):
        super().__init__()
        self.db_path = db_path
        self.store: Store | None = None

    def compose(self) -> ComposeResult:
        yield Header()

    def on_mount(self) -> None:
        self.store = Store(self.db_path)
        self.store.migrate()
        self.push_screen(QueueScreen())


class _DetailTestApp(App):
    """Test wrapper that directly mounts RunDetailScreen."""

    def __init__(self, db_path: str, run_id: str):
        super().__init__()
        self.db_path = db_path
        self._run_id = run_id
        self.store: Store | None = None

    def compose(self) -> ComposeResult:
        yield Header()

    def on_mount(self) -> None:
        self.store = Store(self.db_path)
        self.store.migrate()
        from horse_fish.agents.tmux import TmuxManager

        self.tmux = TmuxManager()
        self.push_screen(RunDetailScreen(self._run_id))


async def test_queue_screen_renders(tmp_path):
    """Test QueueScreen shows run table."""
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.migrate()
    store.close()

    async with _QueueTestApp(db_path=db_path).run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, QueueScreen)
        run_table = screen.query_one("#run-table")
        assert run_table is not None


async def test_run_detail_screen_pipeline_bar(tmp_path):
    """Test RunDetailScreen updates pipeline bar from SQLite run data."""
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.migrate()
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, datetime('now'))",
        ("test-run-123", "test task", "executing"),
    )
    store.close()

    async with _DetailTestApp(db_path=db_path, run_id="test-run-123").run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, RunDetailScreen)
        pipeline_bar = screen.query_one("#pipeline-bar", PipelineBar)
        assert pipeline_bar.run_state == "executing"
        assert pipeline_bar.run_id == "test-run-123"


async def test_run_detail_screen_agent_table(tmp_path):
    """Test RunDetailScreen updates agent table from SQLite data."""
    db_path = str(tmp_path / "test.db")
    store = Store(db_path)
    store.migrate()
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, datetime('now'))",
        ("test-run-456", "test task", "executing"),
    )
    store.execute(
        "INSERT INTO subtasks (id, run_id, description, state, agent_id, created_at)"
        " VALUES (?, ?, ?, ?, ?, datetime('now'))",
        ("st-1", "test-run-456", "fix bug", "running", "agent-1"),
    )
    store.execute(
        "INSERT INTO agents (id, name, runtime, state, task_id) VALUES (?, ?, ?, ?, ?)",
        ("agent-1", "hf-agent-1", "claude", "busy", "st-1"),
    )
    store.close()

    async with _DetailTestApp(db_path=db_path, run_id="test-run-456").run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, RunDetailScreen)
        agent_table = screen.query_one("#agent-table", AgentTable)
        assert agent_table.row_count == 1
