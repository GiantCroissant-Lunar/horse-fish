"""Tests for hf report command and supporting Store methods."""

from pathlib import Path

from click.testing import CliRunner

from horse_fish.cli import main
from horse_fish.store.db import Store


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    store.migrate()
    return store


# --- Store method tests ---


def test_fetch_recent_runs(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.upsert_run("run-1", "first task", "completed", created_at="2026-03-09T00:00:00Z")
    store.upsert_run("run-2", "second task", "failed", created_at="2026-03-09T01:00:00Z")
    store.upsert_run("run-3", "third task", "executing", created_at="2026-03-09T02:00:00Z")

    runs = store.fetch_recent_runs(limit=2)
    assert len(runs) == 2
    assert runs[0]["id"] == "run-3"  # newest first
    assert runs[1]["id"] == "run-2"
    store.close()


def test_fetch_recent_runs_empty(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.fetch_recent_runs() == []
    store.close()


def test_fetch_run_exact(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.upsert_run("run-abc-123", "test task", "completed", created_at="2026-03-09T00:00:00Z")

    run = store.fetch_run("run-abc-123")
    assert run is not None
    assert run["task"] == "test task"
    store.close()


def test_fetch_run_prefix(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.upsert_run("run-abc-123", "test task", "completed", created_at="2026-03-09T00:00:00Z")

    run = store.fetch_run("run-abc")
    assert run is not None
    assert run["id"] == "run-abc-123"
    store.close()


def test_fetch_run_not_found(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.fetch_run("nonexistent") is None
    store.close()


def test_fetch_subtasks(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.upsert_run("run-1", "task", "executing", created_at="2026-03-09T00:00:00Z")
    store.upsert_subtask("sub-1", "run-1", "first", "done", created_at="2026-03-09T00:01:00Z")
    store.upsert_subtask("sub-2", "run-1", "second", "running", created_at="2026-03-09T00:02:00Z")
    store.upsert_subtask("sub-3", "run-1", "third", "pending", created_at="2026-03-09T00:03:00Z")

    subtasks = store.fetch_subtasks("run-1")
    assert len(subtasks) == 3
    assert subtasks[0]["id"] == "sub-1"
    assert subtasks[2]["id"] == "sub-3"
    store.close()


def test_fetch_subtasks_empty(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.fetch_subtasks("nonexistent") == []
    store.close()


def test_fetch_subtasks_with_agent_and_deps(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.upsert_run("run-1", "task", "executing", created_at="2026-03-09T00:00:00Z")
    store.upsert_subtask(
        "sub-1",
        "run-1",
        "complex",
        "running",
        agent_id="agent-1",
        deps='["dep-1"]',
        retry_count=2,
        created_at="2026-03-09T00:01:00Z",
    )

    subtasks = store.fetch_subtasks("run-1")
    assert subtasks[0]["agent_id"] == "agent-1"
    assert subtasks[0]["deps"] == '["dep-1"]'
    assert subtasks[0]["retry_count"] == 2
    store.close()


# --- CLI tests ---


def test_report_recent_no_runs(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setattr("horse_fish.cli.DB_PATH", str(tmp_path / "test.db"))
    runner = CliRunner()
    result = runner.invoke(main, ["report"])
    assert result.exit_code == 0
    assert "No runs found" in result.output


def test_report_recent_shows_runs(tmp_path: Path, monkeypatch: object) -> None:
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("horse_fish.cli.DB_PATH", db)
    store = Store(db)
    store.migrate()
    store.upsert_run("aaaa-1111", "fix the bug", "completed", complexity="SOLO", created_at="2026-03-09T00:00:00Z")
    store.upsert_subtask("sub-1", "aaaa-1111", "do it", "done", created_at="2026-03-09T00:00:00Z")
    store.close()

    runner = CliRunner()
    result = runner.invoke(main, ["report"])
    assert result.exit_code == 0
    assert "aaaa-111" in result.output
    assert "completed" in result.output
    assert "SOLO" in result.output


def test_report_detail(tmp_path: Path, monkeypatch: object) -> None:
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("horse_fish.cli.DB_PATH", db)
    store = Store(db)
    store.migrate()
    store.upsert_run("bbbb-2222", "add feature", "failed", complexity="TRIO", created_at="2026-03-09T00:00:00Z")
    store.upsert_subtask(
        "sub-a",
        "bbbb-2222",
        "implement store",
        "done",
        agent_id="hf-abc",
        created_at="2026-03-09T00:01:00Z",
    )
    store.upsert_subtask(
        "sub-b",
        "bbbb-2222",
        "implement cli",
        "failed",
        retry_count=1,
        created_at="2026-03-09T00:02:00Z",
    )
    store.close()

    runner = CliRunner()
    result = runner.invoke(main, ["report", "bbbb-2222"])
    assert result.exit_code == 0
    assert "bbbb-2222" in result.output
    assert "add feature" in result.output
    assert "TRIO" in result.output
    assert "implement store" in result.output
    assert "implement cli" in result.output
    assert "hf-abc" in result.output


def test_report_detail_prefix(tmp_path: Path, monkeypatch: object) -> None:
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("horse_fish.cli.DB_PATH", db)
    store = Store(db)
    store.migrate()
    store.upsert_run("cccc-3333-long-id", "task", "completed", created_at="2026-03-09T00:00:00Z")
    store.close()

    runner = CliRunner()
    result = runner.invoke(main, ["report", "cccc"])
    assert result.exit_code == 0
    assert "cccc-3333-long-id" in result.output


def test_report_not_found(tmp_path: Path, monkeypatch: object) -> None:
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("horse_fish.cli.DB_PATH", db)
    store = Store(db)
    store.migrate()
    store.close()

    runner = CliRunner()
    result = runner.invoke(main, ["report", "nonexistent"])
    assert result.exit_code == 0
    assert "not found" in result.output


def test_report_json_recent(tmp_path: Path, monkeypatch: object) -> None:
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("horse_fish.cli.DB_PATH", db)
    store = Store(db)
    store.migrate()
    store.upsert_run("dddd-4444", "json test", "completed", created_at="2026-03-09T00:00:00Z")
    store.close()

    runner = CliRunner()
    result = runner.invoke(main, ["report", "--json-output"])
    assert result.exit_code == 0
    import json

    data = json.loads(result.output)
    assert isinstance(data, list)
    assert data[0]["id"] == "dddd-4444"


def test_report_json_detail(tmp_path: Path, monkeypatch: object) -> None:
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("horse_fish.cli.DB_PATH", db)
    store = Store(db)
    store.migrate()
    store.upsert_run("eeee-5555", "json detail", "completed", created_at="2026-03-09T00:00:00Z")
    store.upsert_subtask("sub-x", "eeee-5555", "do stuff", "done", created_at="2026-03-09T00:00:00Z")
    store.close()

    runner = CliRunner()
    result = runner.invoke(main, ["report", "eeee-5555", "--json-output"])
    assert result.exit_code == 0
    import json

    data = json.loads(result.output)
    assert "run" in data
    assert "subtasks" in data
    assert data["run"]["id"] == "eeee-5555"
    assert len(data["subtasks"]) == 1
