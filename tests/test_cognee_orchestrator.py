"""Tests for Cognee integration in Orchestrator, CLI, and SmartPlanner."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.models import Run, Subtask, SubtaskResult


class TestOrchestratorCogneeLearning:
    """Test that orchestrator._learn() uses CogneeMemory."""

    @pytest.mark.asyncio
    async def test_learn_calls_cognee_ingest(self):
        from horse_fish.orchestrator.engine import Orchestrator

        mock_cognee = AsyncMock()
        mock_cognee.ingest_run_result = AsyncMock()

        orch = Orchestrator(
            pool=MagicMock(),
            planner=MagicMock(),
            gates=MagicMock(),
            cognee_memory=mock_cognee,
        )

        run = Run.create(task="test task")
        run.state = "completed"
        run.completed_at = datetime.now(UTC)
        run.subtasks = [Subtask.create("sub1")]
        run.subtasks[0].result = SubtaskResult(
            subtask_id="s1", success=True, output="done", diff="diff", duration_seconds=1.0
        )

        await orch._learn(run)

        mock_cognee.ingest_run_result.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_learn_cognee_failure_does_not_crash(self):
        from horse_fish.orchestrator.engine import Orchestrator

        mock_cognee = AsyncMock()
        mock_cognee.ingest_run_result = AsyncMock(side_effect=RuntimeError("cognee down"))

        orch = Orchestrator(
            pool=MagicMock(),
            planner=MagicMock(),
            gates=MagicMock(),
            cognee_memory=mock_cognee,
        )

        run = Run.create(task="test task")
        run.state = "completed"
        run.completed_at = datetime.now(UTC)
        run.subtasks = []

        # Should not raise
        await orch._learn(run)

    @pytest.mark.asyncio
    async def test_learn_with_both_cognee_and_memvid(self):
        """Both memory systems are called when present."""
        from horse_fish.orchestrator.engine import Orchestrator

        mock_cognee = AsyncMock()
        mock_cognee.ingest_run_result = AsyncMock()

        mock_memvid = AsyncMock()
        mock_memvid.store_run_result = AsyncMock()

        orch = Orchestrator(
            pool=MagicMock(),
            planner=MagicMock(),
            gates=MagicMock(),
            memory=mock_memvid,
            cognee_memory=mock_cognee,
        )

        run = Run.create(task="test task")
        run.state = "completed"
        run.completed_at = datetime.now(UTC)
        run.subtasks = [Subtask.create("sub1")]
        run.subtasks[0].result = SubtaskResult(
            subtask_id="s1", success=True, output="done", diff="", duration_seconds=1.0
        )

        await orch._learn(run)

        mock_cognee.ingest_run_result.assert_awaited_once()
        mock_memvid.store_run_result.assert_awaited_once()


class TestCLICogneeWiring:
    """Test that CLI creates CogneeMemory."""

    def test_init_components_creates_cognee_memory(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".horse-fish").mkdir()

        # Mock CogneeMemory to avoid real cognee import
        mock_class = MagicMock()
        monkeypatch.setattr("horse_fish.cli.CogneeMemory", mock_class)

        from horse_fish.cli import _init_components

        orch, store, pool = _init_components("claude", None, 3)
        mock_class.assert_called_once()


class TestSmartPlannerCogneeSearch:
    """Test that SmartPlanner uses Cognee for semantic context."""

    @pytest.mark.asyncio
    async def test_decompose_queries_cognee(self):
        from horse_fish.models import TaskComplexity
        from horse_fish.planner.smart import SmartPlanner

        mock_planner = MagicMock()
        mock_cognee = AsyncMock()
        mock_cognee.find_similar_tasks = AsyncMock(return_value=[])

        smart = SmartPlanner(
            planner=mock_planner,
            cognee_memory=mock_cognee,
        )

        smart._classify = AsyncMock(return_value=TaskComplexity.solo)

        subtasks, complexity = await smart.decompose("fix auth bug")

        mock_cognee.find_similar_tasks.assert_awaited_once_with("fix auth bug")

    @pytest.mark.asyncio
    async def test_decompose_cognee_context_injected(self):
        """When cognee returns hits, context is injected into decomposition."""
        from horse_fish.memory.cognee_store import CogneeHit
        from horse_fish.models import TaskComplexity
        from horse_fish.planner.smart import SmartPlanner

        mock_planner = MagicMock()
        mock_planner.decompose = AsyncMock(return_value=[Subtask.create("step 1"), Subtask.create("step 2")])

        hit = CogneeHit(node_id="n1", content="Previous auth fix: patched null check", score=0.9, metadata={})
        mock_cognee = AsyncMock()
        mock_cognee.find_similar_tasks = AsyncMock(return_value=[hit])

        smart = SmartPlanner(planner=mock_planner, cognee_memory=mock_cognee)
        smart._classify = AsyncMock(return_value=TaskComplexity.trio)

        subtasks, complexity = await smart.decompose("fix auth bug")

        # decompose was called with context containing past work
        call_args = mock_planner.decompose.call_args
        context_arg = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("context", "")
        assert "Past similar work" in context_arg
        assert "patched null check" in context_arg

    @pytest.mark.asyncio
    async def test_decompose_cognee_failure_does_not_crash(self):
        """If Cognee search fails, decompose continues without context."""
        from horse_fish.models import TaskComplexity
        from horse_fish.planner.smart import SmartPlanner

        mock_planner = MagicMock()
        mock_cognee = AsyncMock()
        mock_cognee.find_similar_tasks = AsyncMock(side_effect=RuntimeError("cognee unavailable"))

        smart = SmartPlanner(planner=mock_planner, cognee_memory=mock_cognee)
        smart._classify = AsyncMock(return_value=TaskComplexity.solo)

        # Should not raise
        subtasks, complexity = await smart.decompose("fix auth bug")
        assert len(subtasks) == 1
