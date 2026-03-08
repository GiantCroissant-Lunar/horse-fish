# Cognee Integration Design

**Date**: 2026-03-09
**Status**: Approved

## Overview

Replace orchestrator-level memvid with Cognee knowledge graph for richer semantic memory. Agent-local memvid and SQLite LessonStore remain unchanged.

## Architecture: Two-Tier Memory

```
┌─────────────────────────────────────────────────┐
│ Tier 1: Agent-Local (memvid)                    │
│  Each agent records raw results in .mv2 files   │
│  Lives in agent worktree, ephemeral             │
└──────────────────┬──────────────────────────────┘
                   │ orchestrator._learn() collects
                   ▼
┌─────────────────────────────────────────────────┐
│ Tier 2: Orchestrator Knowledge (cognee)         │
│  FastEmbed (CPU embeddings, local)              │
│  LanceDB (file-based vector store)              │
│  Kuzu (file-based graph store)                  │
│  LLM: Mercury 2 → fallback Dashscope (qwen)    │
└──────────────────┬──────────────────────────────┘
                   │
┌──────────────────┴──────────────────────────────┐
│ Tier 1.5: LessonStore (SQLite, unchanged)       │
│  Deterministic pattern extraction, fast queries  │
└─────────────────────────────────────────────────┘
```

## Components

### 1. CogneeMemory class (new)

Location: `src/horse_fish/memory/cognee_store.py`

Replaces MemoryStore at orchestrator level:

- `async ingest(content: str, metadata: dict)` — cognee.add() + cognee.cognify()
- `async search(query: str, top_k: int = 5)` — cognee.search()
- `async ingest_run_result(run, subtask_results)` — format + ingest
- Init configures Cognee: FastEmbed, LanceDB, Kuzu, LLM provider
- Storage at `.horse-fish/cognee/`

### 2. LLM Fallback Chain

- Primary: Mercury 2 (custom provider, openai/mercury-coder-small, api.inceptionlabs.ai)
- Fallback: Dashscope (openai/qwen3.5-plus)
- Automatic fallback on cognify failure, logged via tracer

### 3. Orchestrator Changes

- `_learn()`: swap memory.store_run_result() → cognee.ingest_run_result()
- SmartPlanner: swap memory.find_similar_tasks() → cognee.search()
- MemoryStore (memvid) stays for agent-local recording

### 4. CLI Changes

- `_init_components()` creates CogneeMemory instead of wiring MemoryStore to orchestrator
- MemoryStore still created for agent pool (tier 1)

## Config

```python
cognee.config.set_embedding_provider("fastembed")
cognee.config.set_embedding_model("sentence-transformers/all-MiniLM-L6-v2")
cognee.config.set_vector_db_provider("lancedb")
cognee.config.set_graph_db_provider("kuzu")
cognee.config.set_llm_provider("custom")
cognee.config.set_llm_model("openai/mercury-coder-small")
cognee.config.set_llm_endpoint("https://api.inceptionlabs.ai/v1")
cognee.config.set_llm_api_key("<INCEPTION_API_KEY>")
```

## What Stays The Same

- MemoryStore (memvid) — agent-local recording
- LessonStore — SQLite, deterministic patterns, feeds SmartPlanner
- SmartPlanner — uses LessonStore + adds Cognee for semantic search

## Dependencies

```toml
cognee = ["cognee"]  # FastEmbed, LanceDB, Kuzu are defaults
```

## Test Strategy

- Unit tests: mock cognee.add/cognify/search, verify CogneeMemory wraps correctly
- Integration test: real Cognee with FastEmbed+LanceDB+Kuzu (all file-based)
- Mercury 2 fallback test: mock LLM failure, verify Dashscope retry
