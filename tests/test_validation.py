"""Tests for validation gates."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from horse_fish.validation.gates import GateResult, ValidationGates

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def write_valid_py(path: Path, name: str = "ok.py") -> Path:
    """Write a syntactically correct Python file."""
    f = path / name
    f.write_text("x = 1\n")
    return f


def write_invalid_py(path: Path, name: str = "bad.py") -> Path:
    """Write a Python file with a syntax error."""
    f = path / name
    f.write_text("def foo(\n")
    return f


# ---------------------------------------------------------------------------
# FakeProcess for subprocess mocking
# ---------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout.encode(), self._stderr.encode()


def fake_exec_factory(
    processes: list[FakeProcess],
    calls: list[tuple[tuple[object, ...], dict[str, object]]],
):
    async def fake_exec(*args: object, **kwargs: object) -> FakeProcess:
        calls.append((args, kwargs))
        return processes.pop(0)

    return fake_exec


# ---------------------------------------------------------------------------
# GateResult dataclass
# ---------------------------------------------------------------------------


def test_gate_result_fields() -> None:
    r = GateResult(gate="pytest", passed=True, output="5 passed", duration_seconds=1.23)
    assert r.gate == "pytest"
    assert r.passed is True
    assert r.output == "5 passed"
    assert r.duration_seconds == pytest.approx(1.23)


# ---------------------------------------------------------------------------
# all_passed helper
# ---------------------------------------------------------------------------


def test_all_passed_true() -> None:
    results = [
        GateResult("compile", True, "", 0.1),
        GateResult("pytest", True, "", 0.5),
    ]
    assert ValidationGates.all_passed(results) is True


def test_all_passed_false_when_any_fails() -> None:
    results = [
        GateResult("compile", True, "", 0.1),
        GateResult("pytest", False, "FAILED", 0.5),
    ]
    assert ValidationGates.all_passed(results) is False


def test_all_passed_empty() -> None:
    assert ValidationGates.all_passed([]) is True


# ---------------------------------------------------------------------------
# Default gates config
# ---------------------------------------------------------------------------


def test_default_gates() -> None:
    vg = ValidationGates()
    assert vg.gates == ["compile", "ruff-check", "pytest"]


def test_custom_gates() -> None:
    vg = ValidationGates(gates=["compile"])
    assert vg.gates == ["compile"]


# ---------------------------------------------------------------------------
# compile gate — uses real tmp_path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_gate_passes_valid_file(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    write_valid_py(src, "mod.py")

    vg = ValidationGates()
    result = await vg.run_gate("compile", tmp_path)

    assert result.gate == "compile"
    assert result.passed is True
    assert result.duration_seconds >= 0


@pytest.mark.asyncio
async def test_compile_gate_fails_invalid_file(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    write_invalid_py(src, "bad.py")

    vg = ValidationGates()
    result = await vg.run_gate("compile", tmp_path)

    assert result.gate == "compile"
    assert result.passed is False
    assert result.output  # some error text


@pytest.mark.asyncio
async def test_compile_gate_no_python_files(tmp_path: Path) -> None:
    vg = ValidationGates()
    result = await vg.run_gate("compile", tmp_path)

    assert result.passed is True
    assert "no Python files" in result.output


# ---------------------------------------------------------------------------
# ruff-check gate — mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ruff_check_gate_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list = []
    processes = [FakeProcess(returncode=0, stdout="All checks passed.")]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.run_gate("ruff-check", tmp_path)

    assert result.passed is True
    assert result.output == "All checks passed."
    assert calls[0][0][0] == "ruff"
    assert "check" in calls[0][0]


@pytest.mark.asyncio
async def test_ruff_check_gate_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list = []
    processes = [FakeProcess(returncode=1, stdout="src/foo.py:1:1: E302")]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.run_gate("ruff-check", tmp_path)

    assert result.passed is False
    assert "E302" in result.output


# ---------------------------------------------------------------------------
# pytest gate — mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pytest_gate_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list = []
    processes = [FakeProcess(returncode=0, stdout="5 passed")]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.run_gate("pytest", tmp_path)

    assert result.passed is True
    assert "5 passed" in result.output
    assert calls[0][0][0] == "pytest"


@pytest.mark.asyncio
async def test_pytest_gate_fails(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list = []
    processes = [FakeProcess(returncode=1, stdout="2 failed, 3 passed")]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.run_gate("pytest", tmp_path)

    assert result.passed is False
    assert "2 failed" in result.output


# ---------------------------------------------------------------------------
# Unknown gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_gate_returns_failed_result(tmp_path: Path) -> None:
    vg = ValidationGates()
    result = await vg.run_gate("nonexistent-gate", tmp_path)

    assert result.passed is False
    assert "unknown gate" in result.output


# ---------------------------------------------------------------------------
# run_all — no short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_all_returns_all_results_no_short_circuit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """run_all should run every gate even if earlier ones fail."""
    calls: list = []
    processes = [
        FakeProcess(returncode=1, stdout="ruff error"),  # ruff-check fails
        FakeProcess(returncode=0, stdout="5 passed"),  # pytest passes
    ]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    # Use only ruff-check + pytest so we can control all subprocess calls
    vg = ValidationGates(gates=["ruff-check", "pytest"])
    results = await vg.run_all(tmp_path)

    assert len(results) == 2
    assert results[0].gate == "ruff-check"
    assert results[0].passed is False
    assert results[1].gate == "pytest"
    assert results[1].passed is True


@pytest.mark.asyncio
async def test_run_all_all_passed_helper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list = []
    processes = [
        FakeProcess(returncode=0, stdout="ok"),
        FakeProcess(returncode=0, stdout="5 passed"),
    ]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates(gates=["ruff-check", "pytest"])
    results = await vg.run_all(tmp_path)

    assert ValidationGates.all_passed(results) is True


# ---------------------------------------------------------------------------
# auto_fix — mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_fix_runs_ruff_fix_and_format(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """auto_fix should run ruff format, ruff check --fix, then re-check."""
    calls: list = []
    processes = [
        FakeProcess(returncode=0, stdout="3 files reformatted"),  # ruff format
        FakeProcess(returncode=0, stdout="Fixed 2 errors"),  # ruff check --fix
        FakeProcess(returncode=0, stdout="All checks passed!"),  # ruff check (re-check)
    ]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.auto_fix(tmp_path)

    assert result.gate == "auto-fix"
    assert result.passed is True
    assert "3 files reformatted" in result.output
    assert "Fixed 2 errors" in result.output
    # Verify order: format first, then check --fix, then re-check
    assert calls[0][0][:3] == ("ruff", "format", "src/")
    assert calls[1][0][:4] == ("ruff", "check", "--fix", "src/")
    assert calls[2][0][:3] == ("ruff", "check", "src/")


@pytest.mark.asyncio
async def test_auto_fix_returns_failed_on_unfixable_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """auto_fix should return passed=False when re-check still finds errors."""
    calls: list = []
    processes = [
        FakeProcess(returncode=0, stdout=""),  # ruff format
        FakeProcess(returncode=0, stdout=""),  # ruff check --fix
        FakeProcess(returncode=1, stdout="E999 SyntaxError: unfixable"),  # ruff check (re-check fails)
    ]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.auto_fix(tmp_path)

    assert result.gate == "auto-fix"
    assert result.passed is False
    assert "unfixable" in result.output
    # All 3 steps should run (format always runs now)
    assert len(calls) == 3


# ---------------------------------------------------------------------------
# auto_fix_and_commit — mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auto_fix_and_commit_commits_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """auto_fix_and_commit should git add, detect changes, and git commit."""
    calls: list = []
    processes = [
        FakeProcess(returncode=0, stdout="1 file reformatted"),  # ruff format
        FakeProcess(returncode=0, stdout="Fixed 1 error"),  # ruff check --fix
        FakeProcess(returncode=0, stdout="All checks passed!"),  # ruff check (re-check)
        FakeProcess(returncode=0, stdout=""),  # git add -A
        FakeProcess(returncode=1, stdout=""),  # git diff --cached --quiet (1 = has changes)
        FakeProcess(returncode=0, stdout="[branch abc123] chore: auto-fix lint"),  # git commit
    ]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.auto_fix_and_commit(tmp_path)

    assert result.gate == "auto-fix"
    assert result.passed is True
    # Verify git add -A was called (4th call, index 3)
    assert calls[3][0][:3] == ("git", "add", "-A")
    # Verify git diff --cached --quiet was called
    assert calls[4][0][:4] == ("git", "diff", "--cached", "--quiet")
    # Verify git commit was called with the right message
    assert calls[5][0][:4] == ("git", "commit", "-m", "chore: auto-fix lint")


@pytest.mark.asyncio
async def test_auto_fix_and_commit_skips_commit_when_no_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """auto_fix_and_commit should skip git commit when diff --cached --quiet returns 0."""
    calls: list = []
    processes = [
        FakeProcess(returncode=0, stdout=""),  # ruff format
        FakeProcess(returncode=0, stdout="All checks passed"),  # ruff check --fix
        FakeProcess(returncode=0, stdout=""),  # ruff check (re-check)
        FakeProcess(returncode=0, stdout=""),  # git add -A
        FakeProcess(returncode=0, stdout=""),  # git diff --cached --quiet (0 = no changes)
    ]
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec_factory(processes, calls))

    vg = ValidationGates()
    result = await vg.auto_fix_and_commit(tmp_path)

    assert result.gate == "auto-fix"
    assert result.passed is True
    assert "no changes to commit" in result.output
    # Should only have 5 calls (no git commit)
    assert len(calls) == 5
