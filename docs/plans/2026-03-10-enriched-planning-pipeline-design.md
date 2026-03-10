# Enriched Planning Pipeline Design

Date: 2026-03-10
Status: Implemented (session 22)
References: spec-kit (specification-driven development), BMAD-METHOD (scale-adaptive agile), BridgeMind (swarm taxonomy origin for SOLO/TRIO/SQUAD)

## Problem

1. **Planner has no codebase context** — SmartPlanner receives only the raw task description + lessons from memory. It never sees the file tree, source code, conventions, or architecture. Result: subtasks are thin, agents get dispatched with insufficient context, and work is often misaligned.

2. **No pre-planning research phase** — Both spec-kit and BMAD dedicate entire phases to research, clarification, and specification before any implementation planning. Horse-fish jumps straight from task description to DAG decomposition.

3. **Subtasks lack acceptance criteria** — Subtasks have a `description` and `files_hint` but no testable success conditions. The review phase can only run generic gates (compile, test, lint) — it can't validate whether the subtask actually achieved its goal.

4. **Complexity classification is blind** — SmartPlanner classifies SOLO/TRIO/SQUAD based on task description alone. Without knowing the codebase structure, it can't judge how many files/components are actually involved.

## Design

### Overview

Insert a **scout phase** between task submission and planning. A scout agent explores the codebase and produces a structured **context brief** that feeds into the SmartPlanner. This enriches both complexity classification and subtask decomposition.

```
CURRENT:
  task description ──→ SmartPlanner (classify + decompose) ──→ subtasks
                            │
                     (lessons + cognee)

PROPOSED:
  task description ──→ Scout Phase ──→ Context Brief ──→ SmartPlanner ──→ subtasks
                           │                                  │
                    (agent explores codebase)      (classify + decompose with
                                                    full project understanding)
```

### 1. Context Brief Model

New Pydantic model representing the scout's output:

```python
class FileContext(BaseModel):
    path: str                    # relative to repo root
    purpose: str                 # one-line description
    line_count: int | None = None

class ContextBrief(BaseModel):
    relevant_files: list[FileContext]     # files the task will likely touch or depend on
    patterns: list[str]                   # conventions found: "async by default", "Pydantic models for data"
    dependencies: list[str]              # "changes to models.py require updating engine.py"
    acceptance_criteria: list[str]       # "pytest passes", "new CLI command returns expected output"
    risks: list[str]                     # "no existing tests for this module", "hot path"
    suggested_approach: str              # brief implementation strategy
```

This is an in-memory data structure passed to the planner. Not persisted as a file artifact.

### 2. Scout Phase

The scout is a **real agent** spawned in tmux (same infrastructure as builders), but with a different prompt and expected output format.

#### Scout Prompt Template

```
You are a codebase scout. Your job is to explore this project and produce a structured context brief for a planning agent.

TASK: {task_description}

PROJECT CONTEXT:
{claude_md_content}

INSTRUCTIONS:
1. Read the project structure (file tree, key modules)
2. Find files relevant to this task using grep/glob
3. Read those files to understand patterns and conventions
4. Identify what the task will need to change and what depends on those changes
5. Propose acceptance criteria — how will we know the task is done?
6. Note any risks (missing tests, complex dependencies, hot paths)

OUTPUT: Reply with a JSON object matching this schema:
{context_brief_json_schema}

Be thorough but concise. Focus on what the planner needs to make good decomposition decisions.
```

#### Scout Runtime Selection

- Default to the cheapest available runtime (pi/opencode) since scouting is read-only exploration
- Falls back through: pi → opencode → kimi → claude (cost-ascending order)
- Scout timeout: 120s (same as planner)

#### Scout Output Parsing

- Extract JSON from agent output (same markdown-fence-stripping as planner)
- Validate against ContextBrief schema
- On parse failure: log warning, proceed with empty brief (graceful degradation)

### 3. SmartPlanner Changes

#### Classification Enhancement

Current classification prompt gets only the task description. New prompt includes the context brief:

```
Given this task and codebase context, estimate complexity:
- SOLO: Single file or tightly coupled change. One agent handles everything.
- TRIO: 2-4 files across 1-2 components. Needs decomposition but minimal.
- SQUAD: 5+ files, multiple components, needs parallel work.

Task: {task}

Codebase Context:
- Relevant files: {brief.relevant_files}
- Dependencies: {brief.dependencies}
- Risks: {brief.risks}
- Suggested approach: {brief.suggested_approach}

Past lessons: {lessons}

Reply with ONLY one word: SOLO, TRIO, or SQUAD.
```

#### Decomposition Enhancement

The decomposition prompt includes the full context brief, and instructs the planner to:
- Use `relevant_files` to assign accurate `files_hint` per subtask
- Derive `acceptance_criteria` per subtask from the brief's criteria + file-specific checks
- Respect `dependencies` when defining subtask DAG edges
- Note `risks` in subtask descriptions so builders are aware

#### SOLO Fast Path

For SOLO mode, skip spawning a scout agent entirely. Instead, do a **programmatic scout** — the orchestrator reads CLAUDE.md and runs a quick file glob/grep in-process. This keeps SOLO fast (no extra agent spawn overhead).

