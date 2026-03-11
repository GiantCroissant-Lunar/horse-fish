"""Tests for CLI commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from horse_fish.cli import main
from horse_fish.models import Subtask, SubtaskState, Task, TaskState


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def mock_run():
    run = Task.create("test task")
    run.state = TaskState.completed
    run.subtasks = [
        Subtask(id="1", description="Subtask 1", state=SubtaskState.done),
        Subtask(id="2", description="Subtask 2", state=SubtaskState.done),
    ]
    return run


def test_version(runner):
    """Test that --version prints version."""
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


@patch("horse_fish.cli._init_components")
def test_run_command(mock_init_components, runner, mock_run):
    """Test 'hf run' invokes orchestrator and prints result."""
    mock_orchestrator = MagicMock()
    mock_orchestrator.run = AsyncMock(return_value=mock_run)
    mock_store = MagicMock()
    mock_store.close = MagicMock()
    mock_pool = MagicMock()
    mock_init_components.return_value = (mock_orchestrator, mock_store, mock_pool)

    result = runner.invoke(main, ["run", "test task", "--foreground"])

    assert result.exit_code == 0
    assert f"Run {mock_run.id}: completed" in result.output
    assert "[done] Subtask 1" in result.output
    assert "[done] Subtask 2" in result.output
    mock_orchestrator.run.assert_called_once_with("test task")
    mock_store.close.assert_called_once()


@patch("horse_fish.cli.Store")
def test_status_no_agents(mock_store_class, runner):
    """Test 'hf status' with no agents."""
    mock_store = MagicMock()
    mock_store.fetchall.return_value = []
    mock_store_class.return_value = mock_store

    result = runner.invoke(main, ["status"])

    assert result.exit_code == 0
    assert "No active agents" in result.output
    mock_store.migrate.assert_called_once()
    mock_store.close.assert_called_once()


@patch("horse_fish.cli.Store")
def test_status_with_agents(mock_store_class, runner):
    """Test 'hf status' with agents prints table."""
    mock_store = MagicMock()
    mock_store.fetchall.return_value = [
        {"id": "1", "name": "agent-1", "runtime": "claude", "state": "idle", "task_id": "task-1"},
        {"id": "2", "name": "agent-2", "runtime": "pi", "state": "busy", "task_id": None},
    ]
    mock_store_class.return_value = mock_store

    result = runner.invoke(main, ["status"])

    assert result.exit_code == 0
    assert "Name" in result.output
    assert "Runtime" in result.output
    assert "State" in result.output
    assert "agent-1" in result.output
    assert "agent-2" in result.output
    assert "claude" in result.output
    assert "task-1" in result.output
    mock_store.migrate.assert_called_once()
    mock_store.close.assert_called_once()


@patch("horse_fish.cli.AgentPool")
@patch("horse_fish.cli.TmuxManager")
@patch("horse_fish.cli.WorktreeManager")
@patch("horse_fish.cli.Store")
@patch("pathlib.Path.cwd")
def test_clean_command(mock_cwd, mock_store_class, _mock_worktrees, _mock_tmux, mock_pool_class, runner):
    """Test 'hf clean' calls pool.cleanup() and prints count."""
    mock_cwd.return_value = "/repo"
    mock_store = MagicMock()
    mock_store_class.return_value = mock_store
    mock_pool = MagicMock()
    mock_pool.cleanup = AsyncMock(return_value=3)
    mock_pool_class.return_value = mock_pool

    result = runner.invoke(main, ["clean"])

    assert result.exit_code == 0
    assert "Released 3 agents" in result.output
    mock_pool.cleanup.assert_called_once()
    mock_store.migrate.assert_called_once()
    mock_store.close.assert_called_once()


@patch("horse_fish.cli._init_components")
def test_run_with_options(mock_init_components, runner, mock_run):
    """Test 'hf run' with custom options."""
    mock_orchestrator = MagicMock()
    mock_orchestrator.run = AsyncMock(return_value=mock_run)
    mock_store = MagicMock()
    mock_store.close = MagicMock()
    mock_pool = MagicMock()
    mock_init_components.return_value = (mock_orchestrator, mock_store, mock_pool)

    result = runner.invoke(
        main,
        ["run", "custom task", "--runtime", "pi", "--model", "custom-model", "--max-agents", "5", "--foreground"],
    )

    assert result.exit_code == 0
    mock_init_components.assert_called_once_with("pi", "custom-model", 5, None)
    mock_orchestrator.run.assert_called_once_with("custom task")


@patch("horse_fish.cli.Orchestrator")
@patch("horse_fish.cli.Planner")
@patch("horse_fish.cli.Tracer")
@patch("horse_fish.cli.AgentPool")
@patch("horse_fish.cli.WorktreeManager")
@patch("horse_fish.cli.TmuxManager")
@patch("horse_fish.cli.Store")
@patch("pathlib.Path.cwd")
def test_init_components_wires_tracer(
    mock_cwd,
    mock_store_class,
    _mock_tmux_class,
    _mock_worktrees_class,
    _mock_pool_class,
    mock_tracer_class,
    mock_planner_class,
    mock_orchestrator_class,
):
    """_init_components should create one tracer and pass it to planner and orchestrator."""
    mock_cwd.return_value = MagicMock()
    mock_store = MagicMock()
    mock_store_class.return_value = mock_store
    mock_tracer = MagicMock()
    mock_tracer_class.return_value = mock_tracer
    mock_planner = MagicMock()
    mock_planner.model = "claude-sonnet-4-6"
    mock_planner_class.return_value = mock_planner

    from horse_fish.cli import _init_components

    _init_components("claude", None, 3)

    mock_tracer_class.assert_called_once_with()
    mock_planner_class.assert_called_once_with(runtime="claude", model=None, tracer=mock_tracer)
    assert mock_orchestrator_class.call_args.kwargs["tracer"] is mock_tracer


@patch("horse_fish.cli.MergeQueue")
@patch("horse_fish.cli.WorktreeManager")
@patch("horse_fish.cli.Store")
def test_merge_command_processes_queue(mock_store_class, mock_worktrees_class, mock_merge_queue_class, runner):
    """Test 'hf merge' processes the merge queue and displays results."""
    mock_store = MagicMock()
    mock_store_class.return_value = mock_store

    mock_worktrees = MagicMock()
    mock_worktrees_class.return_value = mock_worktrees

    mock_merge_queue = MagicMock()
    mock_merge_queue.process = AsyncMock(
        return_value=[
            MagicMock(subtask_id="task-1", branch="horse-fish/agent-1", success=True, conflict_files=[]),
            MagicMock(subtask_id="task-2", branch="horse-fish/agent-2", success=False, conflict_files=[]),
        ]
    )
    mock_merge_queue_class.return_value = mock_merge_queue

    result = runner.invoke(main, ["merge"])

    assert result.exit_code == 0
    assert "Merge results:" in result.output
    assert "✓ merged" in result.output
    assert "task-1" in result.output
    assert "✗ conflict" in result.output
    assert "task-2" in result.output
    mock_store.migrate.assert_called_once()
    mock_store.close.assert_called_once()
    mock_merge_queue.process.assert_called_once()


@patch("horse_fish.cli.MergeQueue")
@patch("horse_fish.cli.WorktreeManager")
@patch("horse_fish.cli.Store")
def test_merge_dry_run_shows_pending(mock_store_class, mock_worktrees_class, mock_merge_queue_class, runner):
    """Test 'hf merge --dry-run' shows pending merges without processing."""
    mock_store = MagicMock()
    mock_store_class.return_value = mock_store

    mock_worktrees = MagicMock()
    mock_worktrees_class.return_value = mock_worktrees

    mock_merge_queue = MagicMock()
    mock_merge_queue.pending = AsyncMock(
        return_value=[
            {
                "subtask_id": "task-1",
                "agent_name": "agent-1",
                "branch": "horse-fish/agent-1",
                "priority": 0,
                "created_at": "2026-03-08T10:00:00Z",
            },
            {
                "subtask_id": "task-2",
                "agent_name": "agent-2",
                "branch": "horse-fish/agent-2",
                "priority": 1,
                "created_at": "2026-03-08T10:01:00Z",
            },
        ]
    )
    mock_merge_queue_class.return_value = mock_merge_queue

    result = runner.invoke(main, ["merge", "--dry-run"])

    assert result.exit_code == 0
    assert "Subtask" in result.output
    assert "Agent" in result.output
    assert "Branch" in result.output
    assert "Priority" in result.output
    assert "task-1" in result.output
    assert "task-2" in result.output
    assert "agent-1" in result.output
    assert "agent-2" in result.output
    mock_store.migrate.assert_called_once()
    mock_store.close.assert_called_once()
    mock_merge_queue.pending.assert_called_once()
    mock_merge_queue.process.assert_not_called()


@patch("horse_fish.cli.MergeQueue")
@patch("horse_fish.cli.WorktreeManager")
@patch("horse_fish.cli.Store")
def test_merge_no_pending_shows_message(mock_store_class, mock_worktrees_class, mock_merge_queue_class, runner):
    """Test 'hf merge' shows message when no pending merges."""
    mock_store = MagicMock()
    mock_store_class.return_value = mock_store

    mock_worktrees = MagicMock()
    mock_worktrees_class.return_value = mock_worktrees

    mock_merge_queue = MagicMock()
    mock_merge_queue.process = AsyncMock(return_value=[])
    mock_merge_queue_class.return_value = mock_merge_queue

    result = runner.invoke(main, ["merge"])

    assert result.exit_code == 0
    assert "No pending merges to process" in result.output
    mock_store.migrate.assert_called_once()
    mock_store.close.assert_called_once()


@patch("horse_fish.cli.MergeQueue")
@patch("horse_fish.cli.WorktreeManager")
@patch("horse_fish.cli.Store")
def test_merge_dry_run_no_pending_shows_message(mock_store_class, mock_worktrees_class, mock_merge_queue_class, runner):
    """Test 'hf merge --dry-run' shows message when no pending merges."""
    mock_store = MagicMock()
    mock_store_class.return_value = mock_store

    mock_worktrees = MagicMock()
    mock_worktrees_class.return_value = mock_worktrees

    mock_merge_queue = MagicMock()
    mock_merge_queue.pending = AsyncMock(return_value=[])
    mock_merge_queue_class.return_value = mock_merge_queue

    result = runner.invoke(main, ["merge", "--dry-run"])

    assert result.exit_code == 0
    assert "No pending merges in queue" in result.output
    mock_store.migrate.assert_called_once()
    mock_store.close.assert_called_once()


@patch("horse_fish.cli.TmuxManager")
def test_logs_lists_all_sessions(mock_tmux_class, runner):
    """Test 'hf logs' lists only hf- sessions."""
    mock_tmux = MagicMock()
    mock_tmux.list_sessions = AsyncMock(return_value=["hf-agent-1", "hf-agent-2", "unrelated"])
    mock_tmux.capture_pane = AsyncMock(return_value="some output")
    mock_tmux_class.return_value = mock_tmux

    result = runner.invoke(main, ["logs"])

    assert result.exit_code == 0
    assert "--- hf-agent-1 ---" in result.output
    assert "--- hf-agent-2 ---" in result.output
    assert "unrelated" not in result.output
    mock_tmux.list_sessions.assert_called_once()


@patch("horse_fish.cli.TmuxManager")
def test_logs_single_agent(mock_tmux_class, runner):
    """Test 'hf logs --agent' shows output for single agent."""
    mock_tmux = MagicMock()
    mock_tmux.capture_pane = AsyncMock(return_value="line1\nline2\nline3")
    mock_tmux_class.return_value = mock_tmux

    result = runner.invoke(main, ["logs", "--agent", "hf-agent-1"])

    assert result.exit_code == 0
    assert "--- hf-agent-1 ---" in result.output
    assert "line1" in result.output
    mock_tmux.capture_pane.assert_called_once_with("hf-agent-1")


@patch("horse_fish.cli.TmuxManager")
def test_logs_agent_not_found(mock_tmux_class, runner):
    """Test 'hf logs --agent' shows message when agent not found."""
    mock_tmux = MagicMock()
    mock_tmux.capture_pane = AsyncMock(return_value=None)
    mock_tmux_class.return_value = mock_tmux

    result = runner.invoke(main, ["logs", "--agent", "hf-agent-1"])

    assert result.exit_code == 0
    assert "not found" in result.output.lower() or "no output" in result.output.lower()
    mock_tmux.capture_pane.assert_called_once_with("hf-agent-1")


@patch("horse_fish.cli.TmuxManager")
def test_logs_no_sessions(mock_tmux_class, runner):
    """Test 'hf logs' shows message when no hf- sessions exist."""
    mock_tmux = MagicMock()
    mock_tmux.list_sessions = AsyncMock(return_value=["unrelated"])
    mock_tmux_class.return_value = mock_tmux

    result = runner.invoke(main, ["logs"])

    assert result.exit_code == 0
    assert "No active" in result.output
    assert "horse-fish agents" in result.output
    mock_tmux.list_sessions.assert_called_once()


class TestAgentsCommand:
    """Tests for hf agents command."""

    @patch("horse_fish.cli.Store")
    def test_agents_no_agents(self, mock_store_class, runner):
        """Shows message when no agents found."""
        mock_store = MagicMock()
        mock_store.fetchall.return_value = []
        mock_store_class.return_value = mock_store

        result = runner.invoke(main, ["agents"])
        assert result.exit_code == 0
        assert "No agents found" in result.output

    @patch("horse_fish.cli.Store")
    def test_agents_table_output(self, mock_store_class, runner):
        """Prints table with agent info."""
        mock_store = MagicMock()
        mock_store.fetchall.return_value = [
            {
                "name": "hf-abc123",
                "runtime": "kimi",
                "model": "kimi-for-coding",
                "state": "busy",
                "task_id": "task-1",
                "started_at": "2026-03-09T10:00:00+00:00",
            },
        ]
        mock_store_class.return_value = mock_store

        result = runner.invoke(main, ["agents"])
        assert result.exit_code == 0
        assert "hf-abc123" in result.output
        assert "kimi" in result.output
        assert "busy" in result.output
        assert "task-1" in result.output

    @patch("horse_fish.cli.Store")
    def test_agents_state_filter(self, mock_store_class, runner):
        """--state flag filters by agent state."""
        mock_store = MagicMock()
        mock_store.fetchall.return_value = []
        mock_store_class.return_value = mock_store

        result = runner.invoke(main, ["agents", "--state", "idle"])
        assert result.exit_code == 0
        # Verify the query included the state filter
        call_args = mock_store.fetchall.call_args
        assert "WHERE state = ?" in call_args[0][0]
        assert call_args[0][1] == ("idle",)

    @patch("horse_fish.cli.Store")
    def test_agents_json_output(self, mock_store_class, runner):
        """--json flag outputs JSON."""
        mock_store = MagicMock()
        mock_store.fetchall.return_value = [
            {
                "name": "hf-xyz",
                "runtime": "claude",
                "model": "claude-sonnet-4-6",
                "state": "dead",
                "task_id": None,
                "started_at": "2026-03-09T10:00:00+00:00",
            },
        ]
        mock_store_class.return_value = mock_store

        result = runner.invoke(main, ["agents", "--json"])
        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert len(data) == 1
        assert data[0]["name"] == "hf-xyz"
        assert data[0]["runtime"] == "claude"
        assert data[0]["state"] == "dead"
        assert data[0]["task_id"] == "-"

    @patch("horse_fish.cli.Store")
    def test_agents_duration_format(self, mock_store_class, runner):
        """Duration is formatted as human-readable."""
        mock_store = MagicMock()
        mock_store.fetchall.return_value = [
            {
                "name": "hf-old",
                "runtime": "pi",
                "model": "qwen3.5-plus",
                "state": "idle",
                "task_id": None,
                "started_at": None,
            },
        ]
        mock_store_class.return_value = mock_store

        result = runner.invoke(main, ["agents"])
        assert result.exit_code == 0
        assert "N/A" in result.output

    @patch("horse_fish.cli.Store")
    def test_agents_invalid_state(self, mock_store_class, runner):
        """Invalid --state value is rejected by Click."""
        mock_store = MagicMock()
        mock_store_class.return_value = mock_store

        result = runner.invoke(main, ["agents", "--state", "invalid"])
        assert result.exit_code != 0


class TestDashRecord:
    """Tests for hf dash --record flag."""

    def test_dash_record_no_asciinema(self, runner):
        """--record shows error when asciinema is not installed."""
        with patch("shutil.which", return_value=None):
            result = runner.invoke(main, ["dash", "--record"])
            assert "asciinema not found" in result.output

    @patch("os.execvp")
    @patch("shutil.which", return_value="/usr/bin/asciinema")
    def test_dash_record_calls_execvp(self, _mock_which, mock_execvp, runner, tmp_path):
        """--record calls os.execvp with asciinema."""
        with patch("pathlib.Path.mkdir"):
            runner.invoke(main, ["dash", "--record"])
            mock_execvp.assert_called_once()
            args = mock_execvp.call_args
            assert args[0][0] == "asciinema"
            assert "rec" in args[0][1]
            assert "--command" in args[0][1]
            assert "hf dash" in args[0][1]

    def test_dash_without_record(self, runner):
        """dash without --record launches DashApp normally."""
        with patch("horse_fish.dashboard.app.DashApp") as mock_app_class:
            mock_app = MagicMock()
            mock_app_class.return_value = mock_app
            runner.invoke(main, ["dash"])
            mock_app.run.assert_called_once()


class TestEnvCheck:
    """Tests for hf env-check command."""

    def test_env_check_all_keys_present(self, runner, monkeypatch):
        """Shows OK when all keys are set."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
        monkeypatch.setenv("INCEPTION_API_KEY", "test-key")
        monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
        result = runner.invoke(main, ["env-check"])
        assert result.exit_code == 0
        assert "DASHSCOPE_API_KEY" in result.output
        assert "LANGFUSE_HOST" in result.output
        assert "✓" in result.output

    def test_env_check_missing_keys(self, runner, monkeypatch):
        """Shows MISSING for unset keys."""
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.delenv("INCEPTION_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        result = runner.invoke(main, ["env-check"])
        assert result.exit_code == 0
        assert "MISSING" in result.output

    def test_env_check_dotenv_loaded(self, runner, monkeypatch, tmp_path):
        """Keys from .env file are loaded."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DASHSCOPE_API_KEY=from-dotenv\n")  # pragma: allowlist secret
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        # Manually load dotenv since module-level load_dotenv() already ran
        from dotenv import load_dotenv

        load_dotenv(tmp_path / ".env", override=True)

        result = runner.invoke(main, ["env-check"])
        assert result.exit_code == 0
        # Key should show as masked (first 4 chars + "...")
        assert "from..." in result.output


class TestRunForeground:
    """Tests for hf run --foreground flag."""

    @patch("horse_fish.cli._init_components")
    def test_run_foreground_flag(self, mock_init_components, runner, mock_run):
        """--foreground triggers blocking orchestrator run."""
        mock_orchestrator = MagicMock()
        mock_orchestrator.run = AsyncMock(return_value=mock_run)
        mock_store = MagicMock()
        mock_store.close = MagicMock()
        mock_pool = MagicMock()
        mock_init_components.return_value = (mock_orchestrator, mock_store, mock_pool)

        result = runner.invoke(main, ["run", "test task", "--foreground"])

        assert result.exit_code == 0
        assert f"Run {mock_run.id}: completed" in result.output
        mock_init_components.assert_called_once()
        mock_orchestrator.run.assert_called_once_with("test task")

    @patch("horse_fish.orchestrator.run_manager.RunManager")
    def test_run_default_queues(self, mock_run_manager_class, runner):
        """Default hf run (no --foreground) queues the run."""
        mock_manager = MagicMock()
        mock_manager.submit = AsyncMock(return_value="12345678-1234-1234-1234-123456789012")
        mock_run_manager_class.return_value = mock_manager

        result = runner.invoke(main, ["run", "test task"])

        assert result.exit_code == 0
        assert "Queued run 12345678" in result.output
        mock_run_manager_class.assert_called_once()
        mock_manager.submit.assert_called_once_with("test task")


class TestQueueCommand:
    """Tests for hf queue command."""

    @patch("horse_fish.cli.Store")
    def test_queue_command_shows_runs(self, mock_store_class, runner):
        """hf queue shows active, queued, and recent runs."""
        mock_store = MagicMock()
        mock_store.fetch_queued_runs.return_value = [
            {
                "id": "q1",
                "task": "queued task",
                "state": "queued",
                "created_at": "2026-03-09T10:00:00Z",
                "completed_at": None,
            }
        ]
        mock_store.fetch_active_runs.return_value = [
            {
                "id": "a1",
                "task": "active task",
                "state": "executing",
                "created_at": "2026-03-09T09:00:00Z",
                "completed_at": None,
            }
        ]
        mock_store.fetchall.return_value = [
            {
                "id": "c1",
                "task": "completed task",
                "state": "completed",
                "created_at": "2026-03-09T08:00:00Z",
                "completed_at": "2026-03-09T08:30:00Z",
            }
        ]
        mock_store_class.return_value = mock_store

        result = runner.invoke(main, ["queue"])

        assert result.exit_code == 0
        assert "ID" in result.output
        assert "State" in result.output
        assert "Task" in result.output
        assert "queued task" in result.output
        assert "active task" in result.output
        assert "completed task" in result.output
        assert "1 active | 1 queued" in result.output

    @patch("horse_fish.cli.Store")
    def test_queue_command_no_runs(self, mock_store_class, runner):
        """hf queue shows message when no runs found."""
        mock_store = MagicMock()
        mock_store.fetch_queued_runs.return_value = []
        mock_store.fetch_active_runs.return_value = []
        mock_store.fetchall.return_value = []
        mock_store_class.return_value = mock_store

        result = runner.invoke(main, ["queue"])

        assert result.exit_code == 0
        assert "No runs found" in result.output

    @patch("horse_fish.cli.Store")
    def test_queue_command_json_output(self, mock_store_class, runner):
        """hf queue --json-output outputs JSON."""
        mock_store = MagicMock()
        mock_store.fetch_queued_runs.return_value = []
        mock_store.fetch_active_runs.return_value = []
        mock_store.fetchall.return_value = []
        mock_store_class.return_value = mock_store

        result = runner.invoke(main, ["queue", "--json-output"])

        assert result.exit_code == 0
        import json

        data = json.loads(result.output)
        assert data == []


class TestCancelCommand:
    """Tests for hf cancel command."""

    @patch("horse_fish.orchestrator.run_manager.RunManager")
    def test_cancel_command(self, mock_run_manager_class, runner):
        """hf cancel cancels a queued run."""
        mock_manager = MagicMock()
        mock_manager.cancel = AsyncMock(return_value=True)
        mock_run_manager_class.return_value = mock_manager

        result = runner.invoke(main, ["cancel", "12345678-1234-1234-1234-123456789012"])

        assert result.exit_code == 0
        assert "Cancelled run 12345678" in result.output
        mock_run_manager_class.assert_called_once()
        mock_manager.cancel.assert_called_once_with("12345678-1234-1234-1234-123456789012")

    @patch("horse_fish.orchestrator.run_manager.RunManager")
    def test_cancel_nonexistent(self, mock_run_manager_class, runner):
        """hf cancel shows error for nonexistent run."""
        mock_manager = MagicMock()
        mock_manager.cancel = AsyncMock(return_value=False)
        mock_run_manager_class.return_value = mock_manager

        result = runner.invoke(main, ["cancel", "nonexistent-id"])

        assert result.exit_code == 0
        assert "not found or already in terminal state" in result.output


class TestDashWithRunManager:
    """Tests for hf dash with RunManager integration."""

    @patch("horse_fish.dashboard.app.DashApp")
    def test_dash_with_options(self, mock_app_class, runner):
        """dash with options passes them to DashApp."""
        mock_app = MagicMock()
        mock_app_class.return_value = mock_app

        result = runner.invoke(
            main,
            ["dash", "--max-concurrent", "5", "--runtime", "pi", "--model", "custom-model", "--max-agents", "10"],
        )

        assert result.exit_code == 0
        mock_app_class.assert_called_once_with(
            db_path=".horse-fish/state.db",
            max_concurrent_runs=5,
            runtime="pi",
            model="custom-model",
            max_agents=10,
        )
        mock_app.run.assert_called_once()
