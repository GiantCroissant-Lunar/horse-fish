"""Runtime adapters for supported agent CLIs."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import ClassVar, Protocol


class RuntimeAdapter(Protocol):
    """Protocol for agent runtime command builders."""

    runtime_id: str

    def build_spawn_command(self, model: str) -> str:
        """Build the CLI command used to launch a runtime."""

    def build_env(self) -> dict[str, str]:
        """Build environment variables required by the runtime."""


@dataclass(frozen=True, slots=True)
class ClaudeRuntime:
    """Adapter for the Claude Code CLI."""

    runtime_id: ClassVar[str] = "claude"

    def build_spawn_command(self, model: str) -> str:
        if model:
            return f"claude --model {shlex.quote(model)}"
        return "claude"

    def build_env(self) -> dict[str, str]:
        return {}


@dataclass(frozen=True, slots=True)
class CopilotRuntime:
    """Adapter for the GitHub Copilot CLI."""

    runtime_id: ClassVar[str] = "copilot"

    def build_spawn_command(self, model: str) -> str:
        return f"copilot --model {shlex.quote(model)} --allow-all-tools"

    def build_env(self) -> dict[str, str]:
        return {}


@dataclass(frozen=True, slots=True)
class PiRuntime:
    """Adapter for the Pi CLI."""

    runtime_id: ClassVar[str] = "pi"

    def build_spawn_command(self, model: str) -> str:
        return f"pi --model {shlex.quote(model)}"

    def build_env(self) -> dict[str, str]:
        return {}


@dataclass(frozen=True, slots=True)
class OpenCodeRuntime:
    """Adapter for the OpenCode CLI."""

    runtime_id: ClassVar[str] = "opencode"

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

