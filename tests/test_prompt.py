"""Tests for prompt template module."""

from __future__ import annotations

from horse_fish.agents.prompt import build_fix_prompt, build_prompt


def test_build_prompt_includes_task() -> None:
    """Verify task text appears in output."""
    task = "implement feature X"
    result = build_prompt(task=task, worktree_path="/tmp/worktree", branch="feature-branch")

    assert "implement feature X" in result


def test_build_prompt_includes_worktree_info() -> None:
    """Verify worktree_path and branch appear in output."""
    result = build_prompt(
        task="test task",
        worktree_path="/tmp/my-worktree",
        branch="overstory/test-branch",
    )

    assert "/tmp/my-worktree" in result
    assert "overstory/test-branch" in result


def test_build_prompt_includes_project_context() -> None:
    """Verify project context appears in output when provided."""
    context = "Use ruff for linting."
    result = build_prompt(
        task="test task",
        worktree_path="/tmp/worktree",
        branch="main",
        project_context=context,
    )

    assert "Use ruff for linting." in result
    assert "## Project Conventions" in result


def test_build_prompt_works_without_project_context() -> None:
    """Verify no crash when project_context is None."""
    result = build_prompt(
        task="test task",
        worktree_path="/tmp/worktree",
        branch="main",
        project_context=None,
    )

    assert "test task" in result
    assert "/tmp/worktree" in result
    assert "main" in result
    assert "## Project Conventions" not in result


def test_build_prompt_includes_rules() -> None:
    """Verify 'pytest' and 'commit' appear in output."""
    result = build_prompt(task="test", worktree_path="/tmp", branch="main")

    assert "pytest" in result
    assert "Commit" in result


def test_build_prompt_includes_ruff_instruction() -> None:
    """Verify ruff check --fix instruction appears in rules."""
    result = build_prompt(task="test", worktree_path="/tmp", branch="main")
    assert "ruff check --fix" in result
    assert "ruff format" in result


def test_build_fix_prompt_contains_gate_output():
    """Test fix prompt includes gate failure output and worktree path."""
    result = build_fix_prompt(
        gate_output="ruff-check: F401 unused import 'os'",
        worktree_path="/tmp/wt",
        branch="feat-x",
    )
    assert "F401 unused import" in result
    assert "/tmp/wt" in result
    assert "fix" in result.lower()
    assert "commit" in result.lower()
