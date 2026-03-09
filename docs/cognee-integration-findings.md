# Cognee Integration Findings

**Date:** 2026-03-09
**Session:** 7

## Overview

Integrated Cognee as the orchestrator-level knowledge graph memory. Replaced direct memvid usage at the orchestrator with Cognee (FastEmbed + LanceDB + Kuzu + Mercury 2). Agent-local memvid and SQLite LessonStore remain unchanged.

## Stack

| Component | Provider | Notes |
|-----------|----------|-------|
| Embeddings | FastEmbed | CPU-only, local, no API key. Model: `sentence-transformers/all-MiniLM-L6-v2` (384 dims) |
| Vector Store | LanceDB | File-based at `.horse-fish/cognee/lancedb`, zero setup |
| Graph Store | Kuzu | File-based at `.horse-fish/cognee/`, zero setup. **Not concurrent-safe** — only orchestrator process should use it |
| LLM (entity extraction) | Mercury 2 (Inception AI) | Model: `mercury-2`, endpoint: `api.inceptionlabs.ai/v1` |
| LLM fallback | Dashscope (qwen3.5-plus) | Automatic fallback if Mercury 2 fails |

## Python Version Requirement

**Cognee + FastEmbed require Python < 3.13.** FastEmbed uses onnxruntime which is not compatible with Python 3.13+.

```bash
pyenv install 3.12.11
pyenv local 3.12.11
pip install -e ".[memory,dev]"
```

## Cognee Config API Gotchas

These are all issues discovered during integration that differ from Cognee's official docs.

### 1. No `set_embedding_provider` / `set_embedding_model` methods

The docs show `cognee.config.set_embedding_provider("fastembed")` but this method **does not exist** in cognee 0.5.3. Embedding config must be set via environment variables:

```python
os.environ["EMBEDDING_PROVIDER"] = "fastembed"
os.environ["EMBEDDING_MODEL"] = "sentence-transformers/all-MiniLM-L6-v2"
os.environ["EMBEDDING_DIMENSIONS"] = "384"
```

### 2. Custom provider endpoint bug

When using `LLM_PROVIDER=custom`, cognee's `get_llm_client()` creates a `GenericAPIAdapter` but **does not pass the endpoint parameter**:

```python
# In cognee/infrastructure/llm/.../get_llm_client.py
# BUG: endpoint is NOT passed here
return GenericAPIAdapter(
    llm_config.llm_api_key,
    llm_config.llm_model,
    max_completion_tokens,
    "Custom",  # This is the 'name' param, NOT endpoint
    ...
)
```

Compare with the OpenAI provider which correctly passes `endpoint=llm_config.llm_endpoint`.

**Workaround:** Monkey-patch `get_llm_client` to inject the endpoint after creation:

```python
def _patched(raise_api_key_error=True):
    client = _original(raise_api_key_error)
    if hasattr(client, "endpoint") and not client.endpoint:
        cfg = get_llm_config()
        if cfg.llm_endpoint:
            client.endpoint = cfg.llm_endpoint
    return client
```

### 3. Connection test ignores custom endpoint

`test_llm_connection()` calls `LLMGateway.acreate_structured_output()` which creates a fresh adapter — the endpoint bug means it always tries `api.openai.com`.

**Workaround:** `os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"`

### 4. Use `set_llm_config(dict)` not individual methods

Individual `set_llm_provider()`, `set_llm_model()`, etc. work but `set_llm_config(dict)` is more reliable for setting all fields atomically:

```python
cognee.config.set_llm_config({
    "llm_provider": "custom",
    "llm_api_key": "...",
    "llm_model": "openai/mercury-2",
    "llm_endpoint": "https://api.inceptionlabs.ai/v1",
})
```

### 5. Graph provider method name

The method is `set_graph_database_provider()` not `set_graph_db_provider()`:

```python
cognee.config.set_graph_database_provider("kuzu")  # correct
cognee.config.set_graph_db_provider("kuzu")         # does NOT exist
```

### 6. Mercury 2 model name

The model ID is `mercury-2`, not `mercury-coder-small`. With litellm routing, use `openai/mercury-2`:

```python
cognee.config.set_llm_model("openai/mercury-2")
```

### 7. Multi-user access control warning

Cognee 0.5.0+ defaults to multi-user access control. Suppress with:

```python
os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "false"
```

## E2E Verification

Successfully tested the full pipeline:

```
cognee.add(text)     → ingests document
cognee.cognify()     → Mercury 2 extracts entities + relationships → Kuzu graph
cognee.search(query) → FastEmbed embeds query → LanceDB vector search → graph traversal
```

Search returned coherent summary: "The authentication module was fixed by agent claude-1, who modified auth.py and added JWT validation..."

## Architecture: Two-Tier Memory

```
Tier 1: Agent-local (memvid)
  └─ Each agent records raw results in .mv2 files
  └─ Lives in agent worktree, ephemeral

Tier 2: Orchestrator (Cognee)
  └─ orchestrator._learn() collects results
  └─ cognee.add() + cognee.cognify() → knowledge graph
  └─ SmartPlanner uses cognee.search() for semantic context

Tier 1.5: LessonStore (SQLite, unchanged)
  └─ Deterministic pattern extraction (over-decomposition, stalls, no-diff)
  └─ Fast queries, no LLM needed
```
