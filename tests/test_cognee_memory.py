"""Tests for CogneeMemory — Cognee-backed knowledge graph memory."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horse_fish.models import Run, Subtask, SubtaskResult


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
    async def test_ingest_run_result_formats_content(self, tmp_path):
        from horse_fish.memory.cognee_store import CogneeMemory

        mem = CogneeMemory(data_dir=tmp_path / "cognee")

        run = Run.create(task="Fix the login bug")
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

            # Should call add with formatted text
            call_args = mock_cognee.add.call_args
            text = call_args[0][0]
            assert "Fix the login bug" in text
            assert "Fixed null check" in text


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
