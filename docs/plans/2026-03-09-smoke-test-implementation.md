# Smoke Test (`hf smoke`) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add an `hf smoke` CLI command that seeds a broken test, runs the full pipeline with a real Pi agent, verifies 5 success criteria, and cleans up.

**Architecture:** Single CLI command with 5 phases: seed → run → verify → report → cleanup. Reuses existing `_init_components` for orchestrator setup. Seed files are committed to main so worktrees inherit them.

**Tech Stack:** Click CLI, asyncio, existing orchestrator/pool/cognee components.

---

### Task 1: Create seed files helper

**Files:**
- Create: `src/horse_fish/smoke.py`

**Step 1: Write the smoke module with seed/cleanup helpers**

```python
"""Smoke test helpers — seed broken files and verify results."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

SMOKEFIX_SRC = '''\
def add(a: int, b: int) -> int:
    return a - b  # BUG: should be a + b
'''

SMOKEFIX_TEST = '''\
from horse_fish.smokefix import add


def test_add():
    assert add(2, 3) == 5
'''

TASK_DESCRIPTION = (
    "Fix the failing test in tests/test_smokefix.py — "
    "the implementation in src/horse_fish/smokefix.py has a bug. "
    "Run pytest tests/test_smokefix.py to verify your fix."
)


def seed(repo_root: Path) -> str:
    """Write broken smokefix files and commit to main. Returns the commit SHA."""
    src_file = repo_root / "src" / "horse_fish" / "smokefix.py"
    test_file = repo_root / "tests" / "test_smokefix.py"

    src_file.write_text(SMOKEFIX_SRC)
    test_file.write_text(SMOKEFIX_TEST)

    subprocess.run(
        ["git", "add", str(src_file), str(test_file)],
        cwd=repo_root, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "smoke: seed broken smokefix test"],
        cwd=repo_root, check=True, capture_output=True,
    )
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def cleanup(repo_root: Path, seed_sha: str) -> None:
    """Remove seed files and revert the seed commit."""
    src_file = repo_root / "src" / "horse_fish" / "smokefix.py"
    test_file = repo_root / "tests" / "test_smokefix.py"

    # Remove files if they exist
    for f in (src_file, test_file):
        if f.exists():
            f.unlink()

    # Check if we're on main (merge might have left us elsewhere)
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    )
    if result.stdout.strip() != "main":
        subprocess.run(
            ["git", "checkout", "main"],
            cwd=repo_root, check=True, capture_output=True,
        )

    # Stage removals and any merge artifacts, then commit
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo_root, check=True, capture_output=True,
    )
    # Only commit if there are staged changes
    status = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=repo_root, capture_output=True,
    )
    if status.returncode != 0:
        subprocess.run(
            ["git", "commit", "-m", "smoke: cleanup smokefix seed files"],
            cwd=repo_root, check=True, capture_output=True,
        )


def verify_test_passes(repo_root: Path) -> tuple[bool, str]:
    """Run pytest on test_smokefix.py in the repo root. Returns (passed, output)."""
    result = subprocess.run(
        ["pytest", "tests/test_smokefix.py", "-v"],
        cwd=repo_root, capture_output=True, text=True,
    )
    return result.returncode == 0, result.stdout + result.stderr


def verify_merge_commit(repo_root: Path, seed_sha: str) -> tuple[bool, str]:
    """Check git log for a merge commit after the seed commit."""
    result = subprocess.run(
        ["git", "log", "--oneline", f"{seed_sha}..HEAD"],
        cwd=repo_root, check=True, capture_output=True, text=True,
    )
    log = result.stdout.strip()
    has_merge = "Merge" in log or "merge" in log or "Auto-commit" in log
    return has_merge, log


async def verify_cognee(cognee_memory) -> tuple[bool, str]:
    """Search Cognee for smokefix-related knowledge. Returns (found, detail)."""
    if cognee_memory is None:
        return False, "skipped — cognee not installed"
    try:
        hits = await cognee_memory.search("smokefix add function bug fix")
        if hits:
            return True, f"found {len(hits)} hits"
        return False, "no hits found"
    except Exception as exc:
        return False, f"error: {exc}"


def verify_lessons(run) -> tuple[bool, str]:
    """Check if lessons were extracted from the run."""
    if run.lessons:
        return True, f"{len(run.lessons)} lessons"
    return False, "no lessons extracted"
```

**Step 2: Verify the module imports cleanly**

Run: `python -c "from horse_fish.smoke import seed, cleanup, TASK_DESCRIPTION; print('OK')"`
Expected: `OK`

**Step 3: Commit**

```bash
git add src/horse_fish/smoke.py
git commit -m "feat: add smoke test helpers (seed, cleanup, verify)"
```

---

### Task 2: Add `hf smoke` CLI command

**Files:**
- Modify: `src/horse_fish/cli.py` (add smoke command at bottom, before `if __name__`)

**Step 1: Add the smoke command to cli.py**

Add after the `logs` command:

