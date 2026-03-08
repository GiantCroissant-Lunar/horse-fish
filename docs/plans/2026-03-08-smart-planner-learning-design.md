# Smart Planner + Learning System Design

Date: 2026-03-08
Status: Design
Predecessor lessons: giant-isopod (progressive decomposition, market bidding, 4-layer memory), tentacle-punch (quality gates, circuit breaker, coordinator messages), overstory (reliability engineering, SQLite mail, tmux)

## Problem

1. **Planner over-decomposes** — dog-food test showed "add version string" became 3 subtasks (implement, test, commit). The prompt says "aim for 3-8 subtasks", so the LLM always produces 3+.
2. **No learning feedback** — memory store exists but only stores raw run results. No pattern extraction, no feedback into future planning.
3. **Run model is thin** — Run exists in models.py but has no persistence lifecycle, no lessons, no complexity metadata.

## Design

### 1. Smart Planner (replaces current Planner.decompose)

**Two-step approach: classify then optionally decompose.**

#### Step 1: Complexity Classification

Before decomposing, ask the LLM a focused question:

```
Given this task, estimate complexity:
- SOLO: Single file or tightly coupled change. One agent handles everything.
- TRIO: 2-4 files across 1-2 components. Needs decomposition but minimal.
- SQUAD: 5+ files, multiple components, needs parallel work.

Task: {task}
Context: {context}

Reply with ONLY one word: SOLO, TRIO, or SQUAD.
```

- **SOLO** → skip decomposition. Create single subtask wrapping the entire task.
- **TRIO/SQUAD** → proceed to decomposition with adjusted prompt.

#### Step 2: Decomposition (TRIO/SQUAD only)

Replace current prompt with:

```
Decompose this task into the MINIMUM number of subtasks needed for parallel execution.

Rules:
- Return 1 subtask if the task can be done by a single agent
- Never create subtasks for: committing, testing, reviewing, or formatting (agents handle these)
- Each subtask must produce a code change (a git diff), not just analysis
- Maximum {max_subtasks} subtasks

Context from previous runs: {lessons}

Task: {task}
```

Where `max_subtasks` is:
- TRIO → 3
- SQUAD → 8

#### Step 3: Ceremony Stripping

Hard-coded post-filter removes subtasks matching patterns:
- Description contains only "commit", "test", "review", "format", "lint", "verify"
- Description starts with "Write tests for" when a sibling subtask already covers the same files

If stripping reduces to 0 subtasks, fall back to single subtask wrapping the whole task.

#### Implementation

New class `SmartPlanner` wrapping existing `Planner`:

```python
# src/horse_fish/planner/smart.py

class TaskComplexity(StrEnum):
    solo = "SOLO"
    trio = "TRIO"
    squad = "SQUAD"

class SmartPlanner:
    def __init__(self, planner: Planner, memory: LessonStore | None = None):
        self._planner = planner
        self._memory = memory

    async def decompose(self, task: str, context: str = "") -> list[Subtask]:
        # 1. Query lessons for similar tasks
        lessons = await self._get_lessons(task) if self._memory else ""

        # 2. Classify complexity
        complexity = await self._classify(task, context, lessons)

        # 3. Solo → single subtask
        if complexity == TaskComplexity.solo:
            return [Subtask.create(task)]

        # 4. Decompose with cap
        max_subtasks = 3 if complexity == TaskComplexity.trio else 8
        subtasks = await self._planner.decompose(task, context)

        # 5. Strip ceremony
        subtasks = self._strip_ceremony(subtasks)

        # 6. Cap
        subtasks = subtasks[:max_subtasks]

        # 7. Fallback
        if not subtasks:
            return [Subtask.create(task)]

        return subtasks
```

### 2. Lesson Store (pattern B: outcomes + patterns)

New SQLite table for structured lessons learned from completed runs.

#### Schema

```sql
CREATE TABLE IF NOT EXISTS lessons (
    id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES runs(id),
    category TEXT NOT NULL,  -- 'planner', 'dispatch', 'merge', 'agent'
    pattern TEXT NOT NULL,   -- short key: 'over_decomposed', 'merge_conflict', 'agent_stalled'
    content TEXT NOT NULL,   -- human-readable lesson
    task_signature TEXT,     -- normalized task description for matching
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lessons_category ON lessons(category);
CREATE INDEX IF NOT EXISTS idx_lessons_pattern ON lessons(pattern);
```

#### Categories and Patterns

| Category | Pattern | When Stored | Content Example |
|----------|---------|-------------|-----------------|
| planner | over_decomposed | Subtask count > files touched | "Task 'add version' was split into 3 subtasks but only touched 1 file. Should be SOLO." |
| planner | under_decomposed | Single subtask failed, task was complex | "Task 'refactor auth' failed as SOLO. Should be TRIO or SQUAD." |
| dispatch | agent_stalled | Agent hit stall timeout | "Pi agent stalled on test-writing task after 5min. Prefer claude for test tasks." |
| dispatch | runtime_mismatch | Agent failed, different runtime succeeded on retry | "copilot failed on async code. claude succeeded." |
| merge | conflict | Merge conflict detected | "Files src/models.py and src/store.py conflict when edited in parallel." |
| agent | no_diff | Agent produced output but no file changes | "Pi agent on task X produced text but no diff." |

