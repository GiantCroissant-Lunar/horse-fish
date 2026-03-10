# Structured Logging with Context Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add run_id, subtask_id, and agent_name to all log lines using Python contextvars, and warn when Langfuse is not configured.

**Architecture:** A new `log_context` module provides contextvars and a custom `logging.Filter` that injects context fields into every log record. Context is set at orchestrator entry points (run start, subtask dispatch, review, merge). The filter is attached during logging setup in CLI/RunManager.

**Tech Stack:** Python stdlib `contextvars`, `logging`

---

### Task 1: LogContext module with contextvars and filter

**Files:**
- Create: `src/horse_fish/observability/log_context.py`
- Test: `tests/test_log_context.py`

**Step 1: Write the failing test**

```python
# tests/test_log_context.py
"""Tests for structured logging context."""

from __future__ import annotations

import logging

from horse_fish.observability.log_context import LogContextFilter, set_run_context, clear_run_context


def test_filter_injects_run_id(caplog: pytest.LogCaptureFixture) -> None:
    """LogContextFilter should inject run_id into log records."""
    log = logging.getLogger("test.log_context")
    log.addFilter(LogContextFilter())
    log.setLevel(logging.DEBUG)

    set_run_context(run_id="abc-123")
    with caplog.at_level(logging.INFO, logger="test.log_context"):
        log.info("hello")

    assert caplog.records[0].run_id == "abc-123"  # type: ignore[attr-defined]
    assert caplog.records[0].subtask_id == ""  # type: ignore[attr-defined]
    assert caplog.records[0].agent_name == ""  # type: ignore[attr-defined]
    clear_run_context()


def test_filter_injects_all_fields() -> None:
    """LogContextFilter should inject all context fields."""
    log = logging.getLogger("test.log_context.all")
    filt = LogContextFilter()
    log.addFilter(filt)

    set_run_context(run_id="run-1", subtask_id="sub-2", agent_name="hf-abc")
    record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
    filt.filter(record)

    assert record.run_id == "run-1"  # type: ignore[attr-defined]
    assert record.subtask_id == "sub-2"  # type: ignore[attr-defined]
    assert record.agent_name == "hf-abc"  # type: ignore[attr-defined]
    clear_run_context()


def test_clear_resets_context() -> None:
    """clear_run_context should reset all fields to empty."""
    set_run_context(run_id="run-x", subtask_id="sub-y")
    clear_run_context()

    record = logging.LogRecord("test", logging.INFO, "", 0, "msg", (), None)
    filt = LogContextFilter()
    filt.filter(record)

    assert record.run_id == ""  # type: ignore[attr-defined]
    assert record.subtask_id == ""  # type: ignore[attr-defined]
    assert record.agent_name == ""  # type: ignore[attr-defined]


def test_setup_logging_configures_formatter() -> None:
    """setup_logging should attach filter and structured formatter."""
    from horse_fish.observability.log_context import setup_logging

    handler = setup_logging(level=logging.DEBUG)
    assert any(isinstance(f, LogContextFilter) for f in handler.filters)
    fmt = handler.formatter
    assert fmt is not None
    assert "run_id" in fmt._fmt
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_log_context.py -v`
Expected: FAIL with "ModuleNotFoundError" or "ImportError"

**Step 3: Write minimal implementation**

