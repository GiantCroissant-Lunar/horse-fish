"""State-level middleware chain for the Orchestrator.

Middleware wraps each state handler call (scout, plan, execute, review, merge)
with cross-cutting concerns like tracing, persistence, and logging.

Execution model: onion/nested — first middleware registered runs outermost.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from horse_fish.models import Task, TaskState
from horse_fish.observability.log_context import clear_log_context, set_log_context

if TYPE_CHECKING:
    from horse_fish.observability.traces import Tracer

logger = logging.getLogger(__name__)

Handler = Callable[[Task], Any]  # async (Task) -> Task


@dataclass
class MiddlewareContext:
    """Shared mutable context passed through the middleware chain."""

    trace: Any | None = None
    context_brief: Any | None = None  # ContextBrief from scout phase
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Middleware(Protocol):
    """Protocol for state-level middleware."""

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task: ...


def compose_chain(middlewares: list[Middleware], handler: Handler, ctx: MiddlewareContext) -> Handler:
    """Compose a list of middleware around a handler into a single callable.

    Middleware[0] is outermost (runs first on entry, last on exit).
    """
    chain = handler
    for mw in reversed(middlewares):
        prev = chain

        async def _wrapped(run: Task, _mw=mw, _prev=prev) -> Task:
            return await _mw(run, _prev, ctx)

        chain = _wrapped
    return chain


class TracingMiddleware:
    """Creates and ends Langfuse spans around state handlers."""

    def __init__(self, tracer: Tracer | None) -> None:
        self._tracer = tracer

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task:
        if not self._tracer or ctx.trace is None:
            return await next(run)

        span = self._tracer.span(ctx.trace, run.state.value)
        result = await next(run)
        self._tracer.end_span(
            span,
            {"state": result.state.value},
            metadata={"subtask_count": len(result.subtasks)},
        )
        return result


class LogContextMiddleware:
    """Sets structured logging context around state handlers."""

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task:
        set_log_context(run_id=run.id)
        result = await next(run)
        if result.state == TaskState.executing:
            clear_log_context()
            set_log_context(run_id=run.id)
        return result


class PersistenceMiddleware:
    """Persists task state after each state handler."""

    def __init__(self, persist_fn: Callable[[Task], Any]) -> None:
        self._persist_fn = persist_fn

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task:
        result = await next(run)
        await self._persist_fn(result)
        return result


class MemoryMiddleware:
    """Calls learn function when task reaches completed state."""

    def __init__(self, learn_fn: Callable[[Task], Any]) -> None:
        self._learn_fn = learn_fn

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task:
        result = await next(run)
        if result.state == TaskState.completed:
            try:
                await self._learn_fn(result)
            except Exception:
                logger.warning("Memory learn_fn failed", exc_info=True)
        return result


class ScoutContextMiddleware:
    """Captures context brief after scout phase completes."""

    def __init__(self, get_brief_fn: Callable[[], Any]) -> None:
        self._get_brief_fn = get_brief_fn

    async def __call__(self, run: Task, next: Handler, ctx: MiddlewareContext) -> Task:
        original_state = run.state
        result = await next(run)
        if original_state == TaskState.scouting:
            ctx.context_brief = self._get_brief_fn()
        return result
