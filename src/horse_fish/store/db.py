from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
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
    (
        3,
        """
        -- Recreate runs and subtasks with full schema (pre-dashboard tables lacked columns)
        DROP TABLE IF EXISTS subtasks;
        DROP TABLE IF EXISTS runs;

        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            task TEXT NOT NULL,
            state TEXT NOT NULL,
            complexity TEXT,
            created_at TEXT NOT NULL DEFAULT '',
            completed_at TEXT
        );

        CREATE TABLE subtasks (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            description TEXT NOT NULL,
            state TEXT NOT NULL,
            agent_id TEXT,
            deps TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (run_id) REFERENCES runs(id)
        );
        """,
    ),
    (
        4,
        """
        -- Add run_id and pgid columns to agents table for process tracking
        ALTER TABLE agents ADD COLUMN run_id TEXT;
        ALTER TABLE agents ADD COLUMN pgid INTEGER;
        """,
    ),
    (
        5,
        """
        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            goal_conditions TEXT NOT NULL DEFAULT '[]',
            state TEXT NOT NULL DEFAULT 'planning',
            round INTEGER NOT NULL DEFAULT 0,
            max_rounds INTEGER NOT NULL DEFAULT 10,
            created_at TEXT NOT NULL,
            completed_at TEXT
        );

        ALTER TABLE runs ADD COLUMN plan_id TEXT REFERENCES plans(id);
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

    def fetch_recent_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch recent runs, ordered by creation date (newest first)."""
        return self.fetchall(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    def fetch_run(self, run_id: str) -> dict[str, Any] | None:
        """Fetch a single run by ID (supports prefix match)."""
        exact = self.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
        if exact:
            return exact
        matches = self.fetchall("SELECT * FROM runs WHERE id LIKE ?", (f"{run_id}%",))
        return matches[0] if len(matches) == 1 else None

    def fetch_subtasks(self, run_id: str) -> list[dict[str, Any]]:
        """Fetch all subtasks for a given run ID, ordered by creation date."""
        return self.fetchall(
            "SELECT * FROM subtasks WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        )

    def fetch_run_stats(self) -> dict[str, Any]:
        """Fetch aggregate run statistics."""
        total_row = self.fetchone("SELECT COUNT(*) AS total FROM runs")
        total = total_row["total"] if total_row else 0

        state_rows = self.fetchall("SELECT state, COUNT(*) AS cnt FROM runs GROUP BY state")
        by_state: dict[str, int] = {r["state"]: r["cnt"] for r in state_rows}

        avg_row = self.fetchone(
            """SELECT AVG(
                   (julianday(completed_at) - julianday(created_at)) * 86400
               ) AS avg_secs
               FROM runs WHERE completed_at IS NOT NULL AND created_at != ''"""
        )
        avg_duration_secs: float | None = avg_row["avg_secs"] if avg_row else None

        runtime_rows = self.fetchall(
            """SELECT a.runtime, COUNT(DISTINCT s.id) AS cnt
               FROM subtasks s JOIN agents a ON s.agent_id = a.id
               GROUP BY a.runtime ORDER BY cnt DESC"""
        )
        runtimes: list[dict[str, Any]] = [{"runtime": r["runtime"], "count": r["cnt"]} for r in runtime_rows]

        return {
            "total_runs": total,
            "by_state": by_state,
            "avg_duration_secs": avg_duration_secs,
            "runtimes": runtimes,
        }

    def insert_queued_run(self, run_id: str, task: str) -> None:
        """Insert a new run in 'queued' state."""

        now = datetime.now(UTC).isoformat()
        self.execute(
            "INSERT INTO runs (id, task, state, created_at) VALUES (?, ?, 'queued', ?)",
            (run_id, task, now),
        )

    def fetch_queued_runs(self, limit: int = 10) -> list[dict]:
        """Fetch runs in 'queued' state, oldest first."""
        return self.fetchall(
            "SELECT * FROM runs WHERE state = 'queued' ORDER BY created_at ASC LIMIT ?",
            (limit,),
        )

    def fetch_active_runs(self) -> list[dict]:
        """Fetch runs in active states (planning, executing, reviewing, merging)."""
        return self.fetchall(
            "SELECT * FROM runs WHERE state IN ('planning', 'executing', 'reviewing', 'merging') "
            "ORDER BY created_at ASC",
        )

    # --- Plan methods ---

    def insert_plan(self, plan_id: str, goal: str) -> None:
        """Insert a new plan in 'planning' state."""
        now = datetime.now(UTC).isoformat()
        self.execute(
            "INSERT INTO plans (id, goal, state, created_at) VALUES (?, ?, 'planning', ?)",
            (plan_id, goal, now),
        )

    def fetch_plan(self, plan_id: str) -> dict[str, Any] | None:
        """Fetch a single plan by ID (supports prefix match)."""
        exact = self.fetchone("SELECT * FROM plans WHERE id = ?", (plan_id,))
        if exact:
            return exact
        matches = self.fetchall("SELECT * FROM plans WHERE id LIKE ?", (f"{plan_id}%",))
        return matches[0] if len(matches) == 1 else None

    def update_plan_state(self, plan_id: str, state: str, completed_at: str | None = None) -> None:
        """Update a plan's state, optionally setting completed_at."""
        if completed_at:
            self.execute(
                "UPDATE plans SET state = ?, completed_at = ? WHERE id = ?",
                (state, completed_at, plan_id),
            )
        else:
            self.execute("UPDATE plans SET state = ? WHERE id = ?", (state, plan_id))

    def update_plan_round(self, plan_id: str, round: int, goal_conditions: list[str] | None = None) -> None:
        """Update a plan's round counter and optionally goal_conditions."""
        import json

        if goal_conditions is not None:
            self.execute(
                "UPDATE plans SET round = ?, goal_conditions = ? WHERE id = ?",
                (round, json.dumps(goal_conditions), plan_id),
            )
        else:
            self.execute("UPDATE plans SET round = ? WHERE id = ?", (round, plan_id))

    def fetch_active_plans(self) -> list[dict[str, Any]]:
        """Fetch plans in active states (planning, executing, replanning)."""
        return self.fetchall(
            "SELECT * FROM plans WHERE state IN ('planning', 'executing', 'replanning') ORDER BY created_at ASC",
        )

    def fetch_recent_plans(self, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch recent plans, ordered by creation date (newest first)."""
        return self.fetchall(
            "SELECT * FROM plans ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )

    def fetch_plan_tasks(self, plan_id: str) -> list[dict[str, Any]]:
        """Fetch all runs (tasks) associated with a plan."""
        return self.fetchall(
            "SELECT * FROM runs WHERE plan_id = ? ORDER BY created_at",
            (plan_id,),
        )

    def upsert_plan(
        self,
        plan_id: str,
        goal: str,
        state: str,
        goal_conditions: list[str] | None = None,
        round: int = 0,
        created_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        """Insert or update a plan record."""
        import json

        conditions_json = json.dumps(goal_conditions) if goal_conditions is not None else "[]"
        self.execute(
            """INSERT INTO plans (id, goal, state, goal_conditions, round, created_at, completed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET goal=excluded.goal, state=excluded.state,
               goal_conditions=excluded.goal_conditions, round=excluded.round,
               completed_at=excluded.completed_at,
               created_at=COALESCE(excluded.created_at, plans.created_at)""",
            (plan_id, goal, state, conditions_json, round, created_at, completed_at),
        )

    def update_run_state(self, run_id: str, state: str, completed_at: str | None = None) -> None:
        """Update a run's state, optionally setting completed_at."""
        if completed_at:
            self.execute(
                "UPDATE runs SET state = ?, completed_at = ? WHERE id = ?",
                (state, completed_at, run_id),
            )
        else:
            self.execute("UPDATE runs SET state = ? WHERE id = ?", (state, run_id))

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
