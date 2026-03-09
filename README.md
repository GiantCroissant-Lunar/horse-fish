# Horse-Fish

Agent swarm orchestration framework. Coordinates multiple AI coding agents (Claude, Copilot, Pi, Kimi, OpenCode) to work on tasks in parallel.

## Quick Start

```bash
# Install
pip install -e ".[dev]"

# Set up secrets
cp infra/.env.example infra/.env   # fill in API keys
source infra/setup-env.sh

# Run a task
hf run "fix the broken test in tests/test_utils.py" --runtime pi

# Monitor (in another terminal)
hf dash

# Optional: start Langfuse and verify config
task langfuse:up
hf env-check
```

## Architecture

```
Orchestrator (state machine)
  plan → dispatch → execute → review → merge → learn

Each agent runs in its own:
  - tmux pane (interactive session)
  - git worktree (isolated branch)
  - runtime (claude, pi, kimi, copilot, opencode)
```

### Core Components

| Component | Purpose |
|-----------|---------|
| **Orchestrator** | State machine driving run lifecycle |
| **Planner** | LLM decomposes task into DAG of subtasks |
| **SmartPlanner** | Classifies complexity (SOLO/SMALL/MEDIUM/LARGE), uses lessons |
| **Dispatcher** | Market-first agent selection by fitness |
| **Agent Pool** | Manages agent subprocesses in tmux + worktrees |
| **Merge Queue** | FIFO merge with conflict resolution |
| **Validation Gates** | Compile check, tests, lint before merge |
| **Memory** | SQLite-vec + Cognee knowledge graph for cross-session learning |
| **Dashboard** | Textual TUI for real-time swarm monitoring |

## CLI Commands

```bash
hf run "task"          # Submit task to swarm
hf dash                # Live TUI dashboard (read-only)
hf status              # Show active agents
hf logs                # View agent tmux output
hf merge               # Process merge queue
hf clean               # Kill all agents, remove worktrees
hf smoke               # End-to-end smoke test
```

## Available Runtimes

| Runtime | CLI | Model | Notes |
|---------|-----|-------|-------|
| Claude | `claude` | claude-sonnet-4.6 | Best instruction following |
| Pi | `pi` | qwen3.5-plus | Free (Dashscope), reliable |
| Kimi | `kimi` | kimi-for-coding | Free (Moonshot), strong coding |
| Copilot | `copilot` | gpt-5.4 | Fast code generation |
| OpenCode | `opencode` | qwen3.5-plus | Free (Alibaba) |

## Development

```bash
task test              # Run tests
task lint              # Lint + format
task smoke             # End-to-end smoke test
task dash              # Launch dashboard
task setup             # Install dev + all optional deps
```

## Observability

Langfuse is optional but now useful for normal `hf run` executions, not just local experiments.

```bash
# Install the SDK
pip install -e ".[observability]"

# Start the local Langfuse stack
task langfuse:up

# Open Langfuse, create a project, and copy the API keys
# http://localhost:3000

# Add keys to infra/.env or your shell
export LANGFUSE_HOST=http://localhost:3000
export LANGFUSE_PUBLIC_KEY=pk-lf-...
export LANGFUSE_SECRET_KEY=sk-lf-...

# Confirm the CLI sees the config
hf env-check
```

Current tracing covers:

- One root trace per `hf run`
- State spans for planning, executing, reviewing, and merging
- Subtask operation spans for dispatch, result collection, review, merge queueing, and merge
- Generation observations for `smart_planner.classify` and `planner.decompose`
- Generation observations for agent task prompts, fix prompts, and raw prompt sends
- Langfuse-managed text prompts with local fallbacks for planner classify/decompose prompts
- Langfuse-managed text prompts with local fallbacks for agent task/fix prompts
- Trace scores for run success, completed/failed subtasks, review gate pass rate, retries, and merge conflicts
- Run-level metadata such as runtime, model, max agents, subtask counts, and final status

Recommended next Langfuse improvements:

- Add planner-quality scores and evaluator feedback loops
- Add finer-grained spans around retries, stall recovery, and agent readiness

## Project Structure

```
src/horse_fish/
├── orchestrator/    # State machine + run lifecycle
├── planner/         # Task decomposition (LLM -> DAG)
├── dispatch/        # Agent selection + bidding
├── agents/          # Pool, tmux, worktree, runtime adapters
├── merge/           # Merge queue + conflict resolution
├── memory/          # Memvid + Cognee knowledge graph
├── store/           # SQLite persistence
├── validation/      # Pre-merge quality gates
├── dashboard/       # Textual TUI (read-only observer)
└── cli.py           # Click CLI entry point

infra/
├── .env.example     # Required environment variables
├── setup-env.sh     # Load secrets into shell + tmux
└── docker-compose.yml  # Langfuse observability stack
```

## Design Principles

- **No heavy frameworks** — simple Python state machines, no LangGraph/Akka
- **SQLite for everything** — state, tasks, agents, metrics, memory
- **Tmux for agents** — interactive sessions, not stdin/stdout piping
- **Git worktree isolation** — each agent works on its own branch
- **Dashboard is optional** — swarm runs fine without it

## Optional Dependencies

```bash
pip install -e ".[memory]"       # Cognee knowledge graph
pip install -e ".[dashboard]"    # Textual TUI dashboard
pip install -e ".[observability]" # Langfuse tracing
```
