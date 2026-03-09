"""Cognee-backed knowledge graph memory for orchestrator-level learning."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from horse_fish.models import Run, SubtaskResult

logger = logging.getLogger(__name__)

# Module-level import so tests can patch "horse_fish.memory.cognee_store.cognee"
try:
    import cognee
except Exception:
    cognee = None  # type: ignore[assignment]

try:
    from cognee.api.v1.search import SearchType
except Exception:
    SearchType = None  # type: ignore[assignment]


class CogneeHit(BaseModel):
    """A search result from Cognee knowledge graph."""

    node_id: str
    content: str
    score: float
    metadata: dict[str, Any]


class CogneeMemory:
    """Orchestrator-level memory using Cognee knowledge graph.

    Uses FastEmbed (CPU embeddings), LanceDB (vector store), and Kuzu (graph store).
    LLM fallback chain: Mercury 2 → Dashscope (qwen3.5-plus).
    """

    def __init__(
        self,
        data_dir: Path | str | None = None,
        llm_api_key: str | None = None,
        llm_endpoint: str | None = None,
        llm_model: str | None = None,
        fallback_llm_api_key: str | None = None,
        fallback_llm_model: str | None = None,
        fallback_llm_endpoint: str | None = None,
    ) -> None:
        if data_dir is None:
            data_dir = Path.home() / ".horse-fish" / "cognee"
        else:
            data_dir = Path(data_dir)

        self._data_dir = data_dir
        self._configured = False

        # Primary LLM (Mercury 2)
        self._llm_api_key = llm_api_key or os.environ.get("INCEPTION_API_KEY", "")
        self._llm_endpoint = llm_endpoint or "https://api.inceptionlabs.ai/v1"
        self._llm_model = llm_model or "openai/mercury-2"

        # Fallback LLM (Dashscope/qwen)
        self._fallback_llm_api_key = fallback_llm_api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        self._fallback_llm_model = fallback_llm_model or "openai/qwen3.5-plus"
        self._fallback_llm_endpoint = fallback_llm_endpoint or "https://dashscope.aliyuncs.com/compatible-mode/v1"

    def _configure(self, *, use_fallback: bool = False) -> None:
        """Configure Cognee providers. Lazy — called on first use."""
        self._data_dir.mkdir(parents=True, exist_ok=True)

        # Embedding: FastEmbed (local, CPU) — configured via env vars because
        # cognee.config has no set_embedding_provider/set_embedding_model methods
        os.environ.setdefault("EMBEDDING_PROVIDER", "fastembed")
        os.environ.setdefault("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
        os.environ.setdefault("EMBEDDING_DIMENSIONS", "384")

        # Skip connection test — cognee has a bug where the custom provider
        # endpoint is not passed to the GenericAPIAdapter in test_llm_connection
        os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"

        # Vector store: LanceDB (file-based)
        cognee.config.set_vector_db_provider("lancedb")
        cognee.config.set_vector_db_url(str(self._data_dir / "lancedb"))

        # Graph store: Kuzu (file-based)
        cognee.config.set_graph_database_provider("kuzu")
        cognee.config.system_root_directory(str(self._data_dir))

        # LLM — use set_llm_config dict for full control including endpoint
        if use_fallback:
            cognee.config.set_llm_config(
                {
                    "llm_provider": "custom",
                    "llm_api_key": self._fallback_llm_api_key,
                    "llm_model": self._fallback_llm_model,
                    "llm_endpoint": self._fallback_llm_endpoint,
                }
            )
        else:
            cognee.config.set_llm_config(
                {
                    "llm_provider": "custom",
                    "llm_api_key": self._llm_api_key,
                    "llm_model": self._llm_model,
                    "llm_endpoint": self._llm_endpoint,
                }
            )

        # Monkey-patch: fix cognee bug where custom provider endpoint is not
        # passed to GenericAPIAdapter in get_llm_client()
        self._patch_custom_endpoint()

        self._configured = True

    @staticmethod
    def _patch_custom_endpoint() -> None:
        """Patch cognee's get_llm_client to pass endpoint for custom provider."""
        try:
            from cognee.infrastructure.llm.structured_output_framework.litellm_instructor.llm import (
                get_llm_client as client_module,
            )

            _original = client_module.get_llm_client

            def _patched(raise_api_key_error: bool = True):
                client = _original(raise_api_key_error)
                # If endpoint wasn't set on the adapter, inject it from config
                if hasattr(client, "endpoint") and not client.endpoint:
                    from cognee.infrastructure.llm import get_llm_config

                    cfg = get_llm_config()
                    if cfg.llm_endpoint:
                        client.endpoint = cfg.llm_endpoint
                return client

            client_module.get_llm_client = _patched
        except Exception:
            pass

    def _ensure_configured(self) -> None:
        if not self._configured:
            use_fallback = not self._llm_api_key and bool(self._fallback_llm_api_key)
            self._configure(use_fallback=use_fallback)

    async def ingest(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        """Add content to Cognee and build knowledge graph.

        Calls cognee.add() then cognee.cognify(). If cognify fails with
        primary LLM, retries with fallback LLM.

        Uses dataset_name from metadata (default: "general") and
        temporal_cognify=True for time-aware graph construction.
        """
        self._ensure_configured()

        dataset = (metadata or {}).get("dataset", "general")
        await cognee.add(content, dataset_name=dataset)

        try:
            await cognee.cognify(datasets=[dataset], temporal_cognify=True)
        except Exception as exc:
            logger.warning("cognify failed with primary LLM: %s — trying fallback", exc)
            self._configure(use_fallback=True)
            await cognee.cognify(datasets=[dataset], temporal_cognify=True)

    @staticmethod
    def _parse_result(result: Any) -> CogneeHit:
        """Parse a single Cognee search result into a CogneeHit.

        GRAPH_COMPLETION can return str, dict, or object — handle all formats.
        """
        if isinstance(result, str):
            return CogneeHit(node_id="", content=result, score=1.0, metadata={})
        if isinstance(result, dict):
            return CogneeHit(
                node_id=result.get("id", ""),
                content=result.get("text", result.get("content", str(result))),
                score=result.get("score", 1.0),
                metadata=result.get("metadata", {}),
            )
        return CogneeHit(
            node_id=getattr(result, "id", ""),
            content=getattr(result, "text", getattr(result, "content", str(result))),
            score=getattr(result, "score", 1.0),
            metadata=getattr(result, "metadata", {}),
        )

    async def _search_cognee(
        self, query: str, top_k: int = 5, timeout: float = 60.0, **extra_kwargs: Any
    ) -> list[CogneeHit]:
        """Shared search implementation with GRAPH_COMPLETION."""
        self._ensure_configured()

        kwargs: dict[str, Any] = {"query_text": query, **extra_kwargs}
        if SearchType:
            kwargs["query_type"] = SearchType.GRAPH_COMPLETION

        results = await asyncio.wait_for(cognee.search(**kwargs), timeout=timeout)
        return [self._parse_result(r) for r in results[:top_k]]

    async def search(self, query: str, top_k: int = 5) -> list[CogneeHit]:
        """Search the Cognee knowledge graph using GRAPH_COMPLETION."""
        return await self._search_cognee(query, top_k=top_k)

    async def ingest_run_result(self, run: Run, subtask_results: list[SubtaskResult]) -> None:
        """Ingest a completed run into the knowledge graph using structured node_sets.

        Ingests: (1) task summary to task_summaries node_set, (2) subtask outcomes
        to subtask_outcomes node_set, (3) code diffs to code_diffs node_set.
        All go into the "run_results" dataset.
        """
        self._ensure_configured()

        # 1. Ingest task summary
        task_summary = f"Task: {run.task}\nState: {run.state}\nSubtasks: {len(run.subtasks)}"
        await cognee.add(task_summary, dataset_name="run_results", node_set=["task_summaries"])

        # 2. Ingest each subtask result separately
        for result in subtask_results:
            subtask_content = f"Subtask {result.subtask_id}:\n  Success: {result.success}\n  Output: {result.output}"
            await cognee.add(subtask_content, dataset_name="run_results", node_set=["subtask_outcomes"])

            # 3. Ingest diffs separately (code patterns)
            if result.diff:
                await cognee.add(result.diff, dataset_name="run_results", node_set=["code_diffs"])

        # 4. Cognify all at once
        try:
            await cognee.cognify(datasets=["run_results"], temporal_cognify=True)
        except Exception as exc:
            logger.warning("cognify failed with primary LLM: %s — trying fallback", exc)
            self._configure(use_fallback=True)
            await cognee.cognify(datasets=["run_results"], temporal_cognify=True)

    async def find_similar_tasks(self, task_description: str, top_k: int = 3) -> list[CogneeHit]:
        """Find past tasks similar to a new one via knowledge graph search.

        Searches only the "run_results" dataset for relevant past work.
        """
        return await self._search_cognee(task_description, top_k=top_k, datasets=["run_results"])

    async def batch_ingest(self, entries: list) -> int:
        """Batch ingest memory entries into Cognee knowledge graph.

        Args:
            entries: List of MemoryEntry objects (from horse_fish.memory.store).
                Each has: id, content, agent, run_id, domain, tags, timestamp

        Returns:
            Number of successfully ingested entries.
        """
        self._ensure_configured()

        if not entries:
            return 0

        # Group entries by domain
        by_domain: dict[str, list] = {}
        for entry in entries:
            domain = entry.domain
            if domain not in by_domain:
                by_domain[domain] = []
            by_domain[domain].append(entry)

        ingested_count = 0

        for domain, domain_entries in by_domain.items():
            try:
                # Add all entries for this domain
                for entry in domain_entries:
                    await cognee.add(entry.content, dataset_name=domain, node_set=[domain])

                # Cognify once per domain
                try:
                    await cognee.cognify(datasets=[domain], temporal_cognify=True)
                except Exception as exc:
                    logger.warning("cognify failed with primary LLM for domain %s: %s — trying fallback", domain, exc)
                    self._configure(use_fallback=True)
                    await cognee.cognify(datasets=[domain], temporal_cognify=True)

                ingested_count += len(domain_entries)

            except Exception as exc:
                logger.warning("Failed to ingest entries for domain %s: %s", domain, exc)

        return ingested_count
