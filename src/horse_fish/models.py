"""Core domain models for horse-fish agent swarm orchestration."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class AgentState(StrEnum):
    idle = "idle"
    busy = "busy"
    dead = "dead"


class SubtaskState(StrEnum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"
    escalated = "escalated"


class RunState(StrEnum):
    queued = "queued"
    scouting = "scouting"
    planning = "planning"
    executing = "executing"
    reviewing = "reviewing"
    merging = "merging"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class TaskComplexity(StrEnum):
    solo = "SOLO"
    trio = "TRIO"
    squad = "SQUAD"


class AgentSlot(BaseModel):
    id: str
    name: str
    runtime: str  # claude | copilot | pi | opencode
    model: str
    capability: str  # builder | scout | reviewer | lead
    state: AgentState = AgentState.idle
    pid: int | None = None
    tmux_session: str | None = None
    worktree_path: str | None = None
    branch: str | None = None
    task_id: str | None = None
    started_at: datetime | None = None
    idle_since: datetime | None = None


class SubtaskResult(BaseModel):
    subtask_id: str
    success: bool
    output: str
    diff: str
    duration_seconds: float
    # Provenance fields (Cognee pattern)
    agent_id: str | None = None
    agent_runtime: str | None = None
    agent_model: str | None = None
    run_id: str | None = None
    completed_at: datetime | None = None


class FileContext(BaseModel):
    """Context information about a single file relevant to a task."""

    path: str  # relative to repo root
    purpose: str  # one-line description of what this file does
    line_count: int | None = None


class ContextBrief(BaseModel):
    """Context brief produced by scouting phase for enriched planning."""

    relevant_files: list[FileContext] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)  # conventions found in codebase
    dependencies: list[str] = Field(default_factory=list)  # what the change touches
    acceptance_criteria: list[str] = Field(default_factory=list)  # how to verify success
    risks: list[str] = Field(default_factory=list)  # things planner should know
    suggested_approach: str = ""  # brief implementation strategy


class Subtask(BaseModel):
    id: str
    description: str
    agent: str | None = None
    deps: list[str] = Field(default_factory=list)
    files_hint: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    state: SubtaskState = SubtaskState.pending
    result: SubtaskResult | None = None
    retry_count: int = 0
    max_retries: int = 2
    last_activity_at: datetime | None = None
    gate_retry_count: int = 0
    max_gate_retries: int = 1

    @classmethod
    def create(cls, description: str) -> Subtask:
        return cls(id=str(uuid.uuid4()), description=description)


class Run(BaseModel):
    id: str
    task: str
    state: RunState = RunState.scouting
    complexity: TaskComplexity | None = None
    subtasks: list[Subtask] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None

    @classmethod
    def create(cls, task: str) -> Run:
        return cls(id=str(uuid.uuid4()), task=task)
