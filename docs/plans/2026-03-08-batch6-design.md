# Batch 6 Design — Self-Hosting Agent-Facing Improvements

## Overview

Three features to close the gap between horse-fish and overstory: ready detection, prompt injection, runtime fixes. After this batch, horse-fish can drive its own development.

## 1. Ready Detection

Add ready detection to RuntimeAdapter protocol so AgentPool waits for the runtime to be ready before sending tasks.

**Protocol change:**
- `ready_pattern: str` — regex matching the runtime's ready prompt
- `ready_timeout_seconds: int` — max wait before giving up (default 30s)

**Patterns:**
- Claude: `r"[❯>]\s*$"` (shows ❯ or >)
- Pi: `r"[>›]\s*$"`
- Copilot: `r"[>]\s*$"`
- OpenCode: `r"[>]\s*$"`

**AgentPool change:**
- After tmux spawn, call `_wait_for_ready(slot)` which polls `capture_pane` every 1s
- If timeout, raise error (agent didn't start)
- Only then mark agent as idle and return slot

## 2. Agent Prompt Template

New module `agents/prompt.py` that wraps task descriptions with project context.

**Template includes:**
- Worktree path and branch name
- CLAUDE.md content (loaded once at pool init from repo root)
- Task description
- Rules: use pytest, commit when done, stay focused

**AgentPool.send_task() change:**
- Accept optional `project_context: str` parameter
- Wrap prompt through `build_prompt(task, slot, context)` before sending to tmux

## 3. Pi Runtime Fixes

- PiRuntime.build_env() returns `{"DASHSCOPE_API_KEY": os.environ.get("DASHSCOPE_API_KEY", "")}`
- PiRuntime.build_spawn_command() adds `--provider dashscope` when model is dashscope-hosted

## Task Assignment

| Agent | Runtime | Task | Independent? |
|-------|---------|------|-------------|
| ready-detect | Pi/qwen3.5-plus | Ready detection | Yes |
| prompt-template | Pi/qwen3.5-plus | Prompt template | Yes |
| runtime-fix | Pi/qwen3.5-plus | Pi env/command | Yes |
