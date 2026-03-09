"""Tests for Langfuse observability instrumentation."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from horse_fish.observability.traces import RunTrace, Tracer


def make_mock_context(observation: MagicMock) -> MagicMock:
    """Build a context manager mock that yields a Langfuse observation."""
    context = MagicMock()
    context.__enter__.return_value = observation
    context.__exit__.return_value = False
    return context


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
    """Tracer should be no-op when Langfuse credentials are absent."""
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    tracer = Tracer(enabled=True)

    trace = tracer.trace_run("run-456", "another task")
    assert trace.trace_id is None

    span = tracer.span(trace, "dispatch")
    assert span.span_id is None

    tracer.end_span(span)
    tracer.end_trace(trace, "completed")


@patch.dict(
    os.environ,
    {
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sk-test",
        "LANGFUSE_HOST": "http://localhost:3000",
    },
)
@patch("langfuse.Langfuse")
def test_trace_run_creates_root_observation(mock_langfuse_cls: MagicMock) -> None:
    """trace_run should create a root observation and set trace attributes."""
    mock_client = MagicMock()
    mock_root = MagicMock()
    mock_root.id = "root-abc-123"
    mock_root.trace_id = "trace-abc-123"
    mock_client.start_as_current_span.return_value = make_mock_context(mock_root)
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)

    trace = tracer.trace_run(
        "run-789",
        "test task description",
        metadata={"runtime": "claude"},
        tags=["runtime:claude"],
    )

    assert trace.trace_id == "trace-abc-123"
    assert trace.root_id == "root-abc-123"
    mock_client.start_as_current_span.assert_called_once_with(
        name="orchestrator_run",
        input={"task": "test task description"},
        metadata={"runtime": "claude"},
        end_on_exit=False,
    )
    mock_client.update_current_trace.assert_called_once_with(
        name="orchestrator_run",
        session_id="run-789",
        user_id=None,
        input={"task": "test task description"},
        metadata={"runtime": "claude"},
        tags=["runtime:claude"],
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
    mock_root = MagicMock()
    mock_root.id = "root-xyz"
    mock_root.trace_id = "trace-xyz"
    mock_span_obj = MagicMock()
    mock_span_obj.id = "span-123"
    mock_client.start_as_current_span.side_effect = [
        make_mock_context(mock_root),
        make_mock_context(mock_span_obj),
    ]
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)
    trace = tracer.trace_run("run-span-test", "span test task")
    span = tracer.span(trace, "execute", {"agent": "claude", "files": ["src/foo.py"]})

    assert span.span_id == "span-123"
    assert span in trace.spans
    mock_client.start_as_current_span.assert_called_with(
        name="execute",
        metadata={"agent": "claude", "files": ["src/foo.py"]},
        end_on_exit=False,
    )


@patch.dict(
    os.environ,
    {
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sk-test",
    },
)
@patch("langfuse.Langfuse")
def test_generation_creates_generation_observation(mock_langfuse_cls: MagicMock) -> None:
    """generation should create a generation observation within the trace."""
    mock_client = MagicMock()
    mock_root = MagicMock()
    mock_root.id = "root-gen"
    mock_root.trace_id = "trace-gen"
    mock_generation = MagicMock()
    mock_generation.id = "gen-123"
    mock_client.start_as_current_span.return_value = make_mock_context(mock_root)
    mock_client.start_as_current_observation.return_value = make_mock_context(mock_generation)
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)
    trace = tracer.trace_run("run-generation", "generation test")
    generation = tracer.generation(
        trace,
        "planner.decompose",
        input={"prompt": "test prompt"},
        metadata={"runtime": "claude"},
        model="claude-sonnet-4-6",
        model_parameters={"runtime": "claude"},
    )

    assert generation.span_id == "gen-123"
    assert generation.kind == "generation"
    mock_client.start_as_current_observation.assert_called_once_with(
        name="planner.decompose",
        as_type="generation",
        input={"prompt": "test prompt"},
        metadata={"runtime": "claude"},
        model="claude-sonnet-4-6",
        model_parameters={"runtime": "claude"},
        prompt=None,
        end_on_exit=False,
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
    """end_span should update and end the observation."""
    mock_client = MagicMock()
    mock_root = MagicMock()
    mock_root.id = "root-end-span"
    mock_root.trace_id = "trace-end-span"
    mock_span_obj = MagicMock()
    mock_span_obj.id = "span-end-test"
    span_context = make_mock_context(mock_span_obj)
    mock_client.start_as_current_span.side_effect = [
        make_mock_context(mock_root),
        span_context,
    ]
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)
    trace = tracer.trace_run("run-end-span", "end span test")
    span = tracer.span(trace, "merge")

    tracer.end_span(span, {"merged": True, "conflicts": 0})

    mock_span_obj.update.assert_called_once_with(output={"merged": True, "conflicts": 0})
    mock_span_obj.end.assert_called_once_with()
    span_context.__exit__.assert_called_once_with(None, None, None)
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
    """end_trace should update the root observation output and flush."""
    mock_client = MagicMock()
    mock_root = MagicMock()
    mock_root.id = "root-flush-test"
    mock_root.trace_id = "trace-flush-test"
    root_context = make_mock_context(mock_root)
    mock_client.start_as_current_span.return_value = root_context
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)
    trace = tracer.trace_run("run-flush", "flush test")

    tracer.end_trace(trace, "completed", output={"status": "completed", "subtask_count": 1})

    mock_root.update.assert_called_once_with(output={"status": "completed", "subtask_count": 1})
    mock_root.end.assert_called_once_with()
    root_context.__exit__.assert_called_once_with(None, None, None)
    mock_client.flush.assert_called_once()


def test_end_span_idempotent() -> None:
    """end_span should be safe to call multiple times."""
    tracer = Tracer(enabled=False)
    trace = tracer.trace_run("run-idempotent", "test")
    span = tracer.span(trace, "plan")

    tracer.end_span(span, {"first": True})
    first_output = span.output

    tracer.end_span(span, {"second": True})

    assert span.output == first_output
    assert span.ended is True


def test_span_without_trace_id_is_noop() -> None:
    """span should handle trace without trace_id gracefully."""
    tracer = Tracer(enabled=False)
    trace = RunTrace(run_id="no-id", task="test")

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
    mock_client.start_as_current_span.side_effect = Exception("Langfuse error")
    mock_langfuse_cls.return_value = mock_client

    tracer = Tracer(enabled=True)

    trace = tracer.trace_run("run-error", "error test")
    assert trace.trace_id is None

    span = tracer.span(trace, "plan")
    assert span.span_id is None

    tracer.end_span(span)
    tracer.end_trace(trace, "failed")
