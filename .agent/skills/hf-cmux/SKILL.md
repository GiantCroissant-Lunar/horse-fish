---
name: hf-cmux
description: Use when launching, monitoring, or stopping horse-fish agent swarm runs via cmux. Triggers on "start swarm", "run hf", "monitor agents", "watch dashboard", or any task needing horse-fish with visual monitoring.
---

# Horse-Fish Swarm Monitor — cmux

## Overview

Launch and monitor horse-fish agent swarm runs via cmux. Creates a split workspace
with `hf run` (execution) and `hf dash` (TUI dashboard) visible simultaneously.
Claude Code can `read-screen` to inspect agent progress without switching windows.

## Starting a Run

### Step 1: Create cmux workspace with split panes

```bash
cmux new-workspace
WS=$(cmux list-workspaces | grep -v selected | grep -v "Claude Code" | tail -1 | awk '{print $1}')
cmux rename-workspace --workspace $WS "Horse-Fish"
PANES=$(cmux list-panes --workspace $WS)
TOP_PANE=$(echo "$PANES" | head -1 | awk '{print $1}')

# Split horizontally for dashboard pane
cmux new-pane --workspace $WS --direction horizontal
PANES=$(cmux list-panes --workspace $WS)
BOTTOM_PANE=$(echo "$PANES" | tail -1 | awk '{print $1}')
```

### Step 2: Load environment and start run (top pane)

```bash
cmux focus-pane --pane $TOP_PANE --workspace $WS > /dev/null
cmux send --workspace $WS "cd $(pwd) && source .env 2>/dev/null; export DASHSCOPE_API_KEY INCEPTION_API_KEY ZAI_API_KEY\n"
cmux send --workspace $WS "hf run \"YOUR_TASK_HERE\" --runtime pi --planner-runtime pi\n"
```

Replace `YOUR_TASK_HERE` with the actual task description. Adjust `--runtime` as needed (pi, claude, droid).

### Step 3: Start dashboard (bottom pane)

```bash
cmux focus-pane --pane $BOTTOM_PANE --workspace $WS > /dev/null
cmux send --workspace $WS "cd $(pwd) && hf dash\n"
```

## Monitoring

### Read dashboard TUI state

Shows pipeline progress, agent table, subtask status:

```bash
cmux focus-pane --pane $BOTTOM_PANE --workspace $WS > /dev/null
cmux read-screen --workspace $WS --lines 30
```

**What to look for in dashboard output:**
- `PipelineBar`: current state (planning → EXECUTING → reviewing → merging)
- `AgentTable`: agent names, runtimes, states (idle/busy/error)
- `SubtaskTable`: subtask descriptions, states (pending/running/done/failed)

### Read run output / orchestrator logs

```bash
cmux focus-pane --pane $TOP_PANE --workspace $WS > /dev/null
cmux read-screen --workspace $WS --lines 20
```

### Read scrollback for full history

```bash
cmux focus-pane --pane $TOP_PANE --workspace $WS > /dev/null
cmux read-screen --workspace $WS --scrollback --lines 100
```

## Polling Pattern

To check progress periodically from Claude Code without user intervention:

```bash
# Quick status check — read last 10 lines of dashboard
cmux focus-pane --pane $BOTTOM_PANE --workspace $WS > /dev/null
cmux read-screen --workspace $WS --lines 10

# Check if run completed — look for final state in top pane
cmux focus-pane --pane $TOP_PANE --workspace $WS > /dev/null
cmux read-screen --workspace $WS --lines 5
```

**Completion indicators in top pane:**
- `Run run-...: completed` — success
- `Run run-...: failed` — failure, check logs

## Stopping

```bash
# Kill processes and close workspace
cmux close-workspace --workspace $WS
```

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `.env` not found | Create `.env` in repo root with keys. Run `hf env-check` to verify. |
| Dashboard blank | Ensure `.horse-fish/state.db` exists. Run `hf status` first. |
| Agent panes not visible | Agents run in tmux (not cmux). Use `hf logs` or dashboard AgentLog. |
| Keys missing in run pane | Run `source .env && export DASHSCOPE_API_KEY ...` before `hf run`. |
| `hf dash` import error | Install dashboard extra: `pip install -e ".[dashboard]"` |
| Run hangs at planning | Check `hf env-check` — planner needs DASHSCOPE_API_KEY for Pi runtime. |
