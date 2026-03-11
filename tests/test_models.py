"""Tests for core domain models."""

from datetime import UTC, datetime

from horse_fish.models import (
    AgentSlot,
    AgentState,
    Plan,
    PlanState,
    Subtask,
    SubtaskResult,
    SubtaskState,
    Task,
    TaskComplexity,
    TaskState,
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


class TestTaskState:
    def test_values(self):
        assert TaskState.planning == "planning"
        assert TaskState.executing == "executing"
        assert TaskState.reviewing == "reviewing"
        assert TaskState.merging == "merging"
        assert TaskState.completed == "completed"
        assert TaskState.failed == "failed"


class TestTaskComplexity:
    def test_values(self):
        assert TaskComplexity.solo == "SOLO"
        assert TaskComplexity.trio == "TRIO"
        assert TaskComplexity.squad == "SQUAD"

    def test_is_strenum(self):
        assert isinstance(TaskComplexity.solo, str)

    def test_serialization(self):
        assert TaskComplexity.solo.value == "SOLO"
        assert TaskComplexity.trio.value == "TRIO"
        assert TaskComplexity.squad.value == "SQUAD"


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

    def test_provenance_fields(self):
        """Test SubtaskResult has provenance metadata."""
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        result = SubtaskResult(
            subtask_id="s1",
            success=True,
            output="Done",
            diff="commit",
            duration_seconds=10.0,
            agent_id="agent-1",
            agent_runtime="claude",
            agent_model="claude-sonnet-4.6",
            run_id="run-1",
            completed_at=now,
        )
        assert result.agent_id == "agent-1"
        assert result.agent_runtime == "claude"
        assert result.agent_model == "claude-sonnet-4.6"
        assert result.run_id == "run-1"
        assert result.completed_at == now

    def test_provenance_defaults(self):
        """Test SubtaskResult provenance fields default to None."""
        result = SubtaskResult(
            subtask_id="s1",
            success=True,
            output="Done",
            diff="",
            duration_seconds=5.0,
        )
        assert result.agent_id is None
        assert result.agent_runtime is None
        assert result.agent_model is None
        assert result.run_id is None
        assert result.completed_at is None


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

    def test_retry_fields(self):
        """Test Subtask has retry_count and last_activity_at fields."""
        st = Subtask.create("Test task")
        assert st.retry_count == 0
        assert st.max_retries == 2
        assert st.last_activity_at is None

    def test_retry_fields_explicit(self):
        """Test Subtask accepts explicit retry field values."""
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        st = Subtask(
            id="st-1",
            description="Test task",
            retry_count=1,
            max_retries=3,
            last_activity_at=now,
        )
        assert st.retry_count == 1
        assert st.max_retries == 3
        assert st.last_activity_at == now


class TestTask:
    def test_create_factory(self):
        run = Task.create("build the feature")
        assert run.task == "build the feature"
        assert isinstance(run.id, str)
        assert len(run.id) == 36
        assert run.state == TaskState.scouting
        assert run.subtasks == []
        assert isinstance(run.created_at, datetime)
        assert run.completed_at is None

    def test_unique_ids(self):
        r1 = Task.create("task a")
        r2 = Task.create("task b")
        assert r1.id != r2.id

    def test_add_subtasks(self):
        run = Task.create("multi-step task")
        run.subtasks.append(Subtask.create("step 1"))
        run.subtasks.append(Subtask.create("step 2"))
        assert len(run.subtasks) == 2

    def test_state_transitions(self):
        run = Task.create("workflow")
        assert run.state == TaskState.scouting
        run.state = TaskState.executing
        assert run.state == TaskState.executing
        run.state = TaskState.completed

    def test_serialization_roundtrip(self):
        run = Task.create("serialize test")
        run.subtasks.append(Subtask.create("sub1"))
        data = run.model_dump()
        assert data["state"] == "scouting"
        assert len(data["subtasks"]) == 1
        restored = Task.model_validate(data)
        assert restored.id == run.id
        assert restored.task == run.task
        assert len(restored.subtasks) == 1
        assert restored.subtasks[0].description == "sub1"

    def test_completed_at(self):
        run = Task.create("final task")
        now = datetime.now(UTC)
        run.completed_at = now
        run.state = TaskState.completed
        assert run.completed_at == now

    def test_complexity_field_default(self):
        run = Task.create("test task")
        assert run.complexity is None

    def test_complexity_field_explicit(self):
        run = Task.create("complex task")
        run.complexity = TaskComplexity.squad
        assert run.complexity == TaskComplexity.squad

    def test_lessons_field_default(self):
        run = Task.create("learning task")
        assert run.lessons == []

    def test_lessons_field_append(self):
        run = Task.create("learning task")
        run.lessons.append("Lesson 1")
        run.lessons.append("Lesson 2")
        assert len(run.lessons) == 2
        assert run.lessons == ["Lesson 1", "Lesson 2"]

    def test_serialization_with_new_fields(self):
        run = Task.create("serialize test")
        run.complexity = TaskComplexity.trio
        run.lessons.append("Learned something")
        data = run.model_dump()
        assert data["complexity"] == "TRIO"
        assert data["lessons"] == ["Learned something"]
        restored = Task.model_validate(data)
        assert restored.complexity == TaskComplexity.trio
        assert restored.lessons == ["Learned something"]


class TestPlanState:
    def test_values(self):
        assert PlanState.planning == "planning"
        assert PlanState.executing == "executing"
        assert PlanState.replanning == "replanning"
        assert PlanState.completed == "completed"
        assert PlanState.partial_success == "partial_success"
        assert PlanState.failed == "failed"
        assert PlanState.cancelled == "cancelled"

    def test_is_str(self):
        assert isinstance(PlanState.planning, str)


class TestPlan:
    def test_create_factory(self):
        plan = Plan.create("deploy to production")
        assert plan.goal == "deploy to production"
        assert isinstance(plan.id, str)
        assert len(plan.id) == 36  # UUID4
        assert plan.state == PlanState.planning
        assert plan.tasks == []
        assert plan.goal_conditions == []
        assert plan.round == 0
        assert plan.max_rounds == 10
        assert isinstance(plan.created_at, datetime)
        assert plan.completed_at is None

    def test_unique_ids(self):
        p1 = Plan.create("goal a")
        p2 = Plan.create("goal b")
        assert p1.id != p2.id

    def test_add_tasks(self):
        plan = Plan.create("multi-task goal")
        plan.tasks.append(Task.create("step 1"))
        plan.tasks.append(Task.create("step 2"))
        assert len(plan.tasks) == 2

    def test_goal_conditions(self):
        plan = Plan.create("goal")
        plan.goal_conditions = ["all tests pass", "no lint errors"]
        assert len(plan.goal_conditions) == 2

    def test_serialization_roundtrip(self):
        plan = Plan.create("serialize test")
        plan.tasks.append(Task.create("sub1"))
        plan.goal_conditions = ["condition1"]
        data = plan.model_dump()
        assert data["state"] == "planning"
        assert len(data["tasks"]) == 1
        assert data["goal_conditions"] == ["condition1"]
        restored = Plan.model_validate(data)
        assert restored.id == plan.id
        assert restored.goal == plan.goal
        assert len(restored.tasks) == 1
