"""Prompt template module — wraps task descriptions with project context."""

from __future__ import annotations

from horse_fish.observability.prompts import ResolvedPrompt, resolve_text_prompt
from horse_fish.observability.traces import Tracer

PROMPT_TEMPLATE = """You are an agent in the horse-fish swarm working in an isolated git worktree.

## Worktree Information
- Worktree path: {worktree_path}
- Branch: {branch}

{project_context_section}
## Memory Commands
Before starting work, search for relevant past knowledge:
  hf memory search 'keywords about your task'

After completing your work, record what you learned:
  hf memory store 'description of what was built/learned' --domain general --tags 'tag1,tag2'

## Task Description
{task}

## Completion Checklist
You MUST complete ALL of the following before committing:
1. Implement every deliverable mentioned in the task description above — code, tests, CLI commands, etc.
2. If the task asks for tests, create them. If it specifies a test file name, use that exact name.
3. Run `ruff format src/ tests/` to fix line length and formatting.
4. Run `ruff check --fix src/ tests/` to fix lint errors.
5. Run `pytest tests/ -x -q` to verify all tests pass.
6. Commit your changes: `git add --all && git commit -m "description"`.
7. Do not modify files outside your assigned scope.
"""

TASK_PROMPT_NAME = "agent-task-instructions"


def build_prompt(
    task: str,
    worktree_path: str,
    branch: str,
    project_context: str | None = None,
) -> str:
    """Build a prompt wrapped with worktree info and optional project context.

    Args:
        task: The task description to send to the agent.
        worktree_path: Path to the agent's git worktree.
        branch: Branch name the agent is working on.
        project_context: Optional project-specific conventions/instructions.

    Returns:
        A formatted prompt string ready to send to the agent.
    """
    if project_context:
        project_context_section = f"## Project Conventions\n{project_context}\n\n"
    else:
        project_context_section = ""

    return PROMPT_TEMPLATE.format(
        worktree_path=worktree_path,
        branch=branch,
        project_context_section=project_context_section,
        task=task,
    )


def resolve_task_prompt(
    tracer: Tracer | None,
    task: str,
    worktree_path: str,
    branch: str,
    project_context: str | None = None,
) -> ResolvedPrompt:
    """Resolve the agent task prompt from Langfuse or a local fallback."""
    if project_context:
        project_context_section = f"## Project Conventions\n{project_context}\n\n"
    else:
        project_context_section = ""

    return resolve_text_prompt(
        tracer,
        TASK_PROMPT_NAME,
        PROMPT_TEMPLATE,
        task=task,
        worktree_path=worktree_path,
        branch=branch,
        project_context_section=project_context_section,
    )


FIX_PROMPT_TEMPLATE = """Your previous changes failed the following quality gates:

{gate_output}

## Worktree Information
- Worktree path: {worktree_path}
- Branch: {branch}

## Instructions
1. Fix ALL issues listed above.
2. Run `ruff check --fix src/ tests/` and `ruff format src/ tests/`.
3. Run unit tests only (exclude slow integration/e2e tests):
   `pytest tests/ -x -q --ignore=tests/test_e2e.py --ignore=tests/test_smoke.py \
     --ignore=tests/test_integration.py --ignore=tests/test_smart_integration.py`
4. Commit your fixes when done.
"""

FIX_PROMPT_NAME = "agent-fix-instructions"


def build_fix_prompt(
    gate_output: str,
    worktree_path: str,
    branch: str,
) -> str:
    """Build a prompt telling the agent to fix gate failures."""
    return FIX_PROMPT_TEMPLATE.format(
        gate_output=gate_output,
        worktree_path=worktree_path,
        branch=branch,
    )


def resolve_fix_prompt(
    tracer: Tracer | None,
    gate_output: str,
    worktree_path: str,
    branch: str,
) -> ResolvedPrompt:
    """Resolve the agent fix prompt from Langfuse or a local fallback."""
    return resolve_text_prompt(
        tracer,
        FIX_PROMPT_NAME,
        FIX_PROMPT_TEMPLATE,
        gate_output=gate_output,
        worktree_path=worktree_path,
        branch=branch,
    )
