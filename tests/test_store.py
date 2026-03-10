from pathlib import Path

from horse_fish.store.db import Store


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "test.db")
    store.migrate()
    return store


def test_opens_with_wal_mode(tmp_path: Path) -> None:
    with Store(tmp_path / "test.db") as store:
        row = store.fetchone("PRAGMA journal_mode")
        assert row is not None
        assert list(row.values())[0] == "wal"


def test_migrate_creates_tables(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    tables = {r["name"] for r in store.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"agents", "runs", "subtasks", "mail", "schema_version"} <= tables
    store.close()


def test_migrate_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.migrate()  # second call should be a no-op
    row = store.fetchone("SELECT MAX(version) AS v FROM schema_version")
    assert row is not None
    assert row["v"] == 4
    store.close()


def test_execute_and_fetchone(tmp_path: Path) -> None:
    with make_store(tmp_path) as store:
        store.execute(
            "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, ?)",
            ("run-1", "build widget", "pending", "2026-01-01T00:00:00Z"),
        )
        row = store.fetchone("SELECT * FROM runs WHERE id = ?", ("run-1",))
        assert row is not None
        assert row["task"] == "build widget"
        assert row["state"] == "pending"


def test_fetchall(tmp_path: Path) -> None:
    with make_store(tmp_path) as store:
        for i in range(3):
            store.execute(
                "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, ?)",
                (f"run-{i}", f"task-{i}", "pending", "2026-01-01T00:00:00Z"),
            )
        rows = store.fetchall("SELECT * FROM runs ORDER BY id")
        assert len(rows) == 3
        assert rows[0]["id"] == "run-0"


def test_fetchone_returns_none_when_missing(tmp_path: Path) -> None:
    with make_store(tmp_path) as store:
        row = store.fetchone("SELECT * FROM runs WHERE id = ?", ("nonexistent",))
        assert row is None


def test_context_manager_closes(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    with Store(db_path) as store:
        store.migrate()
        store.execute("INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, ?)", ("r1", "t", "x", "now"))
    # Connection should be closed; re-opening should work fine
    with Store(db_path) as store2:
        row = store2.fetchone("SELECT * FROM runs WHERE id = ?", ("r1",))
        assert row is not None


def test_agents_table_schema(tmp_path: Path) -> None:
    with make_store(tmp_path) as store:
        store.execute(
            """
            INSERT INTO agents (id, name, runtime, model, capability, state)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("agent-1", "store-claude", "claude", "claude-sonnet-4-6", "builder", "idle"),
        )
        row = store.fetchone("SELECT * FROM agents WHERE id = ?", ("agent-1",))
        assert row is not None
        assert row["name"] == "store-claude"
        assert row["capability"] == "builder"


def test_mail_autoincrement(tmp_path: Path) -> None:
    with make_store(tmp_path) as store:
        for i in range(3):
            store.execute(
                "INSERT INTO mail (from_agent, to_agent, msg_type, subject, body, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                ("a", "b", "status", f"msg {i}", "body", "now"),
            )
        rows = store.fetchall("SELECT id FROM mail ORDER BY id")
        assert [r["id"] for r in rows] == [1, 2, 3]


def test_subtasks_table(tmp_path: Path) -> None:
    with make_store(tmp_path) as store:
        store.execute(
            "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, ?)",
            ("run-1", "task", "pending", "now"),
        )
        store.execute(
            """
            INSERT INTO subtasks (id, run_id, description, state, deps, retry_count, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("sub-1", "run-1", "do something", "pending", "[]", "0", "now"),
        )
        row = store.fetchone("SELECT * FROM subtasks WHERE id = ?", ("sub-1",))
        assert row is not None
        assert row["run_id"] == "run-1"
        assert row["deps"] == "[]"


def test_lessons_table_exists(tmp_path: Path) -> None:
    """Lessons table should exist after migration."""
    store = make_store(tmp_path)
    store.migrate()
    result = store.fetchone("SELECT name FROM sqlite_master WHERE type='table' AND name='lessons'")
    assert result is not None
    store.close()


def test_lessons_insert_and_query(tmp_path: Path) -> None:
    """Should be able to insert and query lessons."""
    store = make_store(tmp_path)
    store.migrate()
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, ?)",
        ("run-1", "test task", "pending", "2026-03-08T00:00:00"),
    )
    store.execute(
        "INSERT INTO lessons (id, run_id, category, pattern, content, task_signature, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "lesson-1",
            "run-1",
            "planner",
            "over_decomposed",
            "Task was split into 3 subtasks but only touched 1 file",
            "add version",
            "2026-03-08T00:00:00",
        ),
    )
    row = store.fetchone("SELECT * FROM lessons WHERE id = ?", ("lesson-1",))
    assert row is not None
    assert row["category"] == "planner"
    assert row["pattern"] == "over_decomposed"
    store.close()


