---
name: swarm-cmux
description: Use when starting, stopping, or monitoring tentacle-punch agent swarm processes. Triggers on "start swarm", "run swarm", "launch agents", or any task requiring the orchestrator and dynamic agent pool.
---

# Swarm Management — pm2 + cmux

## Overview

**pm2** manages the orchestrator process. **cmux** provides log monitoring. Agents (coder, reviewer, etc.) spawn dynamically as subprocesses of the orchestrator via `AgentPoolManager` — they are NOT separate pm2 processes.

## Architecture

```
pm2: orchestrator (port 8000, supervisor: kimi)
     └── AgentPoolManager (spawns on-demand per task)
         ├── coder (port 9100+)
         ├── reviewer (port 9101+)
         └── ... (any persona from personas/*.yaml)
```

**Config:** `ecosystem.config.cjs` — only the orchestrator process.

**Graph flow:** `ws_plan → ws_spawn → ws_execute → ws_teardown → ws_learn → END`

## Starting the Swarm

### Step 1: Start orchestrator via pm2

```bash
cd /Users/apprenticegc/Work/lunar-horse/yokan-projects/tentacle-punch

# First time (or after config changes)
pm2 start ecosystem.config.cjs

# Restart existing (picks up code changes)
pm2 restart orchestrator
```

### Step 2: Verify orchestrator is healthy

```bash
curl -s http://localhost:8000/health
# Expected: {"ok":true,"checks":{"nacos":{"status":"skipped",...},"orchestrator":{"status":"up"}}}
```

### Step 3: Create cmux log dashboard (single pane)

```bash
cmux new-workspace
WS=$(cmux list-workspaces | grep -v selected | grep -v "Claude Code" | tail -1 | awk '{print $1}')
cmux rename-workspace --workspace $WS "Swarm Logs"
cmux list-panes --workspace $WS
# Note the pane ref (e.g., pane:17)

PANE_ORCH=pane:17   # adjust from list-panes output
cmux focus-pane --pane $PANE_ORCH --workspace $WS > /dev/null
cmux send --workspace $WS "pm2 logs orchestrator --lines 30\n"
```

### Step 4: Verify logs are flowing

```bash
cmux focus-pane --pane $PANE_ORCH --workspace $WS > /dev/null
cmux read-screen --workspace $WS --lines 5
```

Look for "Uvicorn running on http://0.0.0.0:8000" message.

## Sending a Task

From any terminal (your Claude Code session):

```bash
uv run tentacle-punch run \
  --work-dir . \
  --test-cmd "uv run pytest -x -q" \
  --max-retries 2 \
  --timeout 900 \
  "Your task description here"
```

Add `--dry-run` to preview the A2A payload without sending.

**What happens:**
1. Planner (kimi) decomposes task into subtasks, picks agent roles from persona library
2. `AgentPoolManager.spawn()` starts agent subprocesses on ports 9100+
3. Health check polls `/.well-known/agent-card.json` until ready
4. Executor runs subtasks through code→test→review loop (parallel when deps allow)
5. `AgentPoolManager.shutdown()` kills all agent subprocesses (SIGTERM/SIGKILL)

## Monitoring

```bash
# Read current orchestrator output
cmux focus-pane --pane $PANE_ORCH --workspace $WS > /dev/null
cmux read-screen --workspace $WS --lines 20

# Read scrollback for full history
cmux read-screen --workspace $WS --scrollback --lines 100

# Quick non-streaming log snapshot
pm2 logs orchestrator --lines 50 --nostream
```

**Key log lines to watch for:**
- `Spawning coder on port 9100` — pool is spawning agents
- `All agents healthy` — agents ready, execution starting
- `Agent pool torn down` — clean shutdown after task
- `No learner agent configured` — normal if ArcadeDB not running

## Process Management (pm2)

```bash
# View status
pm2 list

# Restart after code changes
pm2 restart orchestrator

# View logs (non-streaming snapshot)
pm2 logs orchestrator --lines 30 --nostream

# Stop orchestrator
pm2 stop orchestrator

# Delete (remove from pm2)
pm2 delete all

# Clean up stale pm2 processes from old config
pm2 delete all && pm2 start ecosystem.config.cjs
```

## Stopping

```bash
# Stop orchestrator (also kills dynamic agent subprocesses via atexit handler)
pm2 stop orchestrator

# Close cmux log dashboard
cmux close-workspace --workspace $WS
```

## Common Issues

| Issue | Fix |
|-------|-----|
| Agents not spawning | Check orchestrator logs: `pm2 logs orchestrator --lines 50 --nostream` |
| Health check timeout | Agent-card endpoint must return 200. Check agent persona YAML is valid |
| Stale coder/reviewer in pm2 | Old 3-process config. Run `pm2 delete all && pm2 start ecosystem.config.cjs` |
| Port 9100+ in use | Previous run didn't clean up. `lsof -i :9100` to find and kill stale process |
| Code changes not reflected | `pm2 restart orchestrator` — pm2 doesn't auto-reload |
| Env vars missing | pm2 loads from ecosystem.config.cjs `env` block — check `.env` file |
| Stale worktree branches | `git worktree prune` in the repo |
| ArcadeDB 403 errors in logs | ArcadeDB not configured — non-blocking, learner step skipped |
| Langfuse KeyboardInterrupt on shutdown | Cosmetic — Langfuse atexit handler race condition, non-blocking |
