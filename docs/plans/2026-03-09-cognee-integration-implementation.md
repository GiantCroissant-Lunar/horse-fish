# Cognee Integration Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace orchestrator-level memvid with Cognee (FastEmbed + LanceDB + Kuzu) for knowledge-graph-based memory, keeping agent-local memvid and SQLite LessonStore unchanged.

**Architecture:** Two-tier memory. Tier 1: agents record raw results via memvid (unchanged). Tier 2: orchestrator ingests results into Cognee knowledge graph (FastEmbed embeddings, LanceDB vectors, Kuzu graph). LessonStore stays as-is for deterministic pattern matching.

**Tech Stack:** cognee, fastembed, lancedb, kuzu, litellm (Cognee's LLM router)

**Design doc:** `docs/plans/2026-03-09-cognee-integration-design.md`

---

### Task 1: Add cognee dependency

**Files:**
- Modify: `pyproject.toml:11-14`

**Step 1: Add cognee to optional deps**

In `pyproject.toml`, replace the `[project.optional-dependencies]` memory section:

```toml
[project.optional-dependencies]
memory = [
    "memvid-sdk>=0.1",
    "cognee>=0.1",
]
```

**Step 2: Install and verify import**

Run: `pip install -e ".[memory,dev]"`
Run: `python -c "import cognee; print('cognee OK')"`
Expected: `cognee OK`

**Step 3: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add cognee to memory dependencies"
```

---

### Task 2: CogneeMemory — write failing tests

**Files:**
- Create: `tests/test_cognee_memory.py`

**Step 1: Write the failing tests**

```python
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
        from horse_fish.memory.cognee_store import CogneeMemory, CogneeHit

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
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_cognee_memory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'horse_fish.memory.cognee_store'`

**Step 3: Commit**

```bash
git add tests/test_cognee_memory.py
git commit -m "test: add failing tests for CogneeMemory"
```

---

### Task 3: CogneeMemory — implement

**Files:**
- Create: `src/horse_fish/memory/cognee_store.py`
- Modify: `src/horse_fish/memory/__init__.py`

**Step 1: Implement CogneeMemory**

```python
"""Cognee-backed knowledge graph memory for orchestrator-level learning."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from horse_fish.models import Run, SubtaskResult

logger = logging.getLogger(__name__)


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
        self._llm_model = llm_model or "openai/mercury-coder-small"

        # Fallback LLM (Dashscope/qwen)
        self._fallback_llm_api_key = fallback_llm_api_key or os.environ.get("DASHSCOPE_API_KEY", "")
        self._fallback_llm_model = fallback_llm_model or "openai/qwen3.5-plus"
        self._fallback_llm_endpoint = (
            fallback_llm_endpoint or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )

    def _configure(self, *, use_fallback: bool = False) -> None:
        """Configure Cognee providers. Lazy — called on first use."""
        import cognee

        self._data_dir.mkdir(parents=True, exist_ok=True)

        cognee.config.set_classification_model(None)

        # Embedding: FastEmbed (local, CPU)
        cognee.config.set_embedding_provider("fastembed")
        cognee.config.set_embedding_model("sentence-transformers/all-MiniLM-L6-v2")

        # Vector store: LanceDB (file-based)
        cognee.config.set_vector_db_provider("lancedb")
        cognee.config.set_vector_db_url(str(self._data_dir / "lancedb"))

        # Graph store: Kuzu (file-based)
        cognee.config.set_graph_db_provider("kuzu")
        cognee.config.system_root_directory(str(self._data_dir))

        # LLM
        if use_fallback:
            cognee.config.set_llm_provider("custom")
            cognee.config.set_llm_api_key(self._fallback_llm_api_key)
            cognee.config.set_llm_model(self._fallback_llm_model)
            cognee.config.set_llm_endpoint(self._fallback_llm_endpoint)
        else:
            cognee.config.set_llm_provider("custom")
            cognee.config.set_llm_api_key(self._llm_api_key)
            cognee.config.set_llm_model(self._llm_model)
            cognee.config.set_llm_endpoint(self._llm_endpoint)

        self._configured = True

    def _ensure_configured(self) -> None:
        if not self._configured:
            self._configure()

    async def ingest(self, content: str, metadata: dict[str, Any] | None = None) -> None:
        """Add content to Cognee and build knowledge graph.

        Calls cognee.add() then cognee.cognify(). If cognify fails with
        primary LLM, retries with fallback LLM.
        """
        import cognee

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
        import cognee

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
```

**Step 2: Update `__init__.py` exports**

In `src/horse_fish/memory/__init__.py`, add CogneeMemory and CogneeHit:

```python
"""Memory module for cross-session learning."""

from horse_fish.memory.lessons import Lesson, LessonStore
from horse_fish.memory.store import MemoryHit, MemoryStore

try:
    from horse_fish.memory.cognee_store import CogneeHit, CogneeMemory
except ImportError:
    CogneeMemory = None  # type: ignore[assignment,misc]
    CogneeHit = None  # type: ignore[assignment,misc]

__all__ = ["MemoryStore", "MemoryHit", "LessonStore", "Lesson", "CogneeMemory", "CogneeHit"]
```

**Step 3: Run tests**

Run: `pytest tests/test_cognee_memory.py -v`
Expected: All tests PASS

**Step 4: Run full suite**

Run: `pytest --tb=short -q`
Expected: 304+ passed

**Step 5: Commit**

```bash
git add src/horse_fish/memory/cognee_store.py src/horse_fish/memory/__init__.py
git commit -m "feat: add CogneeMemory with FastEmbed + LanceDB + Kuzu"
```

---

### Task 4: Wire CogneeMemory into Orchestrator

**Files:**
- Modify: `src/horse_fish/orchestrator/engine.py:1-18,34-63,101-117`
- Create: `tests/test_cognee_orchestrator.py`

**Step 1: Write failing tests**

```python
"""Tests for Cognee integration in Orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from horse_fish.models import Run, Subtask, SubtaskResult, SubtaskState


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
```

**Step 2: Run tests to verify failure**

Run: `pytest tests/test_cognee_orchestrator.py -v`
Expected: FAIL — `TypeError: Orchestrator.__init__() got an unexpected keyword argument 'cognee_memory'`

**Step 3: Add `cognee_memory` parameter to Orchestrator**

In `src/horse_fish/orchestrator/engine.py`, add the import at top:

```python
from horse_fish.memory.cognee_store import CogneeMemory
```

Add `cognee_memory` parameter to `__init__`:

```python
def __init__(
    self,
    pool: AgentPool,
    planner: Planner,
    gates: ValidationGates,
    runtime: str = "claude",
    model: str | None = None,
    max_agents: int = 3,
    selector: AgentSelector | None = None,
    merge_queue: MergeQueue | None = None,
    tracer: Tracer | None = None,
    memory: MemoryStore | None = None,
    lesson_store: LessonStore | None = None,
    cognee_memory: CogneeMemory | None = None,
    stall_timeout_seconds: int = STALL_TIMEOUT_SECONDS,
    concurrency_limits: dict[RunState, int] | None = None,
) -> None:
```

Add `self._cognee_memory = cognee_memory` in the body.

**Step 4: Update `_learn()` to use CogneeMemory**

Replace the `_learn` method:

```python
async def _learn(self, run: Run) -> None:
    """Store completed run results in memory for future learning."""
    subtask_results = [s.result for s in run.subtasks if s.result]

    # Tier 1: memvid (agent-local, backward compat)
    if self._memory:
        try:
            await self._memory.store_run_result(run, subtask_results)
        except Exception as exc:
            logger.warning("Failed to store run in memvid: %s", exc)

    # Tier 2: Cognee knowledge graph
    if self._cognee_memory:
        try:
            await self._cognee_memory.ingest_run_result(run, subtask_results)
        except Exception as exc:
            logger.warning("Failed to ingest run into Cognee: %s", exc)

    # Lessons (deterministic pattern extraction)
    if self._lesson_store:
        try:
            lessons = self._lesson_store.extract_lessons(run)
            for lesson in lessons:
                self._lesson_store.store_lesson(lesson)
            run.lessons = [lesson.id for lesson in lessons]
        except Exception as exc:
            logger.warning("Failed to extract lessons: %s", exc)
```

**Step 5: Handle optional import**

Since cognee is optional, use a conditional import at the top of `engine.py`:

```python
try:
    from horse_fish.memory.cognee_store import CogneeMemory
except ImportError:
    CogneeMemory = None  # type: ignore[assignment,misc]
```

Use `CogneeMemory | None` type annotation as a string `"CogneeMemory | None"` or just `Any` if needed.

**Step 6: Run tests**

Run: `pytest tests/test_cognee_orchestrator.py -v`
Expected: PASS

Run: `pytest --tb=short -q`
Expected: 304+ passed (existing tests unaffected)

**Step 7: Commit**

```bash
git add src/horse_fish/orchestrator/engine.py tests/test_cognee_orchestrator.py
git commit -m "feat: wire CogneeMemory into orchestrator._learn()"
```

---

### Task 5: Wire CogneeMemory into CLI

**Files:**
- Modify: `src/horse_fish/cli.py:10-49`

**Step 1: Write failing test**

Add to `tests/test_cognee_orchestrator.py`:

```python
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
```

**Step 2: Update CLI**

In `src/horse_fish/cli.py`, add the import:

```python
try:
    from horse_fish.memory.cognee_store import CogneeMemory
except ImportError:
    CogneeMemory = None
```

Update `_init_components`:

```python
def _init_components(runtime: str, model: str | None, max_agents: int):
    """Initialize all components needed for orchestration."""
    repo_root = str(Path.cwd())
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    store = Store(DB_PATH)
    store.migrate()
    tmux = TmuxManager()
    worktrees = WorktreeManager(repo_root)
    claude_md = Path.cwd() / "CLAUDE.md"
    project_context = claude_md.read_text() if claude_md.exists() else None
    pool = AgentPool(store, tmux, worktrees, project_context=project_context)
    planner = Planner(runtime=runtime, model=model)
    gates = ValidationGates()
    memory = MemoryStore()
    lesson_store = LessonStore(store)
    cognee_memory = CogneeMemory() if CogneeMemory else None
    orchestrator = Orchestrator(
        pool=pool,
        planner=planner,
        gates=gates,
        runtime=runtime,
        model=model or "",
        max_agents=max_agents,
        memory=memory,
        lesson_store=lesson_store,
        cognee_memory=cognee_memory,
    )
    return orchestrator, store, pool
```

**Step 3: Run tests**

Run: `pytest tests/test_cognee_orchestrator.py -v`
Expected: PASS

Run: `pytest --tb=short -q`
Expected: 304+ passed

**Step 4: Commit**

```bash
git add src/horse_fish/cli.py
git commit -m "feat: wire CogneeMemory into CLI init"
```

---

### Task 6: Wire Cognee search into SmartPlanner

**Files:**
- Modify: `src/horse_fish/planner/smart.py:1-10,43-53,119-131`
- Add to: `tests/test_cognee_orchestrator.py`

**Step 1: Write failing test**

Add to `tests/test_cognee_orchestrator.py`:

```python
class TestSmartPlannerCogneeSearch:
    """Test that SmartPlanner uses Cognee for semantic context."""

    @pytest.mark.asyncio
    async def test_decompose_queries_cognee(self):
        from horse_fish.planner.smart import SmartPlanner

        mock_planner = MagicMock()
        mock_cognee = AsyncMock()
        mock_cognee.find_similar_tasks = AsyncMock(return_value=[])

        smart = SmartPlanner(
            planner=mock_planner,
            cognee_memory=mock_cognee,
        )

        # Mock _classify to return SOLO
        smart._classify = AsyncMock(return_value="solo")

        from horse_fish.models import TaskComplexity

        smart._classify = AsyncMock(return_value=TaskComplexity.solo)

        subtasks, complexity = await smart.decompose("fix auth bug")

        mock_cognee.find_similar_tasks.assert_awaited_once_with("fix auth bug")
```

**Step 2: Add `cognee_memory` to SmartPlanner**

In `src/horse_fish/planner/smart.py`, update `__init__`:

```python
def __init__(
    self,
    planner: Planner,
    lesson_store: LessonStore | None = None,
    cognee_memory: Any | None = None,
) -> None:
    self._planner = planner
    self._lessons = lesson_store
    self._cognee = cognee_memory
```

Add `from typing import Any` to imports.

**Step 3: Add Cognee context to decompose**

Update the `decompose` method to query Cognee for context:

```python
async def decompose(self, task: str, context: str = "") -> tuple[list[Subtask], TaskComplexity]:
    # 1. Query lessons (deterministic)
    lessons_text = self._get_lessons(task)

    # 2. Query Cognee for semantic context
    cognee_context = await self._get_cognee_context(task)
    if cognee_context:
        context = f"{context}\n\nPast similar work:\n{cognee_context}" if context else f"Past similar work:\n{cognee_context}"

    # ... rest unchanged
```

Add the helper method:

```python
async def _get_cognee_context(self, task: str) -> str:
    """Retrieve relevant context from Cognee knowledge graph."""
    if not self._cognee:
        return ""
    try:
        hits = await self._cognee.find_similar_tasks(task)
        if not hits:
            return ""
        return "\n".join(f"- {hit.content}" for hit in hits[:3])
    except Exception as exc:
        logger.warning("Failed to query Cognee: %s", exc)
        return ""
```

**Step 4: Update Orchestrator SmartPlanner creation**

In `engine.py`, update the SmartPlanner creation line in `__init__`:

```python
self._smart_planner = (
    SmartPlanner(planner, lesson_store=lesson_store, cognee_memory=cognee_memory)
    if lesson_store or cognee_memory
    else None
)
```

**Step 5: Run tests**

Run: `pytest tests/test_cognee_orchestrator.py -v`
Expected: PASS

Run: `pytest --tb=short -q`
Expected: 304+ passed

**Step 6: Commit**

```bash
git add src/horse_fish/planner/smart.py src/horse_fish/orchestrator/engine.py tests/test_cognee_orchestrator.py
git commit -m "feat: wire Cognee search into SmartPlanner for semantic context"
```

---

### Task 7: Lint + final test run

**Files:** None new

**Step 1: Lint**

Run: `ruff check src/ tests/ --fix`
Run: `ruff format src/ tests/`

**Step 2: Full test suite**

Run: `pytest --tb=short -q`
Expected: 304+ passed, 0 failures

**Step 3: Commit any lint fixes**

```bash
git add -u
git commit -m "style: lint fixes for cognee integration"
```

---

## Swarm Execution Plan (Overstory)

These tasks can be parallelized for agent swarm execution:

| Agent | Tasks | Runtime | Rationale |
|-------|-------|---------|-----------|
| claude-cognee | Task 1, 2, 3 | claude | Core CogneeMemory — needs careful API design |
| pi-wiring | Task 4, 5 | pi | Mechanical wiring — add param, update calls |
| claude-smart | Task 6 | claude | SmartPlanner Cognee integration — needs context |

Task 7 (lint) runs after all merges.

**Dependency chain:** Tasks 4-6 depend on Task 3 (CogneeMemory class must exist first). Tasks 1-3 can run in one agent. Tasks 4-5 and Task 6 can run in parallel after merge of Tasks 1-3.
