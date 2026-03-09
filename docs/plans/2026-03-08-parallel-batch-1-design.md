# Parallel Batch 1 Design — Agent Pool, Validation Gates, Planner

Date: 2026-03-08

Three independent components built in parallel, no cross-dependencies.

## 1. Agent Pool (`src/horse_fish/agents/pool.py`)

Wires together TmuxManager, WorktreeManager, RuntimeAdapter, and Store to manage agent lifecycle.

### API

```python
class AgentPool:
    def __init__(self, store: Store, tmux: TmuxManager, worktree: WorktreeManager)

    async def spawn(self, name: str, runtime: str, model: str, capability: str) -> AgentSlot
        # Lookup runtime in RUNTIME_REGISTRY → create worktree → spawn tmux session → persist to store → return slot

    async def send_task(self, agent_id: str, prompt: str) -> None
        # send_keys to agent's tmux session

    async def check_status(self, agent_id: str) -> AgentState
        # is_alive check → update state if dead → return state

    async def collect_result(self, agent_id: str) -> SubtaskResult
        # capture_pane + get_diff from worktree → build SubtaskResult

    async def release(self, agent_id: str) -> None
        # kill tmux session, remove worktree, update store to idle/dead

    async def list_agents(self) -> list[AgentSlot]
        # fetch from store

    async def cleanup(self) -> int
        # release all dead/idle agents, cleanup old worktrees
```

### Tests (`tests/test_pool.py`)

Mock tmux/worktree/store. Test: spawn lifecycle, send_task, check_status transitions, collect_result, release, cleanup, error handling (spawn failure, dead agent detection).

## 2. Validation Gates (`src/horse_fish/validation/gates.py`)

Quality checks against a worktree path before merge.

### API

```python
@dataclass
class GateResult:
    gate: str           # "ruff-check" | "ruff-format" | "pytest" | "compile"
    passed: bool
    output: str
    duration_seconds: float

class ValidationGates:
    def __init__(self, gates: list[str] | None = None)
        # Default: ["compile", "ruff-check", "pytest"]

    async def run_all(self, worktree_path: str | Path) -> list[GateResult]
        # Run all gates sequentially, return all results (no short-circuit)

    async def run_gate(self, gate: str, worktree_path: str | Path) -> GateResult
        # Run single gate

    def all_passed(self, results: list[GateResult]) -> bool
```

### Gate Implementations

Each gate runs a subprocess in the worktree directory:
- `compile`: `python -m py_compile` on all `.py` files
- `ruff-check`: `ruff check src/ tests/`
- `pytest`: `pytest tests/`

### Tests (`tests/test_validation.py`)

Use real tmp_path with sample Python files. Test pass/fail for each gate type, run_all aggregation.

## 3. Planner (`src/horse_fish/planner/decompose.py`)

Shells out to runtime CLI to decompose a task into a subtask DAG.

### API

```python
class Planner:
    def __init__(self, runtime: str = "claude", model: str | None = None)

    async def decompose(self, task: str, context: str = "") -> list[Subtask]
        # Build prompt → shell out to `claude --print` → parse JSON → return Subtask list

    def _build_prompt(self, task: str, context: str) -> str
        # System prompt instructing LLM to return JSON array of subtasks
        # Each subtask: {description, deps, files_hint}

    def _parse_response(self, raw: str) -> list[Subtask]
        # Extract JSON from response, validate, create Subtask objects
```

### Tests (`tests/test_planner.py`)

Mock subprocess. Test prompt building, JSON response parsing (valid, malformed, empty), Subtask creation.

## Conventions

All components follow project conventions from CLAUDE.md:
- Ruff: py312, line-length 120, rules E/F/W/I/UP/B
- Pydantic models for data classes
- Async by default
- pytest + pytest-asyncio for tests
