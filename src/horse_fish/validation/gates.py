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

    async def _run_pytest(self, worktree_path: Path, timeout: int = 120) -> tuple[bool, str]:
        proc = await asyncio.create_subprocess_exec(
            "pytest",
            "tests/",
            "--ignore=tests/test_e2e.py",
            "--ignore=tests/test_smoke.py",
            "-x",
            "-q",
            cwd=str(worktree_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"pytest timed out after {timeout}s"
        output = stdout.decode().strip() or stderr.decode().strip()
        return proc.returncode == 0, output

    async def auto_fix(self, worktree_path: str | Path) -> GateResult:
        """Run ruff check --fix and ruff format to auto-fix lint issues.

        Returns GateResult with gate='auto-fix'. If ruff check --fix exits
        non-zero (unfixable errors remain), returns passed=False immediately.
        """
        worktree_path = Path(worktree_path)
        start = time.monotonic()

        try:
            # Run ruff check --fix
            proc = await asyncio.create_subprocess_exec(
                "ruff",
                "check",
                "--fix",
                "src/",
                "tests/",
                cwd=str(worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            check_output = stdout.decode().strip() or stderr.decode().strip()

            if proc.returncode != 0:
                duration = time.monotonic() - start
                return GateResult(gate="auto-fix", passed=False, output=check_output, duration_seconds=duration)

            # Run ruff format
            proc = await asyncio.create_subprocess_exec(
                "ruff",
                "format",
                "src/",
                "tests/",
                cwd=str(worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            format_output = stdout.decode().strip() or stderr.decode().strip()

            duration = time.monotonic() - start
            combined = "\n".join(filter(None, [check_output, format_output]))
            return GateResult(gate="auto-fix", passed=True, output=combined, duration_seconds=duration)
        except Exception as exc:
            duration = time.monotonic() - start
            return GateResult(gate="auto-fix", passed=False, output=f"auto-fix error: {exc}", duration_seconds=duration)

    async def auto_fix_and_commit(self, worktree_path: str | Path) -> GateResult:
        """Run auto_fix, then stage and commit any changes.

        1. Calls auto_fix — if it fails, returns the failure result.
        2. Runs git add -A in the worktree.
        3. Runs git diff --cached --quiet to check for staged changes.
        4. If changes exist, commits with 'chore: auto-fix lint'.
        5. Returns GateResult with passed=True.
        """
        worktree_path = Path(worktree_path)
        start = time.monotonic()

        try:
            fix_result = await self.auto_fix(worktree_path)
            if not fix_result.passed:
                return fix_result

            # Stage all changes
            proc = await asyncio.create_subprocess_exec(
                "git",
                "add",
                "-A",
                cwd=str(worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()

            # Check if there are staged changes (returncode 1 = has changes)
            proc = await asyncio.create_subprocess_exec(
                "git",
                "diff",
                "--cached",
                "--quiet",
                cwd=str(worktree_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()
            has_changes = proc.returncode != 0

            output_parts = [fix_result.output]
            if has_changes:
                proc = await asyncio.create_subprocess_exec(
                    "git",
                    "commit",
                    "-m",
                    "chore: auto-fix lint",
                    cwd=str(worktree_path),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await proc.communicate()
                commit_output = stdout.decode().strip() or stderr.decode().strip()
                output_parts.append(commit_output)
            else:
                output_parts.append("no changes to commit")

            duration = time.monotonic() - start
            combined = "\n".join(filter(None, output_parts))
            return GateResult(gate="auto-fix", passed=True, output=combined, duration_seconds=duration)
        except Exception as exc:
            duration = time.monotonic() - start
            return GateResult(
                gate="auto-fix", passed=False, output=f"auto-fix-and-commit error: {exc}", duration_seconds=duration
            )
