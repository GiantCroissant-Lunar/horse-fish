"""Tests for CogneeMemory — Cognee-backed knowledge graph memory."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horse_fish.models import Subtask, SubtaskResult, Task


class TestCogneeSearchType:
    """Tests that search uses GRAPH_COMPLETION."""

    @pytest.mark.asyncio
    async def test_search_uses_graph_completion(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.search = AsyncMock(return_value=[])
            mock_cognee.config = MagicMock()
            # Mock SearchType import
            mock_search_type = MagicMock()
            with patch("horse_fish.memory.cognee_store.SearchType", mock_search_type):
                await mem.search("test query")

            # Verify GRAPH_COMPLETION was used
            call_kwargs = mock_cognee.search.call_args
            assert call_kwargs is not None
            assert call_kwargs.kwargs.get("query_type") == mock_search_type.GRAPH_COMPLETION


class TestCogneeDatasets:
    """Tests that ingestion uses datasets and node_sets."""

    @pytest.mark.asyncio
    async def test_ingest_uses_dataset_name(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.add = AsyncMock()
            mock_cognee.cognify = AsyncMock()
            mock_cognee.config = MagicMock()

            await mem.ingest("test content", {"type": "run_result"})

            # Should pass dataset_name to cognee.add
            call_args = mock_cognee.add.call_args
            assert call_args.kwargs.get("dataset_name") == "general"

    @pytest.mark.asyncio
    async def test_ingest_uses_custom_dataset(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.add = AsyncMock()
            mock_cognee.cognify = AsyncMock()
            mock_cognee.config = MagicMock()

            await mem.ingest("test content", {"dataset": "custom_ds"})

            call_args = mock_cognee.add.call_args
            assert call_args.kwargs.get("dataset_name") == "custom_ds"


class TestCogneeTemporalCognify:
    """Tests that cognify uses temporal mode."""

    @pytest.mark.asyncio
    async def test_cognify_uses_temporal(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.add = AsyncMock()
            mock_cognee.cognify = AsyncMock()
            mock_cognee.config = MagicMock()

            await mem.ingest("test content")

            call_kwargs = mock_cognee.cognify.call_args
            assert call_kwargs.kwargs.get("temporal_cognify") is True


class TestCogneeStructuredIngestion:
    """Tests for structured run result ingestion."""

    @pytest.mark.asyncio
    async def test_ingest_run_result_uses_node_sets(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")
        run = Task.create(task="Fix auth bug")
        run.state = "completed"
        results = [
            SubtaskResult(
                subtask_id="st-1",
                success=True,
                output="Fixed null check",
                diff="diff --git ...",
                duration_seconds=30.0,
            ),
        ]

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.add = AsyncMock()
            mock_cognee.cognify = AsyncMock()
            mock_cognee.config = MagicMock()

            await mem.ingest_run_result(run, results)

            # Should call add multiple times with different node_sets
            assert mock_cognee.add.await_count >= 3
            # Check for task_summaries node_set
            calls = mock_cognee.add.call_args_list
            node_sets = [call.kwargs.get("node_set") for call in calls]
            assert ["task_summaries"] in node_sets
            assert ["subtask_outcomes"] in node_sets
            assert ["code_diffs"] in node_sets


class TestCogneeMemoryInit:
    """Tests for CogneeMemory initialization and config."""

    @pytest.mark.asyncio
    async def test_init_sets_data_dir(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")
        assert mem._data_dir == tmp_path / "cognee"

    @pytest.mark.asyncio
    async def test_init_default_data_dir(self):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory()
        assert "cognee" in str(mem._data_dir)


class TestCogneeMemoryIngest:
    """Tests for ingesting content into Cognee."""

    @pytest.mark.asyncio
    async def test_ingest_calls_cognee_add_and_cognify(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")

        with (
            patch("horse_fish.memory.cognee_store.cognee") as mock_cognee,
        ):
            mock_cognee.add = AsyncMock()
            mock_cognee.cognify = AsyncMock()
            mock_cognee.config = MagicMock()

            await mem.ingest("test content", {"type": "run_result"})

            mock_cognee.add.assert_awaited_once()
            mock_cognee.cognify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ingest_run_result_structured_content(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")

        run = Task.create(task="Fix the login bug")
        run.subtasks = [Subtask.create("Patch auth.py")]
        run.state = "completed"
        run.completed_at = datetime.now(UTC)

        results = [
            SubtaskResult(
                subtask_id="st-1",
                success=True,
                output="Fixed null check in auth.py",
                diff="diff --git a/auth.py ...",
                duration_seconds=30.0,
            ),
        ]

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.add = AsyncMock()
            mock_cognee.cognify = AsyncMock()
            mock_cognee.config = MagicMock()

            await mem.ingest_run_result(run, results)

            # Should call add multiple times with structured node_sets
            calls = mock_cognee.add.call_args_list
            assert len(calls) >= 3  # task summary + subtask + diff

            # Check task summary contains task name
            task_summary_call = calls[0]
            assert "Fix the login bug" in task_summary_call.args[0]
            assert task_summary_call.kwargs.get("node_set") == ["task_summaries"]

            # Check subtask outcome
            subtask_call = calls[1]
            assert "Fixed null check" in subtask_call.args[0]
            assert subtask_call.kwargs.get("node_set") == ["subtask_outcomes"]

            # Check diff
            diff_call = calls[2]
            assert "diff --git" in diff_call.args[0]
            assert diff_call.kwargs.get("node_set") == ["code_diffs"]


class TestCogneeMemorySearch:
    """Tests for searching Cognee knowledge graph."""

    @pytest.mark.asyncio
    async def test_search_returns_memory_hits(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeHit, CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")

        mock_result = MagicMock()
        mock_result.id = "node-1"
        mock_result.text = "Fix the login bug"
        mock_result.score = 0.92
        mock_result.metadata = {"type": "run_result"}

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.search = AsyncMock(return_value=[mock_result])
            mock_cognee.config = MagicMock()

            hits = await mem.search("login bug")

            assert len(hits) >= 1
            assert isinstance(hits[0], CogneeHit)
            assert "login" in hits[0].content.lower() or hits[0].score > 0

    @pytest.mark.asyncio
    async def test_search_empty_results(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.search = AsyncMock(return_value=[])
            mock_cognee.config = MagicMock()

            hits = await mem.search("nonexistent topic")
            assert hits == []


class TestCogneeMemoryFallback:
    """Tests for LLM fallback chain."""

    @pytest.mark.asyncio
    async def test_cognify_failure_triggers_fallback(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(
            data_dir=tmp_path / "cognee",
            llm_api_key="test-key",
            llm_endpoint="https://api.inceptionlabs.ai/v1",
            llm_model="openai/mercury-coder-small",
            fallback_llm_api_key="dashscope-key",
            fallback_llm_model="openai/qwen3.5-plus",
            fallback_llm_endpoint="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )

        call_count = 0

        async def failing_cognify_then_succeed(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Mercury 2 failed")
            return None

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.add = AsyncMock()
            mock_cognee.cognify = AsyncMock(side_effect=failing_cognify_then_succeed)
            mock_cognee.config = MagicMock()

            await mem.ingest("test content", {})

            # Should have tried cognify twice (primary + fallback)
            assert mock_cognee.cognify.await_count == 2