def test_lessons_query_by_category(tmp_path: Path) -> None:
    """Should query lessons by category."""
    store = make_store(tmp_path)
    store.migrate()
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, ?, ?)",
        ("run-1", "test task", "pending", "2026-03-08T00:00:00"),
    )
    store.execute(
        "INSERT INTO lessons (id, run_id, category, pattern, content, task_signature, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("lesson-1", "run-1", "planner", "over_decomposed", "content", "sig", "2026-03-08T00:00:00"),
    )
    store.execute(
        "INSERT INTO lessons (id, run_id, category, pattern, content, task_signature, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("lesson-2", "run-1", "dispatch", "agent_stalled", "content", "sig", "2026-03-08T00:00:00"),
    )
    rows = store.fetchall("SELECT * FROM lessons WHERE category = ?", ("planner",))
    assert len(rows) == 1
    assert rows[0]["id"] == "lesson-1"
    store.close()


def test_runs_table_has_complexity_column(tmp_path: Path) -> None:
    """Runs table should have complexity column."""
    store = make_store(tmp_path)
    store.migrate()
    columns = {r["name"] for r in store.fetchall("PRAGMA table_info(runs)")}
    assert "complexity" in columns
    store.close()


def test_subtasks_table_has_retry_count_and_created_at(tmp_path: Path) -> None:
    """Subtasks table should have retry_count and created_at columns."""
    store = make_store(tmp_path)
    store.migrate()
    columns = {r["name"] for r in store.fetchall("PRAGMA table_info(subtasks)")}
    assert "retry_count" in columns
    assert "created_at" in columns
    store.close()


def test_upsert_run_insert(tmp_path: Path) -> None:
    """upsert_run should insert a new run."""
    store = make_store(tmp_path)
    store.migrate()
    store.upsert_run(
        run_id="run-1",
        task="test task",
        state="planning",
        complexity="SOLO",
        created_at="2026-03-09T00:00:00Z",
    )
    row = store.fetchone("SELECT * FROM runs WHERE id = ?", ("run-1",))
    assert row is not None
    assert row["task"] == "test task"
    assert row["state"] == "planning"
    assert row["complexity"] == "SOLO"
    store.close()


def test_upsert_run_update(tmp_path: Path) -> None:
    """upsert_run should update an existing run."""
    store = make_store(tmp_path)
    store.migrate()
    store.upsert_run(
        run_id="run-1",
        task="test task",
        state="planning",
        created_at="2026-03-09T00:00:00Z",
    )
    # On update, pass the original created_at to preserve it
    store.upsert_run(
        run_id="run-1",
        task="test task",
        state="completed",
        complexity="TRIO",
        created_at="2026-03-09T00:00:00Z",
        completed_at="2026-03-09T01:00:00Z",
    )
    row = store.fetchone("SELECT * FROM runs WHERE id = ?", ("run-1",))
    assert row is not None
    assert row["state"] == "completed"
    assert row["complexity"] == "TRIO"
    assert row["completed_at"] == "2026-03-09T01:00:00Z"
    store.close()


def test_upsert_subtask_insert(tmp_path: Path) -> None:
    """upsert_subtask should insert a new subtask."""
    store = make_store(tmp_path)
    store.migrate()
    store.upsert_run(
        run_id="run-1",
        task="test task",
        state="planning",
        created_at="2026-03-09T00:00:00Z",
    )
    store.upsert_subtask(
        subtask_id="sub-1",
        run_id="run-1",
        description="do something",
        state="pending",
        deps='["dep-1"]',
        retry_count=0,
        created_at="2026-03-09T00:00:00Z",
    )
    row = store.fetchone("SELECT * FROM subtasks WHERE id = ?", ("sub-1",))
    assert row is not None
    assert row["description"] == "do something"
    assert row["state"] == "pending"
    assert row["deps"] == '["dep-1"]'
    assert row["retry_count"] == 0
    store.close()