```python
# src/horse_fish/observability/log_context.py
"""Structured logging context using contextvars."""

from __future__ import annotations

import logging
from contextvars import ContextVar

_run_id: ContextVar[str] = ContextVar("run_id", default="")
_subtask_id: ContextVar[str] = ContextVar("subtask_id", default="")
_agent_name: ContextVar[str] = ContextVar("agent_name", default="")


def set_run_context(
    *,
    run_id: str | None = None,
    subtask_id: str | None = None,
    agent_name: str | None = None,
) -> None:
    """Set structured logging context fields."""
    if run_id is not None:
        _run_id.set(run_id)
    if subtask_id is not None:
        _subtask_id.set(subtask_id)
    if agent_name is not None:
        _agent_name.set(agent_name)


def clear_run_context() -> None:
    """Reset all context fields to empty."""
    _run_id.set("")
    _subtask_id.set("")
    _agent_name.set("")


class LogContextFilter(logging.Filter):
    """Injects run_id, subtask_id, agent_name into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = _run_id.get()  # type: ignore[attr-defined]
        record.subtask_id = _subtask_id.get()  # type: ignore[attr-defined]
        record.agent_name = _agent_name.get()  # type: ignore[attr-defined]
        return True


LOG_FORMAT = "%(asctime)s %(levelname)-7s [run:%(run_id)s] [subtask:%(subtask_id)s] [agent:%(agent_name)s] %(name)s — %(message)s"


def setup_logging(level: int = logging.INFO) -> logging.StreamHandler:
    """Configure root logger with structured context formatter.

    Returns the handler for testing purposes.
    """
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter(LOG_FORMAT))
    handler.addFilter(LogContextFilter())

    root = logging.getLogger("horse_fish")
    root.setLevel(level)
    # Avoid duplicate handlers on repeated calls
    root.handlers = [handler]
    return handler
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_log_context.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/horse_fish/observability/log_context.py tests/test_log_context.py
git commit -m "feat: add structured logging context with contextvars"
```

---

### Task 2: Wire context into Orchestrator

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py:292-346` (run method)
- Modify: `src/horse_fish/orchestrator/engine.py:518-666` (execute method)
- Modify: `src/horse_fish/orchestrator/engine.py:668-890` (review method)
- Modify: `src/horse_fish/orchestrator/engine.py:892-956` (merge method)
- Test: `tests/test_log_context.py` (add integration test)

**Step 1: Write the failing test**

Add to `tests/test_log_context.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from horse_fish.observability.log_context import LogContextFilter, set_run_context, clear_run_context


