"""tmux-backed process manager for interactive agent runtimes."""

from __future__ import annotations

import asyncio
import shlex
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class _TmuxResult:
    returncode: int
    stdout: str
    stderr: str


class TmuxManager:
    """Manage agent processes running in tmux sessions."""

    async def spawn(
        self,
        name: str,
        command: str,
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> int:
        wrapped_cmd = self._build_wrapped_command(command, env)
        result = await self._run_tmux("new-session", "-d", "-s", name, "-c", cwd, wrapped_cmd)
        if result.returncode != 0:
            raise RuntimeError(f"failed to start tmux session {name!r}: {self._describe_tmux_error(result.stderr)}")

        pid_result = await self._run_tmux("list-panes", "-t", name, "-F", "#{pane_pid}")
        if pid_result.returncode != 0:
            raise RuntimeError(f"failed to read pane pid for {name!r}: {self._describe_tmux_error(pid_result.stderr)}")

        pane_pid = pid_result.stdout.splitlines()[0].strip() if pid_result.stdout.strip() else ""
        if not pane_pid:
            raise RuntimeError(f"tmux session {name!r} did not report a pane pid")

        try:
            return int(pane_pid)
        except ValueError as exc:
            raise RuntimeError(f"tmux session {name!r} returned invalid pane pid: {pane_pid!r}") from exc

    async def send_keys(self, session_name: str, text: str) -> None:
        result = await self._run_tmux("send-keys", "-t", session_name, "-l", text)
        if result.returncode != 0:
            raise RuntimeError(f"failed to send keys to {session_name!r}: {self._describe_tmux_error(result.stderr)}")

        enter_result = await self._run_tmux("send-keys", "-t", session_name, "Enter")
        if enter_result.returncode != 0:
            raise RuntimeError(
                f"failed to send enter to {session_name!r}: {self._describe_tmux_error(enter_result.stderr)}"
            )

    async def capture_pane(self, session_name: str) -> str | None:
        result = await self._run_tmux("capture-pane", "-t", session_name, "-p")
        if result.returncode != 0:
            if self._session_missing(result.stderr):
                return None
            raise RuntimeError(
                f"failed to capture pane for {session_name!r}: {self._describe_tmux_error(result.stderr)}"
            )
        return result.stdout

    async def kill_session(self, session_name: str) -> None:
        result = await self._run_tmux("kill-session", "-t", session_name)
        if result.returncode != 0 and not self._session_missing(result.stderr):
            raise RuntimeError(f"failed to kill session {session_name!r}: {self._describe_tmux_error(result.stderr)}")

    async def list_sessions(self) -> list[str]:
        result = await self._run_tmux("list-sessions", "-F", "#{session_name}")
        if result.returncode != 0:
            if self._no_server_running(result.stderr):
                return []
            raise RuntimeError(f"failed to list tmux sessions: {self._describe_tmux_error(result.stderr)}")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    async def is_alive(self, session_name: str) -> bool:
        return session_name in await self.list_sessions()

    async def _run_tmux(self, *args: str) -> _TmuxResult:
        try:
            process = await asyncio.create_subprocess_exec(
                "tmux",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("tmux is not installed or not available on PATH") from exc

        stdout, stderr = await process.communicate()
        return _TmuxResult(
            returncode=process.returncode,
            stdout=stdout.decode(),
            stderr=stderr.decode(),
        )

    @staticmethod
    def _build_wrapped_command(command: str, env: dict[str, str] | None) -> str:
        exports = [f"export {key}={shlex.quote(value)}" for key, value in sorted((env or {}).items())]
        if not exports:
            return command
        return " && ".join([*exports, command])

    @staticmethod
    def _describe_tmux_error(stderr: str) -> str:
        return stderr.strip() or "unknown tmux error"

    @staticmethod
    def _no_server_running(stderr: str) -> bool:
        return "no server running" in stderr.lower()

    @classmethod
    def _session_missing(cls, stderr: str) -> bool:
        lowered = stderr.lower()
        return cls._no_server_running(lowered) or "can't find session" in lowered or "can't find pane" in lowered
