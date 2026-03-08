"""Tests for core domain models."""

from datetime import UTC, datetime

from horse_fish.models import (
    AgentSlot,
    AgentState,
    Run,
    RunState,
    Subtask,
    SubtaskResult,
    SubtaskState,
)


class TestAgentState:
    def test_values(self):
        assert AgentState.idle == "idle"
        assert AgentState.busy == "busy"
        assert AgentState.dead == "dead"

    def test_is_str(self):
        assert isinstance(AgentState.idle, str)


class TestSubtaskState:
    def test_values(self):
        assert SubtaskState.pending == "pending"
        assert SubtaskState.running == "running"
        assert SubtaskState.done == "done"
        assert SubtaskState.failed == "failed"
        assert SubtaskState.escalated == "escalated"


class TestRunState:
    def test_values(self):
        assert RunState.planning == "planning"
        assert RunState.executing == "executing"
        assert RunState.reviewing == "reviewing"
        assert RunState.merging == "merging"
        assert RunState.completed == "completed"
        assert RunState.failed == "failed"


class TestAgentSlot:
    def test_create_minimal(self):
        slot = AgentSlot(id="a1", name="agent-1", runtime="claude", model="claude-sonnet-4-6", capability="builder")
        assert slot.id == "a1"
        assert slot.name == "agent-1"
        assert slot.runtime == "claude"
        assert slot.model == "claude-sonnet-4-6"
        assert slot.capability == "builder"
        assert slot.state == AgentState.idle

    def test_defaults(self):
        slot = AgentSlot(id="a1", name="agent-1", runtime="copilot", model="gpt-5.4", capability="scout")
        assert slot.pid is None
        assert slot.tmux_session is None
        assert slot.worktree_path is None
        assert slot.branch is None
        assert slot.task_id is None
        assert slot.started_at is None
        assert slot.idle_since is None

    def test_full_fields(self):
        now = datetime.now(UTC)
        slot = AgentSlot(
            id="a2",
            name="agent-2",
            runtime="pi",
            model="kimi-for-coding",
            capability="reviewer",
            state=AgentState.busy,
            pid=12345,
            tmux_session="horse-fish:agent-2",
            worktree_path="/tmp/worktree",
            branch="feature/x",
            task_id="task-1",
            started_at=now,
        )
        assert slot.state == AgentState.busy
        assert slot.pid == 12345
        assert slot.task_id == "task-1"

    def test_serialization(self):
        slot = AgentSlot(id="a1", name="agent-1", runtime="opencode", model="qwen3.5-plus", capability="lead")
        data = slot.model_dump()
        assert data["id"] == "a1"
        assert data["state"] == "idle"
        restored = AgentSlot.model_validate(data)
        assert restored.id == slot.id
        assert restored.state == slot.state


class TestSubtaskResult:
    def test_create(self):
        result = SubtaskResult(
            subtask_id="st-1",
            success=True,
            output="done",
            diff="--- a\n+++ b\n",
            duration_seconds=1.5,
        )
        assert result.subtask_id == "st-1"
        assert result.success is True
        assert result.duration_seconds == 1.5


class TestSubtask:
    def test_create_factory(self):
        st = Subtask.create("implement login")
        assert st.description == "implement login"
        assert isinstance(st.id, str)
        assert len(st.id) == 36  # UUID4
        assert st.state == SubtaskState.pending
        assert st.deps == []
        assert st.files_hint == []
        assert st.agent is None
        assert st.result is None

    def test_create_with_deps(self):
        st = Subtask(id="st-2", description="deploy", deps=["st-1"], files_hint=["src/deploy.py"])
        assert st.deps == ["st-1"]
        assert st.files_hint == ["src/deploy.py"]

    def test_state_transition(self):
        st = Subtask.create("test task")
        assert st.state == SubtaskState.pending
        st.state = SubtaskState.running
        assert st.state == SubtaskState.running
        st.state = SubtaskState.done
        assert st.state == SubtaskState.done

    def test_serialization(self):
        st = Subtask.create("serialize me")
        data = st.model_dump()
        assert data["state"] == "pending"
        restored = Subtask.model_validate(data)
        assert restored.description == st.description
        assert restored.id == st.id

    def test_with_result(self):
        st = Subtask.create("task with result")
        st.result = SubtaskResult(subtask_id=st.id, success=True, output="ok", diff="", duration_seconds=0.5)
        st.state = SubtaskState.done
        assert st.result.success is True
        assert st.state == SubtaskState.done


class TestRun:
    def test_create_factory(self):
        run = Run.create("build the feature")
        assert run.task == "build the feature"
        assert isinstance(run.id, str)
        assert len(run.id) == 36
        assert run.state == RunState.planning
        assert run.subtasks == []
        assert isinstance(run.created_at, datetime)
        assert run.completed_at is None

    def test_unique_ids(self):
        r1 = Run.create("task a")
        r2 = Run.create("task b")
        assert r1.id != r2.id

    def test_add_subtasks(self):
        run = Run.create("multi-step task")
        run.subtasks.append(Subtask.create("step 1"))
        run.subtasks.append(Subtask.create("step 2"))
        assert len(run.subtasks) == 2

    def test_state_transitions(self):
        run = Run.create("workflow")
        assert run.state == RunState.planning
        run.state = RunState.executing
        assert run.state == RunState.executing
        run.state = RunState.completed

    def test_serialization_roundtrip(self):
        run = Run.create("serialize test")
        run.subtasks.append(Subtask.create("sub1"))
        data = run.model_dump()
        assert data["state"] == "planning"
        assert len(data["subtasks"]) == 1
        restored = Run.model_validate(data)
        assert restored.id == run.id
        assert restored.task == run.task
        assert len(restored.subtasks) == 1
        assert restored.subtasks[0].description == "sub1"

    def test_completed_at(self):
        run = Run.create("final task")
        now = datetime.now(UTC)
        run.completed_at = now
        run.state = RunState.completed
        assert run.completed_at == now
