"""Validation gates for pre-merge quality checks."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GateResult:
    gate: str  # 'ruff-check' | 'ruff-format' | 'pytest' | 'compile'
    passed: bool
    output: str
    duration_seconds: float


class ValidationGates:
    """Run quality gates against a worktree path before merge."""

    DEFAULT_GATES = ["compile", "ruff-check", "pytest"]

    def __init__(self, gates: list[str] | None = None) -> None:
        self.gates = gates if gates is not None else list(self.DEFAULT_GATES)

    async def run_all(self, worktree_path: str | Path) -> list[GateResult]:
        """Run all configured gates sequentially, returning all results (no short-circuit)."""
        results = []
        for gate in self.gates:
            result = await self.run_gate(gate, worktree_path)
            results.append(result)
        return results

    async def run_gate(self, gate: str, worktree_path: str | Path) -> GateResult:
        """Run a single named gate against the given worktree path."""
        worktree_path = Path(worktree_path)
        start = time.monotonic()

        try:
            if gate == "compile":
                passed, output = await self._run_compile(worktree_path)
            elif gate == "ruff-check":
                passed, output = await self._run_ruff_check(worktree_path)
            elif gate == "ruff-format":
                passed, output = await self._run_ruff_format(worktree_path)
            elif gate == "pytest":
                passed, output = await self._run_pytest(worktree_path)
            else:
                passed, output = False, f"unknown gate: {gate!r}"
        except Exception as exc:
            passed, output = False, f"gate error: {exc}"

        duration = time.monotonic() - start
        return GateResult(gate=gate, passed=passed, output=output, duration_seconds=duration)

    @staticmethod
    def all_passed(results: list[GateResult]) -> bool:
        return all(r.passed for r in results)

    async def _run_compile(self, worktree_path: Path) -> tuple[bool, str]:
        py_files = list(worktree_path.glob("src/**/*.py")) + list(worktree_path.glob("tests/**/*.py"))
        if not py_files:
            return True, "no Python files found"

        all_output: list[str] = []
        all_passed = True
        for py_file in py_files:
            proc = await asyncio.create_subprocess_exec(
                "python",
                "-m",
                "py_compile",
                str(py_file),
                cwd=str(worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                all_passed = False
                all_output.append(stderr.decode().strip() or stdout.decode().strip())

        return all_passed, "\n".join(all_output) if all_output else "all files compile"

    async def _run_ruff_check(self, worktree_path: Path) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_exec(
            "ruff",
            "check",
            "src/",
            "tests/",
            cwd=str(worktree_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode().strip() or stderr.decode().strip()
        return proc.returncode == 0, output

    async def _run_ruff_format(self, worktree_path: Path) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_exec(
            "ruff",
            "format",
            "--check",
            "src/",
            "tests/",
            cwd=str(worktree_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode().strip() or stderr.decode().strip()
        return proc.returncode == 0, output

    async def _run_pytest(self, worktree_path: Path) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_exec(
            "pytest",
            "tests/",
            cwd=str(worktree_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode().strip() or stderr.decode().strip()
        return proc.returncode == 0, output
