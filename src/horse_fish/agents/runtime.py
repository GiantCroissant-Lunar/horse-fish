"""Runtime adapters for supported agent CLIs."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from typing import ClassVar, Protocol


class RuntimeAdapter(Protocol):
    """Protocol for agent runtime command builders."""

    runtime_id: str
    ready_pattern: str
    ready_timeout_seconds: int
    dismiss_patterns: list[tuple[str, str]]  # [(pattern, key_to_send), ...]

    def build_spawn_command(self, model: str) -> str:
        """Build the CLI command used to launch a runtime."""

    def build_env(self) -> dict[str, str]:
        """Build environment variables required by the runtime."""

    def post_ready_commands(self, model: str) -> list[str]:
        """Commands to send via tmux after the runtime is ready (e.g. model selection)."""


@dataclass(frozen=True, slots=True)
class RuntimeOutputObservation:
    """A tool or prompt observation extracted from runtime pane output."""

    kind: str
    name: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class _ObservationRule:
    kind: str
    name: str
    pattern: re.Pattern[str]


_COMMON_TOOL_RULES: tuple[_ObservationRule, ...] = (
    _ObservationRule(
        kind="tool",
        name="tool_call",
        pattern=re.compile(
            r"(?im)^[^\S\r\n]*(?:[●•⏺*-]\s*)?(?P<tool>Bash|Read|Write|Edit|MultiEdit|Glob|Grep|LS|WebFetch|WebSearch|TodoWrite|NotebookEdit|Task|mcp__[\w.-]+)\((?P<excerpt>[^\n]*)",
        ),
    ),
    _ObservationRule(
        kind="tool",
        name="tool_label",
        pattern=re.compile(
            r"(?im)^\s*Tool:\s*(?P<tool>[A-Za-z_][\w.-]+)(?:\s*[:-]\s*(?P<excerpt>[^\n]*))?$",
        ),
    ),
)

_COMMON_PROMPT_RULES: tuple[_ObservationRule, ...] = (
    _ObservationRule(
        kind="prompt",
        name="help_prompt",
        pattern=re.compile(r"(?im)^(?P<excerpt>.*(?:\? for help|Send /help for help information|shift\+tab).*)$"),
    ),
)

_RUNTIME_PROMPT_RULES: dict[str, tuple[_ObservationRule, ...]] = {
    "claude": (
        _ObservationRule(
            kind="prompt",
            name="permission_prompt",
            pattern=re.compile(r"(?im)^(?P<excerpt>.*bypass permissions.*)$"),
        ),
    ),
    "droid": (
        _ObservationRule(
            kind="prompt",
            name="login_prompt",
            pattern=re.compile(r"(?im)^(?P<excerpt>\s*> Login.*)$"),
        ),
        _ObservationRule(
            kind="prompt",
            name="spec_mode_prompt",
            pattern=re.compile(
                r"(?im)^(?P<excerpt>.*(?:Spec Mode Model Configuration|Select another model for Spec Mode).*)$",
            ),
        ),
    ),
}


def extract_runtime_observations(runtime_id: str, output: str, *, limit: int = 6) -> list[RuntimeOutputObservation]:
    """Extract best-effort tool and prompt observations from runtime pane output."""
    if not output or limit <= 0:
        return []

    observations: list[RuntimeOutputObservation] = []
    seen: set[tuple[str, str, str]] = set()
    rules = (*_RUNTIME_PROMPT_RULES.get(runtime_id, ()), *_COMMON_PROMPT_RULES, *_COMMON_TOOL_RULES)

    for rule in rules:
        for match in rule.pattern.finditer(output):
            excerpt = (match.groupdict().get("excerpt") or match.group(0)).strip()
            if not excerpt:
                continue
            name = (match.groupdict().get("tool") or rule.name).strip()
            key = (rule.kind, name, excerpt)
            if key in seen:
                continue
            seen.add(key)
            observations.append(RuntimeOutputObservation(kind=rule.kind, name=name, excerpt=excerpt[:240]))
            if len(observations) >= limit:
                return observations

    return observations


@dataclass(slots=True)
class ClaudeRuntime:
    """Adapter for the Claude Code CLI."""

    runtime_id: ClassVar[str] = "claude"
    ready_pattern: ClassVar[str] = r"❯|shift\+tab|bypass permissions"
    ready_timeout_seconds: ClassVar[int] = 30
    dismiss_patterns: ClassVar[list[tuple[str, str]]] = []

    def build_spawn_command(self, model: str) -> str:
        parts = ["claude", "--dangerously-skip-permissions"]
        if model:
            parts.extend(["--model", shlex.quote(model)])
        return " ".join(parts)

    def build_env(self) -> dict[str, str]:
        return {}

    def post_ready_commands(self, model: str) -> list[str]:
        return []


@dataclass(slots=True)
class CopilotRuntime:
    """Adapter for the GitHub Copilot CLI."""

    runtime_id: ClassVar[str] = "copilot"
    ready_pattern: ClassVar[str] = r"^(❯\s|>\s)"
    ready_timeout_seconds: ClassVar[int] = 30
    dismiss_patterns: ClassVar[list[tuple[str, str]]] = []

    def build_spawn_command(self, model: str) -> str:
        return f"copilot --model {shlex.quote(model)} --allow-all-tools"

    def build_env(self) -> dict[str, str]:
        return {}

    def post_ready_commands(self, model: str) -> list[str]:
        return []


@dataclass(slots=True)
class PiRuntime:
    """Adapter for the Pi CLI."""

    runtime_id: ClassVar[str] = "pi"
    ready_pattern: ClassVar[str] = r"\d+\.\d+%/\d+\S+"
    ready_timeout_seconds: ClassVar[int] = 45
    dismiss_patterns: ClassVar[list[tuple[str, str]]] = []

    def build_spawn_command(self, model: str) -> str:
        return f"pi --provider dashscope --model {shlex.quote(model)}"

    def build_env(self) -> dict[str, str]:
        api_key = os.environ.get("DASHSCOPE_API_KEY")
        if api_key:
            return {"DASHSCOPE_API_KEY": api_key}
        return {}

    def post_ready_commands(self, model: str) -> list[str]:
        return []


@dataclass(slots=True)
class OpenCodeRuntime:
    """Adapter for the OpenCode CLI."""

    runtime_id: ClassVar[str] = "opencode"
    ready_pattern: ClassVar[str] = r"^(>\s|›\s)"
    ready_timeout_seconds: ClassVar[int] = 45
    dismiss_patterns: ClassVar[list[tuple[str, str]]] = []

    def build_spawn_command(self, model: str) -> str:
        return f"opencode -m {shlex.quote(model)}"

    def build_env(self) -> dict[str, str]:
        return {}

    def post_ready_commands(self, model: str) -> list[str]:
        return []


@dataclass(slots=True)
class KimiRuntime:
    """Adapter for the Kimi Code CLI (kimi-for-coding)."""

    runtime_id: ClassVar[str] = "kimi"
    ready_pattern: ClassVar[str] = r"yolo\s+agent|Send /help"
    ready_timeout_seconds: ClassVar[int] = 30
    dismiss_patterns: ClassVar[list[tuple[str, str]]] = []

    def build_spawn_command(self, model: str) -> str:
        cmd = "kimi --yolo"
        if model:
            cmd += f" --model {shlex.quote(model)}"
        return cmd

    def build_env(self) -> dict[str, str]:
        return {}

    def post_ready_commands(self, model: str) -> list[str]:
        return []


@dataclass(slots=True)
class DroidRuntime:
    """Adapter for the Factory AI Droid CLI (GLM-4.7 via Z.AI)."""

    runtime_id: ClassVar[str] = "droid"
    ready_pattern: ClassVar[str] = r"shift\+tab to cycle|>\s*Try|for help"
    ready_timeout_seconds: ClassVar[int] = 45
    # Dismiss first-run dialogs (Spec Mode config, Login prompt)
    dismiss_patterns: ClassVar[list[tuple[str, str]]] = [
        (r"Select another model for Spec Mode", "Enter"),
        (r"Spec Mode Model Configuration", "Enter"),
        (r"> Login", "Enter"),
    ]

    def build_spawn_command(self, model: str) -> str:
        return "droid --auto high"

    def build_env(self) -> dict[str, str]:
        api_key = os.environ.get("ZAI_API_KEY")
        env: dict[str, str] = {}
        if api_key:
            env["ZAI_API_KEY"] = api_key
        return env

    def post_ready_commands(self, model: str) -> list[str]:
        # Model selection is configured via ~/.factory/settings.json defaultModel.
        # The /model command opens an interactive picker that can't be scripted.
        return []


@dataclass(slots=True)
class BashRuntime:
    """Adapter for plain bash shell — used in testing."""

    runtime_id: ClassVar[str] = "bash"
    ready_pattern: ClassVar[str] = r"\$\s*$"
    ready_timeout_seconds: ClassVar[int] = 5
    dismiss_patterns: ClassVar[list[tuple[str, str]]] = []

    def build_spawn_command(self, model: str) -> str:
        return "bash"

    def build_env(self) -> dict[str, str]:
        return {}

    def post_ready_commands(self, model: str) -> list[str]:
        return []


RUNTIME_REGISTRY: dict[str, RuntimeAdapter] = {
    ClaudeRuntime.runtime_id: ClaudeRuntime(),
    CopilotRuntime.runtime_id: CopilotRuntime(),
    PiRuntime.runtime_id: PiRuntime(),
    OpenCodeRuntime.runtime_id: OpenCodeRuntime(),
    KimiRuntime.runtime_id: KimiRuntime(),
    DroidRuntime.runtime_id: DroidRuntime(),
    BashRuntime.runtime_id: BashRuntime(),
}
