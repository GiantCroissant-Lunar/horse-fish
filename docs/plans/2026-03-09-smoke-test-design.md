# Smoke Test Design — `hf smoke`

## Goal

A single CLI command that exercises the full horse-fish pipeline end-to-end with a real agent (no mocks), verifying plan → dispatch → execute → review → merge → learn.

## Task

Fix a deliberately broken test. A buggy `add()` function returns `a - b` instead of `a + b`. The agent must find and fix the bug so `test_smokefix.py` passes.

## Seed Files

**`src/horse_fish/smokefix.py`**
```python
def add(a: int, b: int) -> int:
    return a - b  # BUG: should be a + b
```

**`tests/test_smokefix.py`**
```python
from horse_fish.smokefix import add

def test_add():
    assert add(2, 3) == 5
```

Seed files are committed to main before the run so the agent's worktree inherits them.

## CLI Command

```
hf smoke [--runtime pi] [--model qwen3.5-plus]
```

## Flow

1. **Seed** — write broken files, commit to main
2. **Run** — `orchestrator.run("Fix the failing test in tests/test_smokefix.py — the implementation in src/horse_fish/smokefix.py has a bug")`
3. **Verify** — 5 success criteria (see below)
4. **Report** — print pass/fail per criterion
5. **Cleanup** — remove seed files, revert seed commit, clean agents/worktrees

## Success Criteria

| # | Criterion | How to check |
|---|-----------|-------------|
| 1 | Pipeline completed | `run.state == RunState.completed` |
| 2 | Test passes on main | `pytest tests/test_smokefix.py` exits 0 post-merge |
| 3 | Auto-commit worked | Git log shows merge commit for the agent's branch |
| 4 | Cognee learned | `cognee_memory.search("smokefix")` returns results |
| 5 | Lessons extracted | `run.lessons` is non-empty |

## Error Handling

- **Timeout**: Orchestrator stall detector handles it (300s stall, 600s subtask timeout). Smoke reports which step failed.
- **Cleanup on failure**: try/finally ensures seed files and worktrees are always cleaned up.
- **Cognee unavailable**: Cognee check reported as "skipped" not "failed".
- **Seed conflicts**: Overwrites existing smokefix files if present.
- **Over-decomposition**: SmartPlanner should classify as SOLO. If it doesn't, that's a real bug we catch.

## Runtime

Default: Pi (qwen3.5-plus via dashscope). ~3 min expected. Free tier.
