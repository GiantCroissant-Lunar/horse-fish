"""Tests for the orchestrator middleware chain."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horse_fish.models import Task, TaskState
from horse_fish.orchestrator.middleware import (
    LogContextMiddleware,
    MemoryMiddleware,
    Middleware,
    MiddlewareContext,
    PersistenceMiddleware,
    ScoutContextMiddleware,
    TracingMiddleware,
    compose_chain,
)


def _make_task(state: TaskState = TaskState.scouting) -> Task:
    t = Task.create(task="test task")
    t.state = state
    return t


def _transition(run: Task, state: TaskState) -> Task:
    run.state = state
    return run


# --- compose_chain tests ---


@pytest.mark.asyncio
async def test_compose_chain_calls_handler():
    """Empty middleware list — handler called directly."""
    task = _make_task()

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.planning)

    ctx = MiddlewareContext()
    chain = compose_chain([], handler, ctx)
    result = await chain(task)
    assert result.state == TaskState.planning


@pytest.mark.asyncio
async def test_compose_chain_middleware_order():
    """Two middleware — verify onion order: a_before, b_before, b_after, a_after."""
    order: list[str] = []

    class MwA:
        async def __call__(self, run: Task, next, ctx: MiddlewareContext) -> Task:
            order.append("a_before")
            result = await next(run)
            order.append("a_after")
            return result

    class MwB:
        async def __call__(self, run: Task, next, ctx: MiddlewareContext) -> Task:
            order.append("b_before")
            result = await next(run)
            order.append("b_after")
            return result

    async def handler(run: Task) -> Task:
        order.append("handler")
        return run

    ctx = MiddlewareContext()
    chain = compose_chain([MwA(), MwB()], handler, ctx)
    await chain(_make_task())
    assert order == ["a_before", "b_before", "handler", "b_after", "a_after"]


@pytest.mark.asyncio
async def test_compose_chain_middleware_can_modify_run():
    """Middleware modifies run before passing to next."""

    class ModifyMw:
        async def __call__(self, run: Task, next, ctx: MiddlewareContext) -> Task:
            run.task = "modified"
            return await next(run)

    async def handler(run: Task) -> Task:
        assert run.task == "modified"
        return run

    ctx = MiddlewareContext()
    chain = compose_chain([ModifyMw()], handler, ctx)
    result = await chain(_make_task())
    assert result.task == "modified"


# --- TracingMiddleware tests ---


@pytest.mark.asyncio
async def test_tracing_middleware_creates_and_ends_span():
    """Verify span created and ended with correct args."""
    tracer = MagicMock()
    span = MagicMock()
    tracer.span.return_value = span
    trace = MagicMock()

    task = _make_task(TaskState.planning)

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.executing)

    ctx = MiddlewareContext(trace=trace)
    mw = TracingMiddleware(tracer)
    result = await mw(task, handler, ctx)

    tracer.span.assert_called_once_with(trace, TaskState.planning.value)
    tracer.end_span.assert_called_once()
    call_args = tracer.end_span.call_args
    assert call_args[0][0] is span
    assert call_args[0][1] == {"state": TaskState.executing.value}
    assert result.state == TaskState.executing


@pytest.mark.asyncio
async def test_tracing_middleware_skips_when_no_tracer():
    """No-op pass-through when tracer is None."""
    task = _make_task()

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.planning)

    ctx = MiddlewareContext()
    mw = TracingMiddleware(None)
    result = await mw(task, handler, ctx)
    assert result.state == TaskState.planning


@pytest.mark.asyncio
async def test_tracing_middleware_skips_when_no_trace():
    """Tracer exists but ctx.trace is None — skip tracing."""
    tracer = MagicMock()
    task = _make_task()

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.planning)

    ctx = MiddlewareContext(trace=None)
    mw = TracingMiddleware(tracer)
    result = await mw(task, handler, ctx)
    assert result.state == TaskState.planning
    tracer.span.assert_not_called()


# --- LogContextMiddleware tests ---


@pytest.mark.asyncio
async def test_log_context_middleware_sets_context():
    """Verify set_log_context called with run_id and state."""
    task = _make_task(TaskState.planning)

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.completed)

    ctx = MiddlewareContext()
    mw = LogContextMiddleware()

    with patch("horse_fish.orchestrator.middleware.set_log_context") as mock_set:
        await mw(task, handler, ctx)
        mock_set.assert_called_with(run_id=task.id)


@pytest.mark.asyncio
async def test_log_context_middleware_clears_on_executing():
    """Verify clear+reset when result state is executing."""
    task = _make_task(TaskState.planning)

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.executing)

    ctx = MiddlewareContext()
    mw = LogContextMiddleware()

    with (
        patch("horse_fish.orchestrator.middleware.set_log_context") as mock_set,
        patch("horse_fish.orchestrator.middleware.clear_log_context") as mock_clear,
    ):
        await mw(task, handler, ctx)
        mock_clear.assert_called_once()
        # Second call to set_log_context after clear should have run_id only
        assert mock_set.call_count == 2
        mock_set.assert_called_with(run_id=task.id)


# --- PersistenceMiddleware tests ---


@pytest.mark.asyncio
async def test_persistence_middleware_persists_after_handler():
    """persist_fn called with the result."""
    persist_fn = AsyncMock()
    task = _make_task()

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.planning)

    ctx = MiddlewareContext()
    mw = PersistenceMiddleware(persist_fn)
    result = await mw(task, handler, ctx)
    persist_fn.assert_awaited_once_with(result)
    assert result.state == TaskState.planning


# --- MemoryMiddleware tests ---


@pytest.mark.asyncio
async def test_memory_middleware_calls_learn_on_completed():
    """learn called when state is completed."""
    learn_fn = AsyncMock()
    task = _make_task()

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.completed)

    ctx = MiddlewareContext()
    mw = MemoryMiddleware(learn_fn)
    result = await mw(task, handler, ctx)
    learn_fn.assert_awaited_once_with(result)


@pytest.mark.asyncio
async def test_memory_middleware_skips_non_terminal():
    """learn NOT called for non-terminal states."""
    learn_fn = AsyncMock()
    task = _make_task()

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.executing)

    ctx = MiddlewareContext()
    mw = MemoryMiddleware(learn_fn)
    await mw(task, handler, ctx)
    learn_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_memory_middleware_handles_learn_failure():
    """learn raises — middleware logs warning, doesn't crash."""
    learn_fn = AsyncMock(side_effect=RuntimeError("learn exploded"))
    task = _make_task()

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.completed)

    ctx = MiddlewareContext()
    mw = MemoryMiddleware(learn_fn)
    result = await mw(task, handler, ctx)
    # Should not raise, should return result
    assert result.state == TaskState.completed
    learn_fn.assert_awaited_once()


