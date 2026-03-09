"""Runtime adapters for supported agent CLIs."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from typing import ClassVar, Protocol


def _get_tmux_env(key: str) -> str | None:
    """Read a variable from tmux global environment (fallback for non-exported keys)."""
    import subprocess

    try:
        result = subprocess.run(
            ["tmux", "show-environment", "-g", key],
            capture_output=True,
            timeout=5,
        )
        if result.returncode == 0:
            line = result.stdout.decode().strip()
            if "=" in line:
                return line.split("=", 1)[1]
    except Exception:
        pass
    return None


class RuntimeAdapter(Protocol):
    """Protocol for agent runtime command builders."""

    runtime_id: str
    ready_pattern: str
    ready_timeout_seconds: int

    def build_spawn_command(self, model: str) -> str:
        """Build the CLI command used to launch a runtime."""

    def build_env(self) -> dict[str, str]:
        """Build environment variables required by the runtime."""


@dataclass(slots=True)
class ClaudeRuntime:
    """Adapter for the Claude Code CLI."""

    runtime_id: ClassVar[str] = "claude"
    ready_pattern: ClassVar[str] = r"❯|shift\+tab|bypass permissions"
    ready_timeout_seconds: ClassVar[int] = 30

    def build_spawn_command(self, model: str) -> str:
        if model:
            return f"claude --model {shlex.quote(model)}"
        return "claude"

    def build_env(self) -> dict[str, str]:
        return {}


@dataclass(slots=True)
class CopilotRuntime:
    """Adapter for the GitHub Copilot CLI."""

    runtime_id: ClassVar[str] = "copilot"
    ready_pattern: ClassVar[str] = r"^(❯\s|>\s)"
    ready_timeout_seconds: ClassVar[int] = 30

    def build_spawn_command(self, model: str) -> str:
        return f"copilot --model {shlex.quote(model)} --allow-all-tools"

    def build_env(self) -> dict[str, str]:
        return {}


@dataclass(slots=True)
class PiRuntime:
    """Adapter for the Pi CLI."""

    runtime_id: ClassVar[str] = "pi"
    ready_pattern: ClassVar[str] = r"\d+\.\d+%/\d+\S+"
    ready_timeout_seconds: ClassVar[int] = 45

    def build_spawn_command(self, model: str) -> str:
        return f"pi --provider dashscope --model {shlex.quote(model)}"

    def build_env(self) -> dict[str, str]:
        api_key = os.environ.get("DASHSCOPE_API_KEY") or _get_tmux_env("DASHSCOPE_API_KEY")
        if api_key:
            return {"DASHSCOPE_API_KEY": api_key}
        return {}


@dataclass(slots=True)
class OpenCodeRuntime:
    """Adapter for the OpenCode CLI."""

    runtime_id: ClassVar[str] = "opencode"
    ready_pattern: ClassVar[str] = r"^(>\s|›\s)"
    ready_timeout_seconds: ClassVar[int] = 45

    def build_spawn_command(self, model: str) -> str:
        return f"opencode -m {shlex.quote(model)}"

    def build_env(self) -> dict[str, str]:
        return {}


RUNTIME_REGISTRY: dict[str, RuntimeAdapter] = {
    ClaudeRuntime.runtime_id: ClaudeRuntime(),
    CopilotRuntime.runtime_id: CopilotRuntime(),
    PiRuntime.runtime_id: PiRuntime(),
    OpenCodeRuntime.runtime_id: OpenCodeRuntime(),
}
