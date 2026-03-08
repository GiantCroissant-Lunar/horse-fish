"""Cognee-backed knowledge graph memory for orchestrator-level learning."""

from __future__ import annotations

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
            self._configure()

    async def ingest(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        """Add content to Cognee and build knowledge graph.

        Calls cognee.add() then cognee.cognify(). If cognify fails with
        primary LLM, retries with fallback LLM.
        """
        self._ensure_configured()

        await cognee.add(content)

        try:
            await cognee.cognify()
        except Exception as exc:
            logger.warning("cognify failed with primary LLM: %s — trying fallback", exc)
            self._configure(use_fallback=True)
            await cognee.cognify()

    async def search(self, query: str, top_k: int = 5) -> list[CogneeHit]:
        """Search the Cognee knowledge graph."""
        self._ensure_configured()

        results = await cognee.search(query_text=query)

        hits: list[CogneeHit] = []
        for result in results[:top_k]:
            hits.append(
                CogneeHit(
                    node_id=getattr(result, "id", ""),
                    content=getattr(result, "text", str(result)),
                    score=getattr(result, "score", 0.0),
                    metadata=getattr(result, "metadata", {}),
                )
            )
        return hits

    async def ingest_run_result(self, run: Run, subtask_results: list[SubtaskResult]) -> None:
        """Ingest a completed run into the knowledge graph."""
        parts = [
            f"Task: {run.task}",
            f"State: {run.state}",
            f"Subtasks: {len(run.subtasks)}",
            "",
        ]

        for result in subtask_results:
            parts.append(f"Subtask {result.subtask_id}:")
            parts.append(f"  Success: {result.success}")
            parts.append(f"  Output: {result.output}")
            if result.diff:
                parts.append(f"  Diff: {result.diff}")
            parts.append("")

        content = "\n".join(parts)
        await self.ingest(content, {"type": "run_result", "run_id": run.id})

    async def find_similar_tasks(self, task_description: str, top_k: int = 3) -> list[CogneeHit]:
        """Find past tasks similar to a new one via knowledge graph search."""
        return await self.search(task_description, top_k=top_k)
