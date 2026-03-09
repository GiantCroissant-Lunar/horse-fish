"""Structured logging with contextvars for horse-fish orchestrator.

Provides context-aware logging where run_id, subtask_id, and agent_id
are automatically injected into log records across async boundaries.
"""

from __future__ import annotations

import logging
import os
import sys
from contextvars import ContextVar
from typing import Any

# Context variables for structured logging
run_id_var: ContextVar[str | None] = ContextVar("run_id", default=None)
subtask_id_var: ContextVar[str | None] = ContextVar("subtask_id", default=None)
agent_id_var: ContextVar[str | None] = ContextVar("agent_id", default=None)


class LogContextFilter(logging.Filter):
    """Logging filter that injects contextvars into log records.

    Automatically adds run_id, subtask_id, and agent_id from contextvars
    to each log record for structured logging.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Inject contextvars into the log record.

        Args:
            record: The log record to enrich with context.

        Returns:
            True to allow the record to be processed.
        """
        record.run_id = run_id_var.get()  # type: ignore[attr-defined]
        record.subtask_id = subtask_id_var.get()  # type: ignore[attr-defined]
        record.agent_id = agent_id_var.get()  # type: ignore[attr-defined]
        return True


def set_log_context(
    *,
    run_id: str | None = None,
    subtask_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[Any, ...]:
    """Set logging context variables.

    Sets the contextvars for run_id, subtask_id, and/or agent_id.
    Returns tokens that can be used to reset the context later.

    Args:
        run_id: The run ID to set in context.
        subtask_id: The subtask ID to set in context.
        agent_id: The agent ID to set in context.

    Returns:
        Tuple of tokens for resetting context (use with reset_log_context).
    """
    tokens = []
    if run_id is not None:
        tokens.append(run_id_var.set(run_id))
    if subtask_id is not None:
        tokens.append(subtask_id_var.set(subtask_id))
    if agent_id is not None:
        tokens.append(agent_id_var.set(agent_id))
    return tuple(tokens)


def reset_log_context(*tokens: Any) -> None:
    """Reset logging context variables using tokens from set_log_context.

    Args:
        tokens: Tokens returned by set_log_context to reset.
    """
    # Reset in reverse order to handle nested contexts properly
    for token in reversed(tokens):
        # Determine which var this token belongs to by trying each
        try:
            run_id_var.reset(token)
        except ValueError:
            try:
                subtask_id_var.reset(token)
            except ValueError:
                try:
                    agent_id_var.reset(token)
                except ValueError:
                    pass  # Token didn't match any var, ignore


def clear_log_context() -> None:
    """Clear all logging context variables.

    Convenience function to reset all contextvars to None.
    """
    run_id_var.set(None)
    subtask_id_var.set(None)
    agent_id_var.set(None)


def get_log_context() -> dict[str, str | None]:
    """Get current logging context as a dictionary.

    Returns:
        Dictionary with run_id, subtask_id, and agent_id values.
    """
    return {
        "run_id": run_id_var.get(),
        "subtask_id": subtask_id_var.get(),
        "agent_id": agent_id_var.get(),
    }


def setup_logging(
    level: int | str = logging.INFO,
    fmt: str | None = None,
    stream: Any = None,
) -> None:
    """Set up structured logging with contextvars support.

    Configures the root logger with a LogContextFilter and a formatter
    that includes run_id, subtask_id, and agent_id in log output.

    Args:
        level: Logging level (default: INFO).
        fmt: Custom format string. If None, uses structured format with context.
        stream: Output stream (default: sys.stderr).
    """
    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    if fmt is None:
        fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        # Add context fields if any are present
        context_fmt = []
        if run_id_var.get() is not None:
            context_fmt.append("run=%(run_id)s")
        if subtask_id_var.get() is not None:
            context_fmt.append("subtask=%(subtask_id)s")
        if agent_id_var.get() is not None:
            context_fmt.append("agent=%(agent_id)s")
        if context_fmt:
            fmt = "%(asctime)s - %(name)s - %(levelname)s - " + " ".join(context_fmt) + " - %(message)s"

    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(logging.Formatter(fmt))
    handler.addFilter(LogContextFilter())

    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers to avoid duplicates
    for h in root_logger.handlers[:]:
        root_logger.removeHandler(h)

    root_logger.addHandler(handler)

    # Also configure horse_fish loggers
    hf_logger = logging.getLogger("horse_fish")
    hf_logger.setLevel(level)


def warn_if_no_langfuse() -> None:
    """Emit a warning if Langfuse credentials are not configured.

    Checks for LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY environment
    variables and logs a warning if either is missing.
    """
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY")

    if not public_key or not secret_key:
        logger = logging.getLogger(__name__)
        logger.warning(
            "Langfuse credentials not configured. "
            "Set LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY environment variables "
            "to enable observability traces."
        )


# Convenience exports
__all__ = [
    "LogContextFilter",
    "agent_id_var",
    "clear_log_context",
    "get_log_context",
    "reset_log_context",
    "run_id_var",
    "set_log_context",
    "setup_logging",
    "subtask_id_var",
    "warn_if_no_langfuse",
]
