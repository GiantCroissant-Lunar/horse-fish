"""Tests for Cognee integration in Orchestrator, CLI, and SmartPlanner."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from horse_fish.models import Run, Subtask, SubtaskResult


class TestOrchestratorLearnPhase:
    """Test that orchestrator._learn() writes to memvid + SQLite entries."""

    @pytest.mark.asyncio
    async def test_learn_stores_entries_for_cognee_ingestion(self):
        """_learn() should call store_entry for each subtask result."""
        from horse_fish.orchestrator.engine import Orchestrator

        mock_memvid = AsyncMock()
        mock_memvid.store_run_result = AsyncMock()
        mock_memvid.store_entry = MagicMock(return_value="entry-1")

        orch = Orchestrator(
            pool=MagicMock(),
            planner=MagicMock(),
            gates=MagicMock(),
            memory=mock_memvid,
        )

        run = Run.create(task="test task")
        run.state = "completed"
        run.completed_at = datetime.now(UTC)
        run.subtasks = [Subtask.create("sub1")]
        run.subtasks[0].result = SubtaskResult(
            subtask_id="s1", success=True, output="done", diff="diff", duration_seconds=1.0
        )

        await orch._learn(run)

        # store_run_result for memvid backward compat
        mock_memvid.store_run_result.assert_awaited_once()
        # store_entry for new Cognee batch ingestion path
        mock_memvid.store_entry.assert_called_once()
        call_kwargs = mock_memvid.store_entry.call_args
        assert call_kwargs[1]["domain"] == "run_result"
        assert call_kwargs[1]["run_id"] == run.id

    @pytest.mark.asyncio
    async def test_learn_no_cognee_direct_ingestion(self):
        """_learn() should NOT call cognee.ingest_run_result directly."""
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
        run.subtasks = []

        await orch._learn(run)

        # Cognee should NOT be called during learn — ingestion is now batch via hf memory organize
        mock_cognee.ingest_run_result.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_learn_store_entry_failure_does_not_crash(self):
        """store_entry failure should be logged, not crash."""
        from horse_fish.orchestrator.engine import Orchestrator

        mock_memvid = AsyncMock()
        mock_memvid.store_run_result = AsyncMock()
        mock_memvid.store_entry = MagicMock(side_effect=RuntimeError("sqlite error"))

        orch = Orchestrator(
            pool=MagicMock(),
            planner=MagicMock(),
            gates=MagicMock(),
            memory=mock_memvid,
        )

        run = Run.create(task="test task")
        run.state = "completed"
        run.completed_at = datetime.now(UTC)
        run.subtasks = [Subtask.create("sub1")]
        run.subtasks[0].result = SubtaskResult(
            subtask_id="s1", success=True, output="done", diff="", duration_seconds=1.0
        )

        # Should not raise
        await orch._learn(run)


class TestCLICogneeWiring:
    """Test that CLI creates CogneeMemory."""

    def test_init_components_creates_cognee_memory(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".horse-fish").mkdir()

        # Mock CogneeMemory to avoid real cognee import
        mock_class = MagicMock()
        monkeypatch.setattr("horse_fish.cli.CogneeMemory", mock_class)
        # CogneeMemory requires an LLM key to be present
        monkeypatch.setenv("INCEPTION_API_KEY", "test-key")  # pragma: allowlist secret

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