The programmatic scout produces a lighter ContextBrief:
- `relevant_files`: from basic glob matching on task keywords
- `patterns`: from CLAUDE.md content
- `acceptance_criteria`: generic (tests pass, lint clean)
- Other fields: empty

### 4. Subtask Model Changes

Add acceptance criteria to the Subtask model:

```python
class Subtask(BaseModel):
    # ... existing fields ...
    acceptance_criteria: list[str] = []   # NEW: testable success conditions
```

The planner populates this from the context brief. The review phase can use it for richer validation (future enhancement — not in this iteration).

### 5. Orchestrator Integration

New state in the pipeline: `planning` splits into `scouting → planning`.

```python
class RunState(StrEnum):
    pending = "pending"
    scouting = "scouting"       # NEW
    planning = "planning"
    executing = "executing"
    reviewing = "reviewing"
    merging = "merging"
    completed = "completed"
    failed = "failed"
```

#### Engine Changes

```python
async def _scout(self, run: Run) -> ContextBrief:
    """Scout phase: gather codebase context for the planner."""
    if run.complexity == TaskComplexity.solo:
        return self._programmatic_scout(run.task)

    # Spawn scout agent
    scout_agent = await self._pool.spawn_scout(run.task)
    brief_json = await self._pool.wait_for_output(scout_agent, timeout=120)
    return ContextBrief.model_validate_json(brief_json)

async def _plan(self, run: Run, brief: ContextBrief) -> list[Subtask]:
    """Plan phase: decompose with context."""
    return await self._planner.decompose(run.task, context_brief=brief)
```

The orchestrator calls `_scout()` first, then passes the brief to `_plan()`.

#### Chicken-and-Egg: Classification Before Scouting

Problem: We need to know SOLO/TRIO/SQUAD to decide whether to spawn a scout, but the scout's output helps classify complexity.

Solution: **Two-pass classification.**
1. **Pre-scout classification** (cheap, fast): Classify based on task description alone (current behavior). This is just to decide "do we need a scout?"
   - If SOLO → programmatic scout → plan with brief
   - If TRIO/SQUAD → spawn scout agent → re-classify with brief → plan with brief
2. **Post-scout re-classification**: After the scout returns, re-classify with the full context. This may downgrade SQUAD→TRIO or upgrade TRIO→SQUAD.

### 6. Agent Pool Changes

Add scout capability to the pool:

```python
async def spawn_scout(self, task: str) -> Agent:
    """Spawn a scout agent for codebase exploration."""
    # Uses cheapest available runtime
    # Scout agents get read-only prompt (no write instructions)
    # Worktree not needed (read-only)
    ...
```

Scout agents:
- Don't need a git worktree (they only read)
- Use the canonical repo directory as cwd
- Get a specialized prompt (explore, don't implement)
- Are cleaned up after the brief is extracted

### 7. Graceful Degradation

Every new component has a fallback:

| Component | Failure Mode | Fallback |
|-----------|-------------|----------|
| Scout agent spawn | Runtime unavailable | Programmatic scout (file reads) |
| Scout output parse | Invalid JSON | Empty ContextBrief, proceed with current behavior |
| Scout timeout | Agent stalls | Kill agent, use programmatic scout |
| Re-classification | LLM error | Keep pre-scout classification |
| Context brief in planner | Brief is empty | Decompose with task description only (current behavior) |

### 8. Observability

- Langfuse span for scout phase (duration, runtime used, brief size)
- Log context brief summary at INFO level
- Track scout success/failure in lessons for future optimization

## Implementation Plan

### Step 1: Models
- Add `ContextBrief`, `FileContext` to `models.py`
- Add `acceptance_criteria` field to `Subtask`
- Add `scouting` to `RunState`

### Step 2: Programmatic Scout
- New function in `planner/` that reads CLAUDE.md + globs relevant files
- Returns a `ContextBrief` without spawning an agent
- Used for SOLO mode and as fallback

### Step 3: Scout Agent
- Scout prompt template in `planner/prompts.py` (or similar)
- `AgentPool.spawn_scout()` method
- Output parsing + validation

### Step 4: SmartPlanner Integration
- `classify()` accepts optional `ContextBrief`
- `decompose()` accepts optional `ContextBrief`
- Updated prompts with brief injection
- Two-pass classification logic

### Step 5: Orchestrator Integration
- `_scout()` method in engine
- `_plan()` receives brief
- State machine: scouting → planning transition
- Langfuse spans

### Step 6: Tests
- Unit tests for ContextBrief parsing
- Unit tests for programmatic scout
- Unit tests for enriched classification/decomposition prompts
- Integration test: scout → plan pipeline
- Fallback/degradation tests

## Scope

**In scope:**
- Scout phase (agent + programmatic)
- Context brief model
- SmartPlanner enrichment
- Subtask acceptance_criteria field
- Two-pass classification
- Graceful degradation
- Tests

**Out of scope (future):**
- Review phase using acceptance_criteria for validation
- Scout caching (reuse brief across retries)
- Persisted spec artifacts (spec-kit style)
- LEGION mode
- Human-in-the-loop gates
