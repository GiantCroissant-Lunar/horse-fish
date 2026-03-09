"""Tests for hf stats command and fetch_run_stats Store method."""

import json
from pathlib import Path

from click.testing import CliRunner

from horse_fish.cli import main
from horse_fish.store.db import Store


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    store.migrate()
    return store


def _seed_agents(store: Store) -> None:
    """Insert agent rows so runtime join works."""
    store.execute(
        "INSERT INTO agents (id, name, runtime, state) VALUES (?, ?, ?, ?)",
        ("agent-1", "hf-a", "pi", "idle"),
    )
    store.execute(
        "INSERT INTO agents (id, name, runtime, state) VALUES (?, ?, ?, ?)",
        ("agent-2", "hf-b", "claude", "idle"),
    )


# --- Store method tests ---


def test_fetch_run_stats_empty(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    stats = store.fetch_run_stats()
    assert stats["total_runs"] == 0
    assert stats["by_state"] == {}
    assert stats["avg_duration_secs"] is None
    assert stats["runtimes"] == []
    store.close()


def test_fetch_run_stats_counts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.upsert_run("r1", "task1", "completed", created_at="2026-03-09T00:00:00Z", completed_at="2026-03-09T00:05:00Z")
    store.upsert_run("r2", "task2", "completed", created_at="2026-03-09T01:00:00Z", completed_at="2026-03-09T01:10:00Z")
    store.upsert_run("r3", "task3", "failed", created_at="2026-03-09T02:00:00Z")

    stats = store.fetch_run_stats()
    assert stats["total_runs"] == 3
    assert stats["by_state"]["completed"] == 2
    assert stats["by_state"]["failed"] == 1
    store.close()


def test_fetch_run_stats_avg_duration(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    # 5 min and 10 min → avg 7.5 min = 450 sec
    store.upsert_run("r1", "t1", "completed", created_at="2026-03-09T00:00:00Z", completed_at="2026-03-09T00:05:00Z")
    store.upsert_run("r2", "t2", "completed", created_at="2026-03-09T01:00:00Z", completed_at="2026-03-09T01:10:00Z")

    stats = store.fetch_run_stats()
    assert stats["avg_duration_secs"] is not None
    assert abs(stats["avg_duration_secs"] - 450.0) < 1.0
    store.close()


def test_fetch_run_stats_runtimes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    _seed_agents(store)
    store.upsert_run("r1", "task", "completed", created_at="2026-03-09T00:00:00Z")
    store.upsert_subtask("s1", "r1", "sub1", "done", agent_id="agent-1", created_at="2026-03-09T00:00:00Z")
    store.upsert_subtask("s2", "r1", "sub2", "done", agent_id="agent-1", created_at="2026-03-09T00:01:00Z")
    store.upsert_subtask("s3", "r1", "sub3", "done", agent_id="agent-2", created_at="2026-03-09T00:02:00Z")

    stats = store.fetch_run_stats()
    assert len(stats["runtimes"]) == 2
    # pi has 2 subtasks, claude has 1 → pi first
    assert stats["runtimes"][0]["runtime"] == "pi"
    assert stats["runtimes"][0]["count"] == 2
    assert stats["runtimes"][1]["runtime"] == "claude"
    assert stats["runtimes"][1]["count"] == 1
    store.close()


# --- CLI tests ---


def test_stats_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("horse_fish.cli.DB_PATH", str(tmp_path / "test.db"))
    runner = CliRunner()
    result = runner.invoke(main, ["stats"])
    assert result.exit_code == 0
    assert "Total runs: 0" in result.output
    assert "N/A" in result.output


def test_stats_with_data(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("horse_fish.cli.DB_PATH", db)
    store = Store(db)
    store.migrate()
    store.upsert_run("r1", "t1", "completed", created_at="2026-03-09T00:00:00Z", completed_at="2026-03-09T00:05:00Z")
    store.upsert_run("r2", "t2", "failed", created_at="2026-03-09T01:00:00Z")
    store.close()

    runner = CliRunner()
    result = runner.invoke(main, ["stats"])
    assert result.exit_code == 0
    assert "Total runs: 2" in result.output
    assert "completed: 1" in result.output
    assert "failed: 1" in result.output


def test_stats_json(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("horse_fish.cli.DB_PATH", db)
    store = Store(db)
    store.migrate()
    store.upsert_run("r1", "t1", "completed", created_at="2026-03-09T00:00:00Z", completed_at="2026-03-09T00:10:00Z")
    store.close()

    runner = CliRunner()
    result = runner.invoke(main, ["stats", "--json-output"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["total_runs"] == 1
    assert data["by_state"]["completed"] == 1
    assert data["avg_duration_secs"] is not None


def test_stats_shows_runtimes(tmp_path: Path, monkeypatch) -> None:
    db = str(tmp_path / "test.db")
    monkeypatch.setattr("horse_fish.cli.DB_PATH", db)
    store = Store(db)
    store.migrate()
    store.execute(
        "INSERT INTO agents (id, name, runtime, state) VALUES (?, ?, ?, ?)",
        ("ag1", "hf-x", "pi", "idle"),
    )
    store.upsert_run("r1", "t1", "completed", created_at="2026-03-09T00:00:00Z")
    store.upsert_subtask("s1", "r1", "sub", "done", agent_id="ag1", created_at="2026-03-09T00:00:00Z")
    store.close()

    runner = CliRunner()
    result = runner.invoke(main, ["stats"])
    assert result.exit_code == 0
    assert "pi: 1 subtask(s)" in result.output
