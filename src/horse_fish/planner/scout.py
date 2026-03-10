"""Scout phase — codebase context gathering for enriched planning."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from horse_fish.models import ContextBrief, FileContext

logger = logging.getLogger(__name__)

SCOUT_PROMPT_TEMPLATE = (
    "You are a codebase scout. Your job is to explore this project and produce a structured "
    "context brief for a planning agent that will decompose a task into subtasks.\n"
    "\n"
    "TASK: {task}\n"
    "\n"
    "PROJECT CONTEXT:\n"
    "{project_context}\n"
    "\n"
    "INSTRUCTIONS:\n"
    "1. Read the project structure (file tree, key modules)\n"
    "2. Find files relevant to this task using grep/glob\n"
    "3. Read those files to understand patterns and conventions\n"
    "4. Identify what the task will need to change and what depends on those changes\n"
    "5. Propose acceptance criteria — how will we know the task is done?\n"
    "6. Note any risks (missing tests, complex dependencies, hot paths)\n"
    "\n"
    "OUTPUT: Reply with ONLY a JSON object matching this schema:\n"
    "{{\n"
    '  "relevant_files": [{{"path": "src/example.py", "purpose": "Main module", "line_count": 100}}],\n'
    '  "patterns": ["async by default", "Pydantic models for data"],\n'
    '  "dependencies": ["changes to models.py require updating engine.py"],\n'
    '  "acceptance_criteria": ["pytest passes", "new function returns expected output"],\n'
    '  "risks": ["no existing tests for this module"],\n'
    '  "suggested_approach": "Add new model class, update planner to use it"\n'
    "}}\n"
    "\n"
    "Be thorough but concise. Focus on what the planner needs to make good decomposition decisions."
)

SCOUT_PROMPT_NAME = "scout-context-brief"

# File extensions to include in programmatic scout
_SOURCE_EXTENSIONS = {".py", ".toml", ".cfg", ".yaml", ".yml", ".json", ".md"}

# Directories to skip
_SKIP_DIRS = {"__pycache__", ".git", ".horse-fish", "node_modules", ".venv", ".tox", ".eggs", ".overstory"}

# Max files to include in the brief
_MAX_FILES = 30


def programmatic_scout(task: str, repo_root: Path | None = None) -> ContextBrief:
    """Gather codebase context without spawning an agent.

    Reads CLAUDE.md and scans source files to produce a lightweight ContextBrief.
    Used for SOLO mode and as fallback when scout agent fails.
    """
    root = repo_root or Path.cwd()

    # Read project context from CLAUDE.md
    patterns: list[str] = []
    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        try:
            content = claude_md.read_text(encoding="utf-8")
            patterns = _extract_patterns(content)
        except Exception as exc:
            logger.warning("Failed to read CLAUDE.md: %s", exc)

    # Scan source files
    relevant_files = _scan_source_files(root, task)

    # Basic acceptance criteria
    acceptance_criteria = ["pytest passes", "ruff check passes"]

    return ContextBrief(
        relevant_files=relevant_files,
        patterns=patterns,
        acceptance_criteria=acceptance_criteria,
    )


def format_brief_for_prompt(brief: ContextBrief) -> str:
    """Format a ContextBrief as text suitable for injection into LLM prompts."""
    sections: list[str] = []

    if brief.relevant_files:
        file_lines = [f"  - {f.path}: {f.purpose}" for f in brief.relevant_files[:15]]
        sections.append("Relevant files:\n" + "\n".join(file_lines))

    if brief.patterns:
        sections.append("Codebase patterns:\n" + "\n".join(f"  - {p}" for p in brief.patterns))

    if brief.dependencies:
        sections.append("Dependencies:\n" + "\n".join(f"  - {d}" for d in brief.dependencies))

    if brief.acceptance_criteria:
        sections.append("Acceptance criteria:\n" + "\n".join(f"  - {c}" for c in brief.acceptance_criteria))

    if brief.risks:
        sections.append("Risks:\n" + "\n".join(f"  - {r}" for r in brief.risks))

    if brief.suggested_approach:
        sections.append("Suggested approach: " + brief.suggested_approach)

    return "\n\n".join(sections) if sections else "No codebase context available."


def parse_scout_output(raw_output: str) -> ContextBrief | None:
    """Extract a ContextBrief JSON from raw scout agent tmux output.

    Looks for a JSON object in the output (possibly inside markdown code fences).
    Returns None if no valid JSON is found.
    """
    if not raw_output:
        return None

    # Try to find JSON in markdown code fences first
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw_output)
    if fence_match:
        text = fence_match.group(1).strip()
    else:
        # Try to find a bare JSON object (first { to last })
        start = raw_output.find("{")
        end = raw_output.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        text = raw_output[start : end + 1]

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Failed to parse scout JSON output")
        return None

    if not isinstance(data, dict):
        return None

    try:
        return ContextBrief.model_validate(data)
    except Exception as exc:
        logger.warning("Scout output failed ContextBrief validation: %s", exc)
        return None


def _extract_patterns(claude_md_content: str) -> list[str]:
    """Extract convention patterns from CLAUDE.md content."""
    patterns: list[str] = []
    lines = claude_md_content.splitlines()
    for line in lines:
        stripped = line.strip()
        # Look for bullet points that describe conventions
        if stripped.startswith("- **") and "**:" in stripped:
            # Extract "key: value" patterns like "- **Ruff**: py312, line-length 120"
            patterns.append(stripped.lstrip("- ").replace("**", ""))
        elif stripped.startswith("- ") and any(
            kw in stripped.lower() for kw in ("default", "always", "convention", "use ", "prefer")
        ):
            patterns.append(stripped.lstrip("- "))
    return patterns[:10]  # Cap at 10 patterns


def _scan_source_files(root: Path, task: str) -> list[FileContext]:
    """Scan source files and rank by relevance to the task."""
    task_lower = task.lower()
    task_words = set(task_lower.split())
    # Remove common stop words
    task_words -= {"the", "a", "an", "to", "in", "for", "and", "or", "is", "it", "of", "with", "this", "that"}

    candidates: list[tuple[float, FileContext]] = []

    src_dir = root / "src"
    scan_dirs = [src_dir] if src_dir.exists() else [root]

    for scan_dir in scan_dirs:
        if not scan_dir.exists():
            continue
        for path in scan_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in _SOURCE_EXTENSIONS:
                continue
            if any(skip in path.parts for skip in _SKIP_DIRS):
                continue

            rel_path = str(path.relative_to(root))
            try:
                line_count = sum(1 for _ in path.open(encoding="utf-8"))
            except Exception:
                line_count = None

            # Score relevance based on path/name overlap with task words
            path_lower = rel_path.lower()
            score = sum(1.0 for w in task_words if w in path_lower)

            # Read first few lines for purpose
            purpose = _infer_purpose(path)

            # Boost if purpose words match task
            if purpose:
                purpose_lower = purpose.lower()
                score += sum(0.5 for w in task_words if w in purpose_lower)

            fc = FileContext(path=rel_path, purpose=purpose or rel_path, line_count=line_count)
            candidates.append((score, fc))

    # Sort by score descending, take top N
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [fc for _, fc in candidates[:_MAX_FILES]]


def _infer_purpose(path: Path) -> str:
    """Read the first docstring or comment from a file to infer its purpose."""
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#!"):
                    continue
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    # Single-line docstring
                    doc = stripped.strip("\"'").strip()
                    if doc:
                        return doc[:120]
                    # Multi-line: read next line
                    next_line = next(f, "").strip().strip("\"'").strip()
                    return next_line[:120] if next_line else ""
                if stripped.startswith("# "):
                    return stripped[2:][:120]
                break
    except Exception:
        pass
    return ""
