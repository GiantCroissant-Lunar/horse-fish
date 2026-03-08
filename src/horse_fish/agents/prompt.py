"""Prompt template module — wraps task descriptions with project context."""

from __future__ import annotations

PROMPT_TEMPLATE = """You are an agent in the horse-fish swarm working in an isolated git worktree.

## Worktree Information
- Worktree path: {worktree_path}
- Branch: {branch}

{project_context_section}
## Task Description
{task}

## Rules
1. Run pytest to verify your changes pass tests.
2. Commit your changes when done.
3. Stay focused on the task at hand.
4. Do not modify files outside your assigned scope.
"""


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
