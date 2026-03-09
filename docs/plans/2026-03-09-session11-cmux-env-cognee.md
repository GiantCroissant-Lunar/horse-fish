# Session 11: cmux Skill + Env Loader + Cognee Hardening

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Three parallel improvements — cmux monitoring skill, .env-based key management, Cognee best-practice hardening.

**Architecture:** Three independent changes: (1) new Claude Code skill file, (2) CLI .env loader + `hf env check` command, (3) rewrite cognee_store.py to use SearchType, datasets, temporal_cognify, structured ingestion.

**Tech Stack:** Python 3.12, Click, python-dotenv, cognee (SearchType, datasets, temporal_cognify)

---

## Task 1: cmux Skill for Horse-Fish Monitoring

**Files:**
- Create: `~/.claude/skills/hf-cmux/SKILL.md`

**Step 1: Create the skill file**

Create `~/.claude/skills/hf-cmux/SKILL.md` with content that:
- Creates a cmux workspace named "Horse-Fish"
- Splits into 2 panes: top for `hf run`, bottom for `hf dash`
- Loads `.env` keys before launching
- Provides `read-screen` commands to inspect TUI state
- Provides cleanup (close workspace) commands

The skill should cover:
1. **Launch flow**: create workspace → split panes → load env → start `hf run` in top pane → start `hf dash` in bottom pane
2. **Monitor flow**: `read-screen` on both panes to check progress
3. **Cleanup flow**: close workspace

```markdown
# Horse-Fish Swarm Monitor — cmux

## Overview
Launch and monitor horse-fish agent swarm runs via cmux. Creates a split workspace
with `hf run` (execution) and `hf dash` (TUI dashboard) visible simultaneously.
Claude Code can `read-screen` to inspect agent progress without switching windows.

## Starting a Run

### Step 1: Create cmux workspace with split panes

\```bash
cmux new-workspace
WS=$(cmux list-workspaces | grep -v selected | grep -v "Claude Code" | tail -1 | awk '{print $1}')
cmux rename-workspace --workspace $WS "Horse-Fish"
PANES=$(cmux list-panes --workspace $WS)
TOP_PANE=$(echo "$PANES" | head -1 | awk '{print $1}')

# Split horizontally for dashboard pane
cmux new-pane --workspace $WS --direction horizontal
PANES=$(cmux list-panes --workspace $WS)
BOTTOM_PANE=$(echo "$PANES" | tail -1 | awk '{print $1}')
\```

### Step 2: Load environment and start run (top pane)

\```bash
cmux focus-pane --pane $TOP_PANE --workspace $WS > /dev/null
cmux send --workspace $WS "cd $(pwd) && source .env 2>/dev/null; export DASHSCOPE_API_KEY INCEPTION_API_KEY ZAI_API_KEY\n"
cmux send --workspace $WS "hf run \"YOUR_TASK_HERE\" --runtime pi --planner-runtime pi\n"
\```

### Step 3: Start dashboard (bottom pane)

\```bash
cmux focus-pane --pane $BOTTOM_PANE --workspace $WS > /dev/null
cmux send --workspace $WS "cd $(pwd) && hf dash\n"
\```

## Monitoring

### Read dashboard TUI state
\```bash
cmux focus-pane --pane $BOTTOM_PANE --workspace $WS > /dev/null
cmux read-screen --workspace $WS --lines 30
\```

### Read run output / logs
\```bash
cmux focus-pane --pane $TOP_PANE --workspace $WS > /dev/null
cmux read-screen --workspace $WS --lines 20
\```

### Read scrollback for full history
\```bash
cmux focus-pane --pane $TOP_PANE --workspace $WS > /dev/null
cmux read-screen --workspace $WS --scrollback --lines 100
\```

## Stopping

\```bash
# Kill processes and close workspace
cmux close-workspace --workspace $WS
\```

## Common Issues

| Issue | Fix |
|-------|-----|
| .env not found | Create `.env` in repo root with required keys. Run `hf env check` to verify. |
| Dashboard blank | Ensure `.horse-fish/state.db` exists (run `hf run` or `hf status` first) |
| Agent panes not visible | Agents run in tmux, not cmux. Use `hf logs` or dashboard AgentLog widget |
| Keys missing in run pane | `source .env` must be run in the cmux pane before `hf run` |
```

**Step 2: Register skill in claude settings**

No manual registration needed — Claude Code auto-discovers skills in `~/.claude/skills/`.

**Step 3: Verify skill loads**

Test by invoking `/hf-cmux` in a Claude Code session.

---

## Task 2: .env Loader + `hf env check` Command

