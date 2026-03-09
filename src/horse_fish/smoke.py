"""Smoke test helpers — seed broken files and verify results."""

from __future__ import annotations

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
    import asyncio

    if cognee_memory is None:
        return False, "skipped — cognee not installed"
    try:
        hits = await asyncio.wait_for(
            cognee_memory.search("smokefix add function bug fix"),
            timeout=30,
        )
        if hits:
            return True, f"found {len(hits)} hits"
        return False, "no hits found"
    except asyncio.TimeoutError:
        return False, "skipped — cognee search timed out"
    except Exception as exc:
        return False, f"error: {exc}"


def verify_lessons(run) -> tuple[bool, str]:
    """Check if lessons were extracted from the run."""
    if run.lessons:
        return True, f"{len(run.lessons)} lessons"
    return False, "no lessons extracted"
