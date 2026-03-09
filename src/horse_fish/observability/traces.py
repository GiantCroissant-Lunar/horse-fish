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
    root_id: str | None = None
    spans: list[Span] = field(default_factory=list)
    _root_observation: Any | None = None
    _root_context: Any | None = None


@dataclass
class Span:
    """Represents a Langfuse observation within a trace."""

    name: str
    trace: RunTrace | None
    span_id: str | None = None
    kind: str = "span"
    metadata: dict[str, Any] | None = None
    output: Any | None = None
    ended: bool = False
    _observation: Any | None = None
    _context: Any | None = None


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

    def get_prompt(self, name: str, fallback: str) -> Any | None:
        """Fetch a Langfuse-managed prompt, returning None on failure/no-op."""
        if self._is_noop():
            return None

        try:
            return self._client.get_prompt(name, type="text", fallback=fallback)
        except Exception:
            return None

    def trace_run(
        self,
        run_id: str,
        task: str,
        *,
        metadata: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        user_id: str | None = None,
    ) -> RunTrace:
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
            root_context = self._client.start_as_current_span(
                name="orchestrator_run",
                input={"task": task},
                metadata=metadata,
                end_on_exit=False,
            )
            root_observation = root_context.__enter__()
            trace.trace_id = root_observation.trace_id
            trace.root_id = root_observation.id
            trace._root_observation = root_observation
            trace._root_context = root_context

            self._client.update_current_trace(
                name="orchestrator_run",
                session_id=run_id,
                user_id=user_id,
                input={"task": task},
                metadata=metadata,
                tags=tags,
            )
        except Exception:
            # Silently fail in production - don't break the run
            pass

        return trace

    def span(self, trace: RunTrace | None, name: str, metadata: dict[str, Any] | None = None) -> Span:
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

        if self._is_noop() or (trace is not None and trace._root_observation is None):
            if trace is not None:
                trace.spans.append(span)
            return span

        try:
            span_context = self._client.start_as_current_span(
                name=name,
                metadata=metadata,
                end_on_exit=False,
            )
            observation = span_context.__enter__()
            span.span_id = observation.id
            span._observation = observation
            span._context = span_context
        except Exception:
            pass

        if trace is not None:
            trace.spans.append(span)
        return span

    def generation(
        self,
        trace: RunTrace | None,
        name: str,
        *,
        input: Any | None = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        model_parameters: dict[str, Any] | None = None,
        prompt: Any | None = None,
    ) -> Span:
        """Create a generation observation within a trace."""
        generation = Span(name=name, trace=trace, kind="generation", metadata=metadata)

        if self._is_noop():
            if trace is not None:
                trace.spans.append(generation)
            return generation

        try:
            generation_context = self._client.start_as_current_observation(
                name=name,
                as_type="generation",
                input=input,
                metadata=metadata,
                model=model,
                model_parameters=model_parameters,
                prompt=prompt,
                end_on_exit=False,
            )
            observation = generation_context.__enter__()
            generation.span_id = observation.id
            generation._observation = observation
            generation._context = generation_context
        except Exception:
            pass

        if trace is not None:
            trace.spans.append(generation)
        return generation

    def end_span(
        self,
        span: Span,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
        level: str | None = None,
        status_message: str | None = None,
    ) -> None:
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

        if self._is_noop() or span._observation is None:
            return

        try:
            update_kwargs: dict[str, Any] = {}
            if output is not None:
                update_kwargs["output"] = output
            if metadata is not None:
                update_kwargs["metadata"] = metadata
            if level is not None:
                update_kwargs["level"] = level
            if status_message is not None:
                update_kwargs["status_message"] = status_message
            if update_kwargs:
                span._observation.update(**update_kwargs)
            span._observation.end()
            if span._context is not None:
                span._context.__exit__(None, None, None)
        except Exception:
            pass

    def end_trace(self, trace: RunTrace, status: str, output: Any | None = None) -> None:
        """
        End the run trace.

        Args:
            trace: RunTrace to end.
            status: Final status of the run (e.g., "completed", "failed").
        """
        if self._is_noop() or trace._root_observation is None:
            return

        try:
            trace_output = output if output is not None else {"status": status}
            trace._root_observation.update(output=trace_output)
            trace._root_observation.end()
            if trace._root_context is not None:
                trace._root_context.__exit__(None, None, None)
            self._client.flush()
        except Exception:
            pass

    def score_trace(
        self,
        trace: RunTrace,
        name: str,
        value: float | str,
        *,
        data_type: str | None = None,
        comment: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Create a score on the given trace."""
        if self._is_noop() or trace.trace_id is None:
            return

        try:
            self._client.create_score(
                name=name,
                value=value,
                trace_id=trace.trace_id,
                session_id=trace.run_id,
                data_type=data_type,
                comment=comment,
                metadata=metadata,
            )
        except Exception:
            pass
