from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY,
            name TEXT,
            runtime TEXT,
            model TEXT,
            capability TEXT,
            state TEXT,
            pid INTEGER,
            tmux_session TEXT,
            worktree_path TEXT,
            branch TEXT,
            task_id TEXT,
            started_at TEXT,
            idle_since TEXT
        );

        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            state TEXT NOT NULL,
            complexity TEXT,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS subtasks (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            description TEXT NOT NULL,
            state TEXT NOT NULL,
            agent_id TEXT,
            deps TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(id)
        );

        CREATE TABLE IF NOT EXISTS mail (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_agent TEXT,
            to_agent TEXT,
            msg_type TEXT,
            subject TEXT,
            body TEXT,
            created_at TEXT,
            read INTEGER DEFAULT 0
        );
        """,
    ),
    (
        2,
        """
        CREATE TABLE IF NOT EXISTS lessons (
            id TEXT PRIMARY KEY,
            run_id TEXT REFERENCES runs(id),
            category TEXT NOT NULL,
            pattern TEXT NOT NULL,
            content TEXT NOT NULL,
            task_signature TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_lessons_category ON lessons(category);
        CREATE INDEX IF NOT EXISTS idx_lessons_pattern ON lessons(pattern);
        """,
    ),
]


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._conn = sqlite3.connect(self._db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema_version_table()

    def _ensure_schema_version_table(self) -> None:
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER PRIMARY KEY
            )
            """
        )
        self._conn.commit()

    def migrate(self) -> None:
        row = self._conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        current = row[0] if row[0] is not None else 0

        for version, sql in MIGRATIONS:
            if version > current:
                self._conn.executescript(sql)
                self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                self._conn.commit()

    def upsert_run(
        self,
        run_id: str,
        task: str,
        state: str,
        complexity: str | None = None,
        created_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        """Insert or update a run record."""
        self.execute(
            """INSERT INTO runs (id, task, state, complexity, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET state=excluded.state, complexity=excluded.complexity,
               completed_at=excluded.completed_at, created_at=COALESCE(excluded.created_at, runs.created_at)""",
            (run_id, task, state, complexity, created_at, completed_at),
        )

    def upsert_subtask(
        self,
        subtask_id: str,
        run_id: str,
        description: str,
        state: str,
        agent_id: str | None = None,
        deps: str | None = None,
        retry_count: int = 0,
        created_at: str | None = None,
    ) -> None:
        """Insert or update a subtask record."""
        self.execute(
            """INSERT INTO subtasks (id, run_id, description, state, agent_id, deps, retry_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET state=excluded.state, agent_id=excluded.agent_id,
               deps=excluded.deps, retry_count=excluded.retry_count""",
            (subtask_id, run_id, description, state, agent_id, deps, retry_count, created_at),
        )

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Cursor:
        cursor = self._conn.execute(sql, params)
        self._conn.commit()
        return cursor

    def fetchone(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> dict[str, Any] | None:
        row = self._conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def fetchall(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[dict[str, Any]]:
        rows = self._conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