def test_upsert_subtask_update(tmp_path: Path) -> None:
    """upsert_subtask should update an existing subtask."""
    store = make_store(tmp_path)
    store.migrate()
    store.upsert_run(
        run_id="run-1",
        task="test task",
        state="planning",
        created_at="2026-03-09T00:00:00Z",
    )
    store.upsert_subtask(
        subtask_id="sub-1",
        run_id="run-1",
        description="do something",
        state="pending",
        created_at="2026-03-09T00:00:00Z",
    )
    store.upsert_subtask(
        subtask_id="sub-1",
        run_id="run-1",
        description="do something",
        state="done",
        agent_id="agent-1",
        retry_count=1,
        created_at="2026-03-09T00:00:00Z",
    )
    row = store.fetchone("SELECT * FROM subtasks WHERE id = ?", ("sub-1",))
    assert row is not None
    assert row["state"] == "done"
    assert row["agent_id"] == "agent-1"
    assert row["retry_count"] == 1
    store.close()


def test_insert_queued_run(tmp_path: Path) -> None:
    """insert_queued_run should insert a run with state 'queued'."""
    store = make_store(tmp_path)
    store.migrate()
    store.insert_queued_run("run-1", "test task")
    row = store.fetchone("SELECT * FROM runs WHERE id = ?", ("run-1",))
    assert row is not None
    assert row["state"] == "queued"
    assert row["task"] == "test task"
    assert row["created_at"] is not None
    store.close()


def test_fetch_queued_runs_ordering(tmp_path: Path) -> None:
    """fetch_queued_runs should return oldest first."""
    store = make_store(tmp_path)
    store.migrate()
    # Insert with explicit timestamps to control ordering
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
        ("run-3", "task 3", "2026-03-09T03:00:00Z"),
    )
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
        ("run-1", "task 1", "2026-03-09T01:00:00Z"),
    )
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
        ("run-2", "task 2", "2026-03-09T02:00:00Z"),
    )
    runs = store.fetch_queued_runs()
    assert len(runs) == 3
    assert runs[0]["id"] == "run-1"
    assert runs[1]["id"] == "run-2"
    assert runs[2]["id"] == "run-3"
    store.close()


def test_fetch_queued_runs_excludes_active(tmp_path: Path) -> None:
    """fetch_queued_runs should only return queued runs, not active ones."""
    store = make_store(tmp_path)
    store.migrate()
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
        ("run-queued", "queued task", "2026-03-09T01:00:00Z"),
    )
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'executing', ?)",
        ("run-executing", "executing task", "2026-03-09T02:00:00Z"),
    )
    runs = store.fetch_queued_runs()
    assert len(runs) == 1
    assert runs[0]["id"] == "run-queued"
    store.close()


def test_fetch_active_runs(tmp_path: Path) -> None:
    """fetch_active_runs should return only runs in active states."""
    store = make_store(tmp_path)
    store.migrate()
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
        ("run-queued", "queued task", "2026-03-09T01:00:00Z"),
    )
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'planning', ?)",
        ("run-planning", "planning task", "2026-03-09T02:00:00Z"),
    )
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'executing', ?)",
        ("run-executing", "executing task", "2026-03-09T03:00:00Z"),
    )
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'completed', ?)",
        ("run-completed", "completed task", "2026-03-09T04:00:00Z"),
    )
    runs = store.fetch_active_runs()
    assert len(runs) == 2
    ids = {r["id"] for r in runs}
    assert ids == {"run-planning", "run-executing"}
    store.close()


def test_update_run_state(tmp_path: Path) -> None:
    """update_run_state should update a run's state."""
    store = make_store(tmp_path)
    store.migrate()
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
        ("run-1", "test task", "2026-03-09T01:00:00Z"),
    )
    store.update_run_state("run-1", "planning")
    row = store.fetchone("SELECT * FROM runs WHERE id = ?", ("run-1",))
    assert row is not None
    assert row["state"] == "planning"
    store.close()


def test_update_run_state_with_completed_at(tmp_path: Path) -> None:
    """update_run_state should set completed_at when provided."""
    store = make_store(tmp_path)
    store.migrate()
    store.execute(
        "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'planning', ?)",
        ("run-1", "test task", "2026-03-09T01:00:00Z"),
    )
    store.update_run_state("run-1", "completed", "2026-03-09T02:00:00Z")
    row = store.fetchone("SELECT * FROM runs WHERE id = ?", ("run-1",))
    assert row is not None
    assert row["state"] == "completed"
    assert row["completed_at"] == "2026-03-09T02:00:00Z"
    store.close()