@pytest.mark.asyncio
async def test_orchestrator_sets_run_context(caplog: pytest.LogCaptureFixture) -> None:
    """Orchestrator.run() should set run_id in log context."""
    import logging
    from horse_fish.orchestrator.engine import Orchestrator
    from horse_fish.models import RunState

    logger = logging.getLogger("horse_fish")
    logger.addFilter(LogContextFilter())

    pool = MagicMock()
    pool.runtime_observation_summary.return_value = {
        "total_count": 0, "tool_count": 0, "prompt_count": 0,
        "first_observed_at": None, "last_observed_at": None,
        "subtasks_with_runtime_observations": 0, "subtask_ids": [],
        "subtask_breakdown": [], "runtimes": {}, "observation_names": {},
        "recent_observations": [],
    }
    planner = AsyncMock()
    planner.decompose.return_value = []  # trigger failure path (fast exit)
    gates = MagicMock()

    orchestrator = Orchestrator(pool=pool, planner=planner, gates=gates)
    run = await orchestrator.run("test task")

    # Find log records from the orchestrator that have run_id set
    run_logs = [r for r in caplog.records if hasattr(r, "run_id") and r.run_id]
    assert len(run_logs) > 0
    assert all(r.run_id == run.id for r in run_logs)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_log_context.py::test_orchestrator_sets_run_context -v`
Expected: FAIL (no run_id attribute on records yet)

**Step 3: Modify engine.py**

Add imports at top of `engine.py`:
```python
from horse_fish.observability.log_context import set_run_context, clear_run_context
```

In `run()` method, after `run = Run.create(task)`:
```python
set_run_context(run_id=run.id[:8])
```

In `run()` finally block, after persisting:
```python
clear_run_context()
```

In `_execute()`, inside the dispatch loop after successful dispatch:
```python
set_run_context(subtask_id=subtask.id[:8], agent_name=slot.name)
```

In `_review()`, inside the subtask loop:
```python
set_run_context(subtask_id=subtask.id[:8], agent_name=subtask.agent[:8] if subtask.agent else "")
```

In `_merge()`, inside the subtask loop:
```python
set_run_context(subtask_id=subtask.id[:8])
```

**Step 4: Run test to verify it passes**

Run: `pytest tests/test_log_context.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/horse_fish/orchestrator/engine.py tests/test_log_context.py
git commit -m "feat: wire structured logging context into orchestrator"
```

---

### Task 3: Wire context into RunManager and add Langfuse warning

**Files:**
- Modify: `src/horse_fish/orchestrator/run_manager.py:143-164` (start method)
- Modify: `src/horse_fish/orchestrator/run_manager.py:218-236` (_run_orchestrator method)
- Test: `tests/test_log_context.py` (add Langfuse warning test)

**Step 1: Write the failing test**

Add to `tests/test_log_context.py`:

```python
def test_langfuse_not_configured_warning(caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    """RunManager should log a warning when Langfuse is not configured."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    from horse_fish.observability.log_context import warn_if_no_langfuse

    with caplog.at_level(logging.WARNING):
        warn_if_no_langfuse()

    assert any("langfuse" in r.message.lower() for r in caplog.records)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_log_context.py::test_langfuse_not_configured_warning -v`
Expected: FAIL (ImportError — function doesn't exist yet)

**Step 3: Add warn_if_no_langfuse to log_context.py**

```python
def warn_if_no_langfuse() -> None:
    """Log a warning if Langfuse credentials are not configured."""
    import os

    if not os.environ.get("LANGFUSE_PUBLIC_KEY") or not os.environ.get("LANGFUSE_SECRET_KEY"):
        logging.getLogger("horse_fish.observability").warning(
            "Langfuse not configured (LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY missing). "
            "Run tracing is disabled. Set these env vars to enable observability."
        )
```

**Step 4: Wire into RunManager**

In `run_manager.py`, add import:
```python
from horse_fish.observability.log_context import set_run_context, clear_run_context, setup_logging, warn_if_no_langfuse
```

In `start()` method, before the while loop:
```python
setup_logging()
warn_if_no_langfuse()
```

In `_run_orchestrator()`, wrap the orchestrator call:
```python
set_run_context(run_id=run_id[:8])
try:
    return await orchestrator.run(task_desc)
finally:
    clear_run_context()
```

**Step 5: Run all tests**

Run: `pytest tests/test_log_context.py -v`
Expected: PASS

**Step 6: Commit**

```bash
git add src/horse_fish/observability/log_context.py src/horse_fish/orchestrator/run_manager.py tests/test_log_context.py
git commit -m "feat: add Langfuse warning and wire logging into RunManager"
```

---

### Task 4: Wire setup_logging into CLI and update __init__.py

**Files:**
- Modify: `src/horse_fish/cli.py` (add setup_logging call in CLI entry)
- Modify: `src/horse_fish/observability/__init__.py` (export new symbols)

**Step 1: Modify CLI**

In `cli.py`, find the `run` command function. Add at the top of the function:
```python
from horse_fish.observability.log_context import setup_logging, warn_if_no_langfuse
setup_logging()
warn_if_no_langfuse()
```

**Step 2: Update __init__.py**

```python
from horse_fish.observability.log_context import LogContextFilter, setup_logging, set_run_context, clear_run_context
```

**Step 3: Run full test suite**

Run: `pytest tests/ -x -q`
Expected: All 514+ tests pass

**Step 4: Commit**

```bash
git add src/horse_fish/cli.py src/horse_fish/observability/__init__.py
git commit -m "feat: wire structured logging into CLI entry point"
```

---

### Task 5: Manual smoke test

**Step 1: Run hf with logging**

```bash
hf run "create a file called /tmp/hf-test.txt with hello world" --foreground --runtime pi
```

**Step 2: Verify log output**

Look for log lines with format:
```
2026-03-09 12:00:00 INFO    [run:abc12345] [subtask:] [agent:] horse_fish.orchestrator.engine — Starting run ...
2026-03-09 12:00:01 WARNING [run:abc12345] [subtask:] [agent:] horse_fish.observability — Langfuse not configured ...
```

**Step 3: Verify context changes during execution**

As the run progresses through phases, subtask_id and agent_name should appear in logs.
