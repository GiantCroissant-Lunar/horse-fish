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
    assert row["v"] == 2
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
            INSERT INTO subtasks (id, run_id, description, state, deps, files_hint)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            ("sub-1", "run-1", "do something", "pending", "[]", "[]"),
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
