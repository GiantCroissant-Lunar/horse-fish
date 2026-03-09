# Horse-Fish

Agent swarm orchestration framework. Python 3.12+. Built from scratch using lessons from tentacle-punch and giant-isopod.

## Architecture

Horse-fish is a standalone agent swarm system. It orchestrates multiple AI coding agents (claude, copilot, pi, opencode) to work on tasks in parallel.

### Core Components

1. **Orchestrator** — State machine: plan → dispatch → execute → review → merge → learn
2. **Planner** — LLM decomposes task into DAG of subtasks
3. **Dispatcher** — Market-first: agents bid on tasks by fitness (capability + load + history)
4. **Agent Pool** — Manages agent subprocesses in tmux panes with git worktree isolation
5. **Merge Queue** — FIFO merge with conflict resolution
6. **Memory** — SQLite-vec vector search + knowledge store for cross-session learning
7. **Validation Gates** — Compile check, tests, lint before merge

### Design Principles

- **No heavy frameworks**: No LangGraph, no Akka. Simple Python state machines
- **SQLite for everything**: Mail, tasks, artifacts, metrics, memory — all in SQLite
- **Tmux for agents**: Each agent runs in its own tmux pane (not piped subprocesses)
- **Git worktree isolation**: Each agent works on its own branch in its own directory
- **Agents write files**: Interactive tmux sessions, not stdin/stdout piping

## Project Structure

```
src/horse_fish/
├── orchestrator/    # State machine + run lifecycle
├── planner/         # Task decomposition (LLM → DAG)
├── dispatch/        # Market-first agent selection + bidding
├── agents/          # Agent pool, tmux management, runtime adapters
├── merge/           # Merge queue + conflict resolution
├── memory/          # SQLite-vec embeddings + knowledge graph
├── store/           # SQLite persistence (mail, tasks, artifacts)
├── validation/      # Pre-merge quality gates
└── cli.py           # Click CLI entry point
```

## Conventions

- **Ruff**: py312, line-length 120, rules E/F/W/I/UP/B
- **Tests**: pytest + pytest-asyncio in `tests/`
- **Types**: Use Pydantic models for data classes
- **SQLite for state**: All persistent state in SQLite (not YAML, not JSON files)
- **Async by default**: Use asyncio for subprocess management and I/O

## Available Agent Runtimes

| Runtime | CLI | Model | Strengths |
|---------|-----|-------|-----------|
| Claude Code | `claude` | claude-sonnet-4.6 | Best instruction following, planning |
| Copilot | `copilot` | gpt-5.4 | Fast, good code generation |
| Pi | `pi` | kimi-for-coding | Free tier, good for bulk work |
| OpenCode | `opencode` | qwen3.5-plus | Free tier (Alibaba), fast |
| Kimi | `kimi` | kimi-for-coding | Free tier (Moonshot), strong coding |
| Droid | `droid` | glm-4.7 | Z.AI GLM model, fast and cheap |

## Commands

```bash
pip install -e ".[dev]"     # Install in dev mode
hf run "task description"   # Submit task to swarm
pytest                      # Run tests
ruff check src/ tests/      # Lint
ruff format src/ tests/     # Format
```