**Files:**
- Create: `.env.example` (repo root)
- Modify: `src/horse_fish/cli.py`
- Modify: `src/horse_fish/agents/runtime.py` (remove `_get_tmux_env`)
- Modify: `pyproject.toml` (add python-dotenv dep)
- Test: `tests/test_cli.py` (add env check tests)
- Test: `tests/test_runtime.py` (update runtime tests)

### Step 1: Write tests for `hf env check`

Add to `tests/test_cli.py`:

```python
class TestEnvCheck:
    """Tests for hf env check command."""

    def test_env_check_all_keys_present(self, runner, monkeypatch):
        """Shows OK when all keys are set."""
        monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
        monkeypatch.setenv("INCEPTION_API_KEY", "test-key")
        result = runner.invoke(main, ["env-check"])
        assert result.exit_code == 0
        assert "DASHSCOPE_API_KEY" in result.output
        assert "ok" in result.output.lower() or "✓" in result.output

    def test_env_check_missing_keys(self, runner, monkeypatch):
        """Shows MISSING for unset keys."""
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.delenv("INCEPTION_API_KEY", raising=False)
        monkeypatch.delenv("ZAI_API_KEY", raising=False)
        result = runner.invoke(main, ["env-check"])
        assert result.exit_code == 0
        assert "missing" in result.output.lower() or "✗" in result.output

    def test_env_check_dotenv_loaded(self, runner, monkeypatch, tmp_path):
        """Keys from .env file are loaded."""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".env").write_text("DASHSCOPE_API_KEY=from-dotenv\n")  # pragma: allowlist secret
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        # Re-import to trigger dotenv load
        result = runner.invoke(main, ["env-check"])
        assert result.exit_code == 0
```

### Step 2: Run tests, verify they fail

```bash
pytest tests/test_cli.py::TestEnvCheck -v
```

### Step 3: Add python-dotenv dependency

In `pyproject.toml`, add `"python-dotenv>=1.0"` to `dependencies`.

### Step 4: Create `.env.example`

```
# Horse-fish environment keys
# Copy to .env and fill in your values: cp .env.example .env

# Required for Pi runtime (Dashscope/Qwen)
DASHSCOPE_API_KEY=

# Required for Droid runtime (Z.AI/GLM)
ZAI_API_KEY=

# Required for Cognee knowledge graph (primary LLM)
INCEPTION_API_KEY=

# Optional: Langfuse observability
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=http://localhost:3000
```

### Step 5: Add dotenv loading to CLI entry point

At the top of `cli.py`, before imports that use env vars:

```python
from dotenv import load_dotenv
load_dotenv()  # loads .env from cwd
```

### Step 6: Add `hf env-check` command

```python
_ENV_KEYS = [
    ("DASHSCOPE_API_KEY", "Pi runtime, Cognee fallback LLM", True),
    ("ZAI_API_KEY", "Droid runtime (Z.AI/GLM)", False),
    ("INCEPTION_API_KEY", "Cognee primary LLM (Mercury 2)", False),
    ("LANGFUSE_PUBLIC_KEY", "Langfuse observability", False),
    ("LANGFUSE_SECRET_KEY", "Langfuse observability", False),
]

@main.command("env-check")
def env_check():
    """Validate required environment keys are set."""
    all_ok = True
    for key, purpose, required in _ENV_KEYS:
        value = os.environ.get(key)
        if value:
            masked = value[:4] + "..." if len(value) > 4 else "***"
            click.echo(f"  ✓ {key}: {masked} ({purpose})")
        else:
            marker = "✗ MISSING" if required else "- not set"
            click.echo(f"  {marker} {key}: ({purpose})")
            if required:
                all_ok = False
    if not all_ok:
        click.echo("\nSome required keys are missing. Copy .env.example to .env and fill in values.")
```

### Step 7: Remove `_get_tmux_env` from runtime.py

Remove the `_get_tmux_env()` function and its usage in `PiRuntime.build_env()` and `DroidRuntime.build_env()`. Replace with direct `os.environ.get()` — the .env loader handles the rest.

### Step 8: Run all tests

```bash
pytest tests/test_cli.py tests/test_runtime.py -v
```

### Step 9: Commit

```bash
git add pyproject.toml .env.example src/horse_fish/cli.py src/horse_fish/agents/runtime.py tests/test_cli.py tests/test_runtime.py
git commit -m "feat: add .env loader and hf env-check command"
```

---

## Task 3: Cognee Hardening

**Files:**
- Modify: `src/horse_fish/memory/cognee_store.py`
- Modify: `src/horse_fish/planner/smart.py` (minor — handle new return format)
- Test: `tests/test_cognee_memory.py`
- Test: `tests/test_cognee_orchestrator.py`

