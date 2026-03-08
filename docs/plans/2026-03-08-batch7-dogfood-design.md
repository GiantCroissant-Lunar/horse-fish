# Batch 7 Design — Dog-Food Smoke Test + Live Run

## Overview

Prove horse-fish works end-to-end by running a smoke test with mocked planner + real agents, then a live `hf run` on the horse-fish repo itself.

## Part 1: Smoke Test

Full orchestrator loop with real tmux + worktrees + Pi agent, mocked planner only.

1. Create temp git repo with initial commit
2. Mock planner returns single subtask: "Create hello.py with print('hello')"
3. Real AgentPool spawns Pi/qwen3.5-plus in tmux
4. Ready detection waits for Pi prompt
5. Prompt template wraps task with context
6. Agent executes, creates file, commits
7. Orchestrator detects completion, runs validation gates
8. Merge queue merges worktree branch into main
9. Assert: hello.py exists on main

## Part 2: Live Run

```bash
hf run "Add a __version__ string to src/horse_fish/__init__.py" --runtime pi --max-agents 1
```

## Not Swarmed

This is a manual testing session, not an agent swarm batch.
