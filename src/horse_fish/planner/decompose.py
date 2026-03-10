"""Task decomposition via LLM CLI runtimes."""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid

from horse_fish.agents.runtime import RUNTIME_REGISTRY
from horse_fish.models import Subtask
from horse_fish.observability.prompts import resolve_text_prompt
from horse_fish.observability.traces import Tracer

SYSTEM_PROMPT_TEMPLATE = """\
You are a task decomposition assistant. Given a high-level task description, break it down into
concrete, independently-implementable subtasks suitable for parallel execution by AI coding agents.

Return a JSON array of subtask objects. Each object must have:
- "description": string — a clear, self-contained description of what to implement
- "deps": array of strings — descriptions of other subtasks this depends on (use exact description strings)
- "files_hint": array of strings — file paths likely involved (relative to project root)

Rules:
- Return ONLY a JSON array, no other text outside the JSON
- Each subtask must be independently testable
- Keep subtasks focused and concrete
- Use deps to express ordering constraints (list descriptions of prerequisite subtasks)
- Aim for 3-8 subtasks for most tasks

Context: {context}

Task: {task}
"""

DECOMPOSE_PROMPT_NAME = "planner-decompose"

_CLI_COMMANDS: dict[str, list[str]] = {
    "claude": ["claude", "--print", "--model", "{model}", "{prompt}"],
    "copilot": ["copilot", "--print", "--model", "{model}", "{prompt}"],
    "pi": ["pi", "--print", "--provider", "dashscope", "--model", "{model}", "{prompt}"],
    "opencode": ["opencode", "--print", "-m", "{model}", "{prompt}"],
    "droid": ["droid", "exec", "-m", "{model}", "{prompt}"],
}

_DEFAULT_MODELS: dict[str, str] = {
    "claude": "claude-sonnet-4-6",
    "copilot": "gpt-4o",
    "pi": "qwen3.5-plus",
    "opencode": "qwen3.5-plus",
    "droid": "glm-4.7",
}


class PlannerError(Exception):
    """Raised when the planner fails to decompose a task."""


class Planner:
    """Decomposes tasks into subtask DAGs via LLM CLI runtimes."""

    def __init__(self, runtime: str = "claude", model: str | None = None, tracer: Tracer | None = None) -> None:
        if runtime not in _CLI_COMMANDS:
            raise ValueError(f"Unknown runtime: {runtime!r}. Must be one of: {sorted(_CLI_COMMANDS)}")
        self.runtime = runtime
        self.model = model or _DEFAULT_MODELS[runtime]
        self._tracer = tracer

    async def decompose(self, task: str, context: str = "") -> list[Subtask]:
        """Decompose a task description into a list of Subtask objects."""
        resolved_prompt = resolve_text_prompt(
            self._tracer,
            DECOMPOSE_PROMPT_NAME,
            SYSTEM_PROMPT_TEMPLATE,
            task=task,
            context=context or "No additional context provided.",
        )
        prompt = resolved_prompt.compiled
        generation = (
            self._tracer.generation(
                None,
                "planner.decompose",
                input={"task": task, "context": context, "prompt": prompt},
                metadata={
                    "runtime": self.runtime,
                    "model": self.model,
                    "context_provided": bool(context),
                    "prompt_name": resolved_prompt.name,
                    "prompt_source": resolved_prompt.source,
                    "prompt_version": resolved_prompt.version,
                },
                model=self.model,
                model_parameters={"runtime": self.runtime},
                prompt=resolved_prompt.prompt_client,
            )
            if self._tracer
            else None
        )

        try:
            cmd = self._build_command(prompt)
            raw = await self._run_cli(cmd)
            subtasks = self._parse_response(raw)
        except Exception as exc:
            if self._tracer and generation:
                self._tracer.end_span(
                    generation,
                    {"error": str(exc)},
                    metadata={
                        "runtime": self.runtime,
                        "model": self.model,
                        "prompt_name": resolved_prompt.name,
                        "prompt_source": resolved_prompt.source,
                    },
                    level="ERROR",
                    status_message=str(exc),
                )
            raise

        if self._tracer and generation:
            self._tracer.end_span(
                generation,
                {"raw_output": raw, "subtask_count": len(subtasks)},
                metadata={
                    "runtime": self.runtime,
                    "model": self.model,
                    "prompt_name": resolved_prompt.name,
                    "prompt_source": resolved_prompt.source,
                    "prompt_version": resolved_prompt.version,
                },
            )
        return subtasks

    def _build_prompt(self, task: str, context: str) -> str:
        return SYSTEM_PROMPT_TEMPLATE.format(task=task, context=context or "No additional context provided.")

    def _build_command(self, prompt: str) -> list[str]:
        template = _CLI_COMMANDS[self.runtime]
        return [part.format(model=self.model, prompt=prompt) if "{" in part else part for part in template]

    async def _run_cli(self, cmd: list[str], timeout: float = 120.0) -> str:
        env = None
        adapter = RUNTIME_REGISTRY.get(self.runtime)
        if adapter:
            extra = adapter.build_env()
            if extra:
                env = {**os.environ, **extra}
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise PlannerError(f"Runtime CLI timed out after {timeout}s") from None
        if proc.returncode != 0:
            raise PlannerError(f"Runtime CLI exited with code {proc.returncode}: {stderr.decode()}")
        return stdout.decode()

    def _parse_response(self, raw: str) -> list[Subtask]:
        """Extract JSON from raw CLI output and return Subtask list."""
        text = raw.strip()
        if not text:
            raise PlannerError("Empty response from runtime CLI")

        # Strip markdown code fences if present
        fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
        if fence_match:
            text = fence_match.group(1).strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PlannerError(f"Failed to parse JSON from runtime response: {exc}\nRaw: {raw!r}") from exc

        if not isinstance(data, list):
            raise PlannerError(f"Expected JSON array from runtime, got {type(data).__name__}")

        subtasks: list[Subtask] = []
        for item in data:
            if not isinstance(item, dict):
                raise PlannerError(f"Expected subtask object, got {type(item).__name__}: {item!r}")
            description = item.get("description")
            if not description or not isinstance(description, str):
                raise PlannerError(f"Subtask missing valid 'description' field: {item!r}")
            subtask = Subtask(
                id=str(uuid.uuid4()),
                description=description,
                deps=item.get("deps", []),
                files_hint=item.get("files_hint", []),
            )
            subtasks.append(subtask)

        return subtasks
