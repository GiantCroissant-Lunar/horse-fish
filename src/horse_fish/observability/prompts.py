"""Helpers for Langfuse-managed prompts with local fallbacks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from horse_fish.observability.traces import Tracer


@dataclass
class ResolvedPrompt:
    """Resolved prompt data used for tracing and execution."""

    name: str
    compiled: str
    prompt_client: Any | None = None
    source: str = "fallback"
    version: int | None = None
    labels: list[str] | None = None


def resolve_text_prompt(
    tracer: Tracer | None,
    name: str,
    fallback_template: str,
    **variables: Any,
) -> ResolvedPrompt:
    """Fetch a text prompt from Langfuse, falling back to the local template."""
    compiled_fallback = fallback_template.format(**variables)
    if tracer is None:
        return ResolvedPrompt(name=name, compiled=compiled_fallback)

    prompt = tracer.get_prompt(name, fallback=_to_langfuse_fallback(fallback_template))
    if prompt is None:
        return ResolvedPrompt(name=name, compiled=compiled_fallback)

    try:
        compiled = prompt.compile(**variables)
    except Exception:
        return ResolvedPrompt(name=name, compiled=compiled_fallback)

    source = "fallback" if getattr(prompt, "is_fallback", False) else "langfuse"
    return ResolvedPrompt(
        name=name,
        compiled=compiled,
        prompt_client=prompt,
        source=source,
        version=getattr(prompt, "version", None),
        labels=getattr(prompt, "labels", None),
    )


def _to_langfuse_fallback(template: str) -> str:
    """Convert a Python format string to the double-brace format Langfuse expects."""
    return template.replace("{", "{{").replace("}", "}}")