# --- ScoutContextMiddleware tests ---


@pytest.mark.asyncio
async def test_scout_context_middleware_stores_brief():
    """brief stored in ctx after scout phase."""
    brief = MagicMock()
    get_brief_fn = MagicMock(return_value=brief)
    task = _make_task(TaskState.scouting)

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.planning)

    ctx = MiddlewareContext()
    mw = ScoutContextMiddleware(get_brief_fn)
    await mw(task, handler, ctx)
    get_brief_fn.assert_called_once()
    assert ctx.context_brief is brief


@pytest.mark.asyncio
async def test_scout_context_middleware_skips_non_scout():
    """No brief stored for non-scout states."""
    get_brief_fn = MagicMock(return_value=MagicMock())
    task = _make_task(TaskState.planning)

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.executing)

    ctx = MiddlewareContext()
    mw = ScoutContextMiddleware(get_brief_fn)
    await mw(task, handler, ctx)
    get_brief_fn.assert_not_called()
    assert ctx.context_brief is None


# --- Full integration test ---


@pytest.mark.asyncio
async def test_full_chain_integration():
    """Compose all 5 middleware, run through, verify all effects."""
    # Setup mocks
    tracer = MagicMock()
    span = MagicMock()
    tracer.span.return_value = span
    trace = MagicMock()

    persist_fn = AsyncMock()
    learn_fn = AsyncMock()
    brief = MagicMock()
    get_brief_fn = MagicMock(return_value=brief)

    task = _make_task(TaskState.scouting)

    async def handler(run: Task) -> Task:
        return _transition(run, TaskState.completed)

    ctx = MiddlewareContext(trace=trace)
    middlewares: list[Middleware] = [
        TracingMiddleware(tracer),
        LogContextMiddleware(),
        PersistenceMiddleware(persist_fn),
        MemoryMiddleware(learn_fn),
        ScoutContextMiddleware(get_brief_fn),
    ]

    with patch("horse_fish.orchestrator.middleware.set_log_context"):
        chain = compose_chain(middlewares, handler, ctx)
        result = await chain(task)

    assert result.state == TaskState.completed
    tracer.span.assert_called_once()
    tracer.end_span.assert_called_once()
    persist_fn.assert_awaited_once()
    learn_fn.assert_awaited_once()
    get_brief_fn.assert_called_once()
    assert ctx.context_brief is brief
