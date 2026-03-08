# Batch 5 Design — 2026-03-08

## Overview

Three parallel tasks plus housekeeping. Focus: make horse-fish usable for real DAG execution.

## 1. Blocker-aware dispatch (ID-based deps)

**Problem:** `_deps_met()` matches deps by description string — fragile if planner wording varies.

**Change:** Switch deps to subtask IDs.

- `_deps_met()` matches `subtask.deps` against `s.id` of done subtasks (not `s.description`)
- Add `_dispatchable(run) -> list[Subtask]` helper that returns pending subtasks with deps met
- Planner must emit ID-based deps (update `decompose.py` to wire subtask IDs into deps)
- Update existing tests that set deps as descriptions

**Files:** `engine.py`, `decompose.py`, `test_orchestrator.py`
**Tests:** ~3 new tests (deps met with IDs, circular dep detection, partial DAG completion)

## 2. CLI `hf logs`

**Command:** `hf logs [--agent NAME] [--follow] [--lines N]`

- No args: list all active tmux sessions + last 20 lines each
- `--agent NAME`: full pane capture for that agent
- `--follow`: poll every 2s, print new output (like `tail -f`)
- `--lines N`: control how many lines (default 20)

Uses existing `TmuxManager.capture_pane()` and `TmuxManager.list_sessions()`.

**Files:** `cli.py`, `test_cli.py`
**Tests:** ~4 tests (list all, single agent, agent not found, lines option)

## 3. End-to-end integration test

Real subprocess test (not mocked) proving tmux + worktree + merge flow.

**Setup:**
- Create temp git repo with initial commit
- Mock Planner to return a single subtask
- Use a shell script "agent" that creates a file + commits in worktree

**Flow:**
1. Orchestrator.run() with real Pool, real TmuxManager, real WorktreeManager
2. Shell script agent runs in tmux, creates file, commits
3. Orchestrator detects completion via collect_result
4. Merge happens, verify file on main branch

**Files:** `tests/test_e2e.py`
**Tests:** ~2-3 (single subtask flow, two-subtask DAG, failure/retry)
**Requires:** tmux available in test env

## 4. .gitignore housekeeping

Add to `.gitignore`:
```
__pycache__/
.env
.horse-fish/
*.pyc
```

## Agent Assignment

| Agent | Runtime | Task | Est. Time |
|-------|---------|------|-----------|
| blocker-dispatch | Pi/qwen3.5-plus | ID-based deps + _dispatchable | ~3min |
| cli-logs | Pi/qwen3.5-plus | hf logs command | ~3min |
| e2e-test | Claude | Real subprocess e2e test | ~3min |

.gitignore done manually before swarming.
