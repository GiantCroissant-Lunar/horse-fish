"""Langfuse instrumentation for horse-fish orchestrator run lifecycle."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

# Langfuse is imported at runtime when credentials are present
# For testing, patch 'langfuse.Langfuse' directly
Langfuse = None  # type: ignore[assignment,misc]


@dataclass
class RunTrace:
    """Represents a trace for an orchestrator run."""

    run_id: str
    task: str
    trace_id: str | None = None
    spans: list[Span] = field(default_factory=list)


@dataclass
class Span:
    """Represents a span within a trace (plan, dispatch, execute, review, merge)."""

    name: str
    trace: RunTrace
    span_id: str | None = None
    metadata: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    ended: bool = False


class Tracer:
    """
    Langfuse tracer for orchestrator run lifecycle.

    No-op by default: If LANGFUSE_PUBLIC_KEY not set or enabled=False,
    all methods are silent no-ops.

    Environment variables:
        LANGFUSE_PUBLIC_KEY: Langfuse public key
        LANGFUSE_SECRET_KEY: Langfuse secret key
        LANGFUSE_HOST: Langfuse host URL (optional)
    """

    def __init__(self, enabled: bool = True) -> None:
        """
        Initialize Langfuse client from env vars.

        Args:
            enabled: If False, tracer operates in no-op mode.
        """
        self._enabled = enabled
        self._public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
        self._secret_key = os.environ.get("LANGFUSE_SECRET_KEY")
        self._host = os.environ.get("LANGFUSE_HOST")

        # No-op if disabled or missing credentials
        if not self._enabled or not self._public_key or not self._secret_key:
            self._client = None
            return

        # Import langfuse only when needed and credentials are present
        global Langfuse
        try:
            import langfuse

            Langfuse = langfuse.Langfuse  # type: ignore[assignment]

            self._client = Langfuse(
                public_key=self._public_key,
                secret_key=self._secret_key,
                host=self._host,
            )
        except ImportError:
            self._client = None

    def _is_noop(self) -> bool:
        """Check if tracer is in no-op mode."""
        return self._client is None

    def trace_run(self, run_id: str, task: str) -> RunTrace:
        """
        Start a trace for an orchestrator run.

        Args:
            run_id: Unique identifier for the run.
            task: Task description.

        Returns:
            RunTrace object representing the run trace.
        """
        trace = RunTrace(run_id=run_id, task=task)

        if self._is_noop():
            return trace

        try:
            langfuse_trace = self._client.trace(
                id=run_id,
                name="orchestrator_run",
                input={"task": task},
            )
            trace.trace_id = langfuse_trace.id
        except Exception:
            # Silently fail in production - don't break the run
            pass

        return trace

    def span(self, trace: RunTrace, name: str, metadata: dict[str, Any] | None = None) -> Span:
        """
        Create a span within a trace.

        Args:
            trace: Parent RunTrace.
            name: Span name (e.g., "plan", "dispatch", "execute", "review", "merge").
            metadata: Optional metadata for the span.

        Returns:
            Span object.
        """
        span = Span(name=name, trace=trace, metadata=metadata)

        if self._is_noop() or trace.trace_id is None:
            trace.spans.append(span)
            return span

        try:
            langfuse_span = self._client.span(
                trace_id=trace.trace_id,
                name=name,
                metadata=metadata,
            )
            span.span_id = langfuse_span.id
        except Exception:
            pass

        trace.spans.append(span)
        return span

    def end_span(self, span: Span, output: dict[str, Any] | None = None) -> None:
        """
        End a span with optional output.

        Args:
            span: Span to end.
            output: Optional output data.
        """
        if span.ended:
            return

        span.output = output
        span.ended = True

        if self._is_noop() or span.span_id is None:
            return

        try:
            span_obj = self._client.span(id=span.span_id)
            span_obj.end(output=output)
        except Exception:
            pass

    def end_trace(self, trace: RunTrace, status: str) -> None:
        """
        End the run trace.

        Args:
            trace: RunTrace to end.
            status: Final status of the run (e.g., "completed", "failed").
        """
        if self._is_noop() or trace.trace_id is None:
            return

        try:
            langfuse_trace = self._client.trace(id=trace.trace_id)
            langfuse_trace.update(status=status)
            self._client.flush()
        except Exception:
            pass
