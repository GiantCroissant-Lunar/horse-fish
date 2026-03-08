"""Tests for Langfuse observability instrumentation."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from horse_fish.observability.traces import RunTrace, Tracer

# ---------------------------------------------------------------------------
# No-op mode tests
# ---------------------------------------------------------------------------


def test_tracer_noop_when_disabled() -> None:
    """Tracer should be no-op when enabled=False."""
    tracer = Tracer(enabled=False)

    trace = tracer.trace_run("run-123", "test task")
    assert trace.run_id == "run-123"
    assert trace.task == "test task"
    assert trace.trace_id is None

    span = tracer.span(trace, "plan", {"key": "value"})
    assert span.name == "plan"
    assert span.span_id is None
    assert span.metadata == {"key": "value"}

    tracer.end_span(span, {"result": "ok"})
    assert span.ended is True
    assert span.output == {"result": "ok"}

    tracer.end_trace(trace, "completed")


def test_tracer_noop_when_missing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tracer should be no-op when LANGFUSE_PUBLIC_KEY is not set."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    tracer = Tracer(enabled=True)

    trace = tracer.trace_run("run-456", "another task")
    assert trace.trace_id is None

    span = tracer.span(trace, "dispatch")
    assert span.span_id is None

    tracer.end_span(span)
    tracer.end_trace(trace, "completed")


# ---------------------------------------------------------------------------
# Functional tests with mocked langfuse
# ---------------------------------------------------------------------------


@patch.dict(
    os.environ,
    {
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sk-test",
        "LANGFUSE_HOST": "http://localhost:3000",
    },
)
@patch("langfuse.Langfuse")
def test_trace_run_creates_trace(mock_langfuse_cls: MagicMock) -> None:
    """trace_run should create a Langfuse trace when enabled."""
    mock_client = MagicMock()
    mock_trace = MagicMock()
    mock_trace.id = "trace-abc-123"
    mock_client.trace.return_value = mock_trace
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)

    trace = tracer.trace_run("run-789", "test task description")

    assert trace.trace_id == "trace-abc-123"
    mock_client.trace.assert_called_once_with(
        id="run-789",
        name="orchestrator_run",
        input={"task": "test task description"},
    )


@patch.dict(
    os.environ,
    {
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sk-test",
    },
)
@patch("langfuse.Langfuse")
def test_span_creates_child_span(mock_langfuse_cls: MagicMock) -> None:
    """span should create a child span within a trace."""
    mock_client = MagicMock()
    mock_trace_obj = MagicMock()
    mock_trace_obj.id = "trace-xyz"
    mock_span_obj = MagicMock()
    mock_span_obj.id = "span-123"
    mock_client.trace.return_value = mock_trace_obj
    mock_client.span.return_value = mock_span_obj
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)
    trace = tracer.trace_run("run-span-test", "span test task")
    span = tracer.span(trace, "execute", {"agent": "claude", "files": ["src/foo.py"]})

    assert span.span_id == "span-123"
    assert span in trace.spans
    mock_client.span.assert_called_once_with(
        trace_id="trace-xyz",
        name="execute",
        metadata={"agent": "claude", "files": ["src/foo.py"]},
    )


@patch.dict(
    os.environ,
    {
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sk-test",
    },
)
@patch("langfuse.Langfuse")
def test_end_span_ends_span(mock_langfuse_cls: MagicMock) -> None:
    """end_span should end the span with output."""
    mock_client = MagicMock()
    mock_span_obj = MagicMock()
    mock_span_obj.id = "span-end-test"
    mock_client.span.return_value = mock_span_obj
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)
    trace = tracer.trace_run("run-end-span", "end span test")
    span = tracer.span(trace, "merge")
    span.span_id = "span-end-test"

    tracer.end_span(span, {"merged": True, "conflicts": 0})

    mock_span_obj.end.assert_called_once_with(output={"merged": True, "conflicts": 0})
    assert span.ended is True


@patch.dict(
    os.environ,
    {
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sk-test",
    },
)
@patch("langfuse.Langfuse")
def test_end_trace_flushes(mock_langfuse_cls: MagicMock) -> None:
    """end_trace should update trace status and flush."""
    mock_client = MagicMock()
    mock_trace_obj = MagicMock()
    mock_trace_obj.id = "trace-flush-test"
    mock_client.trace.return_value = mock_trace_obj
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)
    trace = tracer.trace_run("run-flush", "flush test")
    trace.trace_id = "trace-flush-test"

    tracer.end_trace(trace, "completed")

    mock_trace_obj.update.assert_called_once_with(status="completed")
    mock_client.flush.assert_called_once()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_end_span_idempotent() -> None:
    """end_span should be safe to call multiple times."""
    tracer = Tracer(enabled=False)
    trace = tracer.trace_run("run-idempotent", "test")
    span = tracer.span(trace, "plan")

    tracer.end_span(span, {"first": True})
    first_output = span.output

    tracer.end_span(span, {"second": True})
    # Should not change - first call wins
    assert span.output == first_output
    assert span.ended is True


def test_span_without_trace_id_is_noop() -> None:
    """span should handle trace without trace_id gracefully."""
    tracer = Tracer(enabled=False)
    trace = RunTrace(run_id="no-id", task="test")
    # trace_id is None

    span = tracer.span(trace, "review")
    assert span.span_id is None
    assert span in trace.spans


@patch.dict(
    os.environ,
    {
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sk-test",
    },
)
@patch("langfuse.Langfuse")
def test_exception_in_langfuse_does_not_break_tracer(mock_langfuse_cls: MagicMock) -> None:
    """Tracer should silently handle Langfuse exceptions."""
    mock_client = MagicMock()
    mock_client.trace.side_effect = Exception("Langfuse error")
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)

    # Should not raise
    trace = tracer.trace_run("run-error", "error test")
    assert trace.trace_id is None  # Failed silently

    span = tracer.span(trace, "plan")
    assert span.span_id is None

    tracer.end_span(span)
    tracer.end_trace(trace, "failed")
