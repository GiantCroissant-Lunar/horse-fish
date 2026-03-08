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
            task TEXT,
            state TEXT,
            created_at TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS subtasks (
            id TEXT PRIMARY KEY,
            run_id TEXT REFERENCES runs(id),
            description TEXT,
            agent TEXT,
            deps TEXT,
            files_hint TEXT,
            state TEXT,
            result_output TEXT,
            result_diff TEXT,
            result_duration REAL,
            result_success INTEGER
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