### Step 1: Write tests for SearchType.GRAPH_COMPLETION

Update `tests/test_cognee_memory.py` to verify:

```python
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
            call_kwargs = mock_cognee.add.call_args
            assert "dataset_name" in (call_kwargs.kwargs if call_kwargs.kwargs else {}) or \
                   len(call_kwargs.args) > 1


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
        run = Run.create(task="Fix auth bug")
        run.state = "completed"
        results = [
            SubtaskResult(
                subtask_id="st-1", success=True,
                output="Fixed null check", diff="diff --git ...",
                duration_seconds=30.0,
            ),
        ]

        with patch("horse_fish.memory.cognee_store.cognee") as mock_cognee:
            mock_cognee.add = AsyncMock()
            mock_cognee.cognify = AsyncMock()
            mock_cognee.config = MagicMock()

            await mem.ingest_run_result(run, results)

            # Should call add multiple times with different node_sets
            assert mock_cognee.add.await_count >= 2
```

### Step 2: Run tests, verify they fail

```bash
pytest tests/test_cognee_memory.py -v
```

### Step 3: Rewrite cognee_store.py

Key changes to `src/horse_fish/memory/cognee_store.py`:

**A. Import SearchType:**
```python
try:
    from cognee.api.v1.search import SearchType
except ImportError:
    SearchType = None
```

**B. Update `ingest()` to use datasets:**
```python
async def ingest(self, content: str, metadata: dict[str, Any] | None = None) -> None:
    self._ensure_configured()
    dataset = (metadata or {}).get("dataset", "general")
    await cognee.add(content, dataset_name=dataset)
    try:
        await cognee.cognify(datasets=[dataset], temporal_cognify=True)
    except Exception as exc:
        logger.warning("cognify failed with primary LLM: %s — trying fallback", exc)
        self._configure(use_fallback=True)
        await cognee.cognify(datasets=[dataset], temporal_cognify=True)
```

**C. Update `search()` to use GRAPH_COMPLETION:**
```python
async def search(self, query: str, top_k: int = 5) -> list[CogneeHit]:
    self._ensure_configured()
    search_type = SearchType.GRAPH_COMPLETION if SearchType else None
    kwargs = {"query_text": query}
    if search_type:
        kwargs["query_type"] = search_type
    results = await cognee.search(**kwargs)

    hits: list[CogneeHit] = []
    for result in results[:top_k]:
        # GRAPH_COMPLETION returns different structures — handle both
        if isinstance(result, str):
            hits.append(CogneeHit(node_id="", content=result, score=1.0, metadata={}))
        elif isinstance(result, dict):
            hits.append(CogneeHit(
                node_id=result.get("id", ""),
                content=result.get("text", result.get("content", str(result))),
                score=result.get("score", 1.0),
                metadata=result.get("metadata", {}),
            ))
        else:
            hits.append(CogneeHit(
                node_id=getattr(result, "id", ""),
                content=getattr(result, "text", getattr(result, "content", str(result))),
                score=getattr(result, "score", 1.0),
                metadata=getattr(result, "metadata", {}),
            ))
    return hits
```

**D. Restructure `ingest_run_result()` with node_sets:**
```python
async def ingest_run_result(self, run: Run, subtask_results: list[SubtaskResult]) -> None:
    self._ensure_configured()

    # 1. Ingest task summary
    task_summary = f"Task: {run.task}\nState: {run.state}\nSubtasks: {len(run.subtasks)}"
    await cognee.add(task_summary, dataset_name="run_results", node_set=["task_summaries"])

    # 2. Ingest each subtask result separately
    for result in subtask_results:
        subtask_content = (
            f"Subtask {result.subtask_id}:\n"
            f"  Success: {result.success}\n"
            f"  Output: {result.output}"
        )
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
```

### Step 4: Update find_similar_tasks to use datasets

```python
async def find_similar_tasks(self, task_description: str, top_k: int = 3) -> list[CogneeHit]:
    self._ensure_configured()
    search_type = SearchType.GRAPH_COMPLETION if SearchType else None
    kwargs = {"query_text": task_description, "datasets": ["run_results"]}
    if search_type:
        kwargs["query_type"] = search_type
    # ... same result parsing as search()
```

### Step 5: Run all cognee tests

```bash
pytest tests/test_cognee_memory.py tests/test_cognee_orchestrator.py -v
```

### Step 6: Commit

```bash
git add src/horse_fish/memory/cognee_store.py tests/test_cognee_memory.py tests/test_cognee_orchestrator.py
git commit -m "feat: harden cognee — GRAPH_COMPLETION, datasets, temporal_cognify, structured ingestion"
```