#### Lesson Extraction

After each run completes (success or failure), `_learn()` analyzes the run and extracts lessons:

```python
# src/horse_fish/memory/lessons.py

class LessonStore:
    def __init__(self, store: Store): ...

    async def extract_lessons(self, run: Run) -> list[Lesson]:
        lessons = []

        # Over-decomposition detection
        if len(run.subtasks) > 1:
            files_touched = set()
            for s in run.subtasks:
                if s.result and s.result.diff:
                    files_touched.update(self._extract_files(s.result.diff))
            if len(files_touched) <= 2 and len(run.subtasks) > 2:
                lessons.append(Lesson(
                    category="planner",
                    pattern="over_decomposed",
                    content=f"Task '{run.task}' split into {len(run.subtasks)} subtasks but only touched {len(files_touched)} files. Should be SOLO.",
                    task_signature=self._normalize(run.task),
                ))

        # Stall detection
        for s in run.subtasks:
            if s.retry_count > 0 and s.result:
                lessons.append(Lesson(
                    category="dispatch",
                    pattern="agent_stalled",
                    content=f"Subtask '{s.description}' stalled {s.retry_count} times with {s.result.agent_runtime}.",
                    task_signature=self._normalize(s.description),
                ))

        # No-diff detection
        for s in run.subtasks:
            if s.result and s.result.success and not s.result.diff:
                lessons.append(Lesson(
                    category="agent",
                    pattern="no_diff",
                    content=f"Agent {s.result.agent_runtime} produced no diff for '{s.description}'.",
                    task_signature=self._normalize(s.description),
                ))

        return lessons

    async def get_lessons_for_task(self, task: str, limit: int = 5) -> list[Lesson]:
        """Retrieve relevant lessons for a new task."""
        ...

    def _normalize(self, text: str) -> str:
        """Normalize task description for fuzzy matching."""
        ...
```

#### Integration with Planner

SmartPlanner queries lessons before classifying:

```python
async def _get_lessons(self, task: str) -> str:
    lessons = await self._memory.get_lessons_for_task(task)
    if not lessons:
        return ""
    return "\n".join(f"- [{l.pattern}] {l.content}" for l in lessons)
```

This injects into both the classification and decomposition prompts as context.

### 3. Run Model Enhancements

Add fields to Run for better lifecycle tracking:

```python
class Run(BaseModel):
    id: str
    task: str
    state: RunState = RunState.planning
    complexity: TaskComplexity | None = None  # NEW: classification result
    subtasks: list[Subtask] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)  # NEW: lesson IDs from this run
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
```

Add `complexity` column to runs table (migration 2).

### 4. Orchestrator Changes

Update `_plan()` to use SmartPlanner:

```python
async def _plan(self, run: Run) -> Run:
    subtasks, complexity = await self._smart_planner.decompose(run.task)
    run.complexity = complexity
    run.subtasks = self._resolve_deps(subtasks)
    run.state = RunState.executing
    return run
```

Update `_learn()` to extract and store lessons:

```python
async def _learn(self, run: Run) -> None:
    # Existing: store in memvid
    if self._memory:
        await self._memory.store_run_result(run, ...)

    # New: extract and store lessons
    if self._lessons:
        extracted = await self._lessons.extract_lessons(run)
        for lesson in extracted:
            await self._lessons.store(lesson)
        run.lessons = [l.id for l in extracted]
```

### 5. File Changes Summary

| File | Change |
|------|--------|
| `src/horse_fish/planner/smart.py` | NEW: SmartPlanner with classify + decompose + strip |
| `src/horse_fish/memory/lessons.py` | NEW: LessonStore with extract + query |
| `src/horse_fish/models.py` | ADD: TaskComplexity enum, Run.complexity, Run.lessons |
| `src/horse_fish/store/db.py` | ADD: migration 2 with lessons table + runs.complexity column |
| `src/horse_fish/orchestrator/engine.py` | UPDATE: use SmartPlanner, wire LessonStore into _learn() |
| `src/horse_fish/planner/decompose.py` | UPDATE: accept max_subtasks param, remove "aim for 3-8" from prompt |
| `tests/test_smart_planner.py` | NEW: tests for classify, strip, fallback |
| `tests/test_lessons.py` | NEW: tests for lesson extraction and querying |

### 6. What This Does NOT Include (Future Work)

- A2A protocol layer (later — orchestrator as A2A server)
- Market bidding (agents self-select) — current scoring-based dispatch stays
- Progressive decomposition (agents propose sub-plans)
- Typed artifact registry
- Multi-run sessions
- Operator console (hf status, hf msg)