```python
@main.command()
@click.option("--runtime", default="pi", help="Runtime for smoke test agent")
@click.option("--model", default=None, help="Model override")
def smoke(runtime: str, model: str | None):
    """Run end-to-end smoke test with a real agent."""
    from horse_fish.smoke import (
        TASK_DESCRIPTION,
        cleanup,
        seed,
        verify_cognee,
        verify_lessons,
        verify_merge_commit,
        verify_test_passes,
    )

    repo_root = Path.cwd()
    results: list[tuple[str, bool, str]] = []

    # Phase 1: Seed
    click.echo("=== Phase 1: Seeding broken test ===")
    seed_sha = seed(repo_root)
    click.echo(f"Seeded at commit {seed_sha[:8]}")

    try:
        # Phase 2: Run pipeline
        click.echo("=== Phase 2: Running pipeline ===")
        orchestrator, store, pool = _init_components(runtime, model, max_agents=1)
        try:
            run_result = asyncio.run(orchestrator.run(TASK_DESCRIPTION))
            click.echo(f"Run {run_result.id}: {run_result.state}")
            for subtask in run_result.subtasks:
                click.echo(f"  [{subtask.state}] {subtask.description}")
        except Exception as exc:
            click.echo(f"Pipeline failed: {exc}")
            run_result = None
        finally:
            # Clean up agents regardless
            asyncio.run(pool.cleanup())
            store.close()

        # Phase 3: Verify
        click.echo("\n=== Phase 3: Verification ===")

        # Check 1: Pipeline completed
        if run_result and run_result.state.value == "completed":
            results.append(("pipeline_completed", True, "completed"))
        else:
            state = run_result.state.value if run_result else "no result"
            results.append(("pipeline_completed", False, state))

        # Check 2: Test passes on main
        passed, output = verify_test_passes(repo_root)
        results.append(("test_passes", passed, output.splitlines()[-1] if output.strip() else "no output"))

        # Check 3: Merge commit exists
        passed, log = verify_merge_commit(repo_root, seed_sha)
        results.append(("merge_commit", passed, log.splitlines()[0] if log.strip() else "no commits"))

        # Check 4: Cognee learned
        cognee_mem = CogneeMemory() if CogneeMemory else None
        passed, detail = asyncio.run(verify_cognee(cognee_mem))
        results.append(("cognee_learned", passed, detail))

        # Check 5: Lessons extracted
        if run_result:
            passed, detail = verify_lessons(run_result)
            results.append(("lessons_extracted", passed, detail))
        else:
            results.append(("lessons_extracted", False, "no run result"))

        # Phase 4: Report
        click.echo("\n=== Phase 4: Results ===")
        all_passed = True
        for name, passed, detail in results:
            status = "PASS" if passed else "FAIL"
            if not passed and "skipped" in detail:
                status = "SKIP"
            else:
                all_passed = all_passed and passed
            click.echo(f"  [{status}] {name}: {detail}")

        click.echo(f"\n{'ALL CHECKS PASSED' if all_passed else 'SOME CHECKS FAILED'}")

    finally:
        # Phase 5: Cleanup
        click.echo("\n=== Phase 5: Cleanup ===")
        cleanup(repo_root, seed_sha)
        click.echo("Cleaned up seed files")
```

**Step 2: Verify CLI registers the command**

Run: `hf smoke --help`
Expected: Shows help with `--runtime` and `--model` options

**Step 3: Commit**

```bash
git add src/horse_fish/cli.py
git commit -m "feat: add hf smoke CLI command for e2e testing"
```

---

### Task 3: Run the live smoke test

**Step 1: Ensure Pi environment is ready**

```bash
tmux set-environment -g DASHSCOPE_API_KEY "REDACTED_DASHSCOPE_KEY"
```

**Step 2: Run the smoke test**

```bash
hf smoke --runtime pi
```

Watch the output and note any failures.

**Step 3: If failures occur, debug and fix**

Common failure modes:
- SmartPlanner over-decomposes → check planner output, may need to adjust SOLO threshold
- Agent doesn't fix the bug → check `hf logs` output, may need to improve prompt
- Validation gates fail on unrelated tests → may need to scope gates to `test_smokefix.py`
- Cognee not installed → `pip install -e ".[memory]"`
- Merge fails → check if worktree branch exists, check for conflicts

**Step 4: If the full test suite fails in validation gates**

The gates run `pytest tests/` in the worktree. If unrelated tests fail there, we have two options:
- A: Scope the smoke test gates to only `pytest tests/test_smokefix.py` (pragmatic)
- B: Fix whatever tests fail in a worktree context (thorough)

Decide based on what actually fails.

---

### Task 4: Fix any issues found and re-run

This task is iterative — fix whatever breaks, re-run `hf smoke`, repeat until all 5 checks pass.

**Step 1: After all checks pass, commit any fixes**

```bash
git add -A
git commit -m "fix: smoke test fixes from live run"
```

**Step 2: Update memory with findings**

Document what worked, what broke, and any workarounds in the session handover doc.
