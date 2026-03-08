"""Tests for dispatch module - agent selection logic."""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from horse_fish.dispatch.selector import RUNTIME_SPEED, AgentScore, AgentSelector
from horse_fish.models import AgentSlot, AgentState, Subtask
from horse_fish.store.db import Store


class TestAgentScore:
    def test_create(self):
        score = AgentScore(agent_id="a1", runtime="claude", capability="builder", score=0.85)
        assert score.agent_id == "a1"
        assert score.runtime == "claude"
        assert score.capability == "builder"
        assert score.score == 0.85

    def test_dataclass_fields(self):
        score = AgentScore(agent_id="a2", runtime="pi", capability="reviewer", score=0.6)
        assert hasattr(score, "agent_id")
        assert hasattr(score, "runtime")
        assert hasattr(score, "capability")
        assert hasattr(score, "score")


class TestRuntimeSpeedRankings:
    def test_all_runtimes_defined(self):
        assert "claude" in RUNTIME_SPEED
        assert "pi" in RUNTIME_SPEED
        assert "copilot" in RUNTIME_SPEED
        assert "opencode" in RUNTIME_SPEED

    def test_rankings_order(self):
        # claude=1.0, pi=0.7, copilot=0.4, opencode=0.3
        assert RUNTIME_SPEED["claude"] == 1.0
        assert RUNTIME_SPEED["pi"] == 0.7
        assert RUNTIME_SPEED["copilot"] == 0.4
        assert RUNTIME_SPEED["opencode"] == 0.3

    def test_rankings_are_ordered(self):
        assert RUNTIME_SPEED["claude"] > RUNTIME_SPEED["pi"]
        assert RUNTIME_SPEED["pi"] > RUNTIME_SPEED["copilot"]
        assert RUNTIME_SPEED["copilot"] > RUNTIME_SPEED["opencode"]


class TestAgentSelector:
    def setup_method(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.temp_db.close()
        self.store = Store(self.temp_db.name)
        self.store.migrate()
        self.selector = AgentSelector(self.store)

    def teardown_method(self):
        Path(self.temp_db.name).unlink(missing_ok=True)

    def _create_agent(
        self,
        agent_id: str = "a1",
        runtime: str = "claude",
        capability: str = "builder",
        state: AgentState = AgentState.idle,
        idle_since: datetime | None = None,
        worktree_path: str | None = None,
    ) -> AgentSlot:
        return AgentSlot(
            id=agent_id,
            name=f"agent-{agent_id}",
            runtime=runtime,
            model="test-model",
            capability=capability,
            state=state,
            worktree_path=worktree_path,
            idle_since=idle_since,
        )

    def test_select_returns_none_when_empty(self):
        subtask = Subtask.create("do something")
        result = self.selector.select(subtask, [])
        assert result is None

    def test_select_returns_best_agent(self):
        now = datetime.now(UTC)
        agents = [
            self._create_agent("a1", runtime="opencode", capability="builder", idle_since=now),
            self._create_agent("a2", runtime="claude", capability="builder", idle_since=now),
        ]
        subtask = Subtask.create("build feature")

        result = self.selector.select(subtask, agents)

        assert result is not None
        assert result.id == "a2"  # claude is faster

    def test_select_returns_best_by_capability(self):
        now = datetime.now(UTC)
        agents = [
            self._create_agent("a1", runtime="claude", capability="scout", idle_since=now),
            self._create_agent("a2", runtime="claude", capability="builder", idle_since=now),
        ]
        subtask = Subtask.create("build feature")

        result = self.selector.select(subtask, agents)

        # builder capability should win when runtime is equal
        assert result is not None
        assert result.id == "a2"

    def test_score_agent_basic(self):
        agent = self._create_agent("a1", runtime="claude", capability="builder")
        subtask = Subtask.create("build something")

        score = self.selector.score_agent(subtask, agent)

        assert score.agent_id == "a1"
        assert score.runtime == "claude"
        assert score.capability == "builder"
        assert 0.0 <= score.score <= 1.0

    def test_score_agent_capability_match(self):
        agent = self._create_agent("a1", runtime="claude", capability="builder")
        subtask = Subtask.create("build feature")

        score = self.selector.score_agent(subtask, agent)

        # Capability match should contribute 0.4 * 1.0 = 0.4 to score
        assert score.score >= 0.4

    def test_score_agent_capability_mismatch(self):
        agent = self._create_agent("a1", runtime="claude", capability="reviewer")
        subtask = Subtask.create("build feature")

        score = self.selector.score_agent(subtask, agent)

        # Capability mismatch should contribute 0.0
        # But runtime still contributes
        assert score.score < 0.5  # Less than if capability matched

    def test_score_agent_runtime_preference(self):
        subtask = Subtask.create("build feature")

        claude_agent = self._create_agent("a1", runtime="claude", capability="builder")
        opencode_agent = self._create_agent("a2", runtime="opencode", capability="builder")

        claude_score = self.selector.score_agent(subtask, claude_agent)
        opencode_score = self.selector.score_agent(subtask, opencode_agent)

        # Claude should score higher due to runtime preference
        assert claude_score.score > opencode_score.score

    def test_rank_returns_sorted_list(self):
        now = datetime.now(UTC)
        agents = [
            self._create_agent("a1", runtime="opencode", capability="builder", idle_since=now),
            self._create_agent("a2", runtime="claude", capability="builder", idle_since=now),
            self._create_agent("a3", runtime="pi", capability="builder", idle_since=now),
        ]
        subtask = Subtask.create("build feature")

        ranked = self.selector.rank(subtask, agents)

        assert len(ranked) == 3
        assert ranked[0].agent_id == "a2"  # claude first
        assert ranked[1].agent_id == "a3"  # pi second
        assert ranked[2].agent_id == "a1"  # opencode last

    def test_rank_empty_list(self):
        subtask = Subtask.create("build feature")
        ranked = self.selector.rank(subtask, [])
        assert ranked == []

    def test_capability_match_for_review_task(self):
        reviewer = self._create_agent("a1", runtime="claude", capability="reviewer")
        builder = self._create_agent("a2", runtime="claude", capability="builder")
        subtask = Subtask.create("review the code changes")

        reviewer_score = self.selector.score_agent(subtask, reviewer)
        builder_score = self.selector.score_agent(subtask, builder)

        assert reviewer_score.score > builder_score.score

    def test_lead_can_do_anything(self):
        lead = self._create_agent("a1", runtime="claude", capability="lead")
        builder = self._create_agent("a2", runtime="claude", capability="builder")
        subtask = Subtask.create("build feature")

        lead_score = self.selector.score_agent(subtask, lead)
        builder_score = self.selector.score_agent(subtask, builder)

        # Lead gets partial score (0.8 * 0.4 = 0.32) vs builder (1.0 * 0.4 = 0.4)
        # So builder should still win for build tasks
        assert builder_score.score > lead_score.score

    def test_scout_can_do_builder_tasks(self):
        scout = self._create_agent("a1", runtime="claude", capability="scout")
        reviewer = self._create_agent("a2", runtime="claude", capability="reviewer")
        subtask = Subtask.create("build feature")

        scout_score = self.selector.score_agent(subtask, scout)
        reviewer_score = self.selector.score_agent(subtask, reviewer)

        # Scout gets partial score (0.6 * 0.4 = 0.24) vs reviewer (0.0)
        assert scout_score.score > reviewer_score.score

    def test_files_hint_overlap(self):
        agent = self._create_agent(
            "a1",
            runtime="claude",
            capability="builder",
            worktree_path="/tmp/worktree/src/horse_fish/dispatch",
        )
        subtask = Subtask(
            id="st-1",
            description="build feature",
            files_hint=["src/horse_fish/dispatch/selector.py"],
        )

        score = self.selector.score_agent(subtask, agent)

        # Should get high files hint score
        assert score.score > 0.5

    def test_idle_time_scoring(self):
        now = datetime.now(UTC)
        long_idle = now - timedelta(hours=2)
        short_idle = now - timedelta(minutes=1)

        long_idle_agent = self._create_agent("a1", runtime="claude", capability="builder", idle_since=long_idle)
        short_idle_agent = self._create_agent("a2", runtime="claude", capability="builder", idle_since=short_idle)
        busy_agent = self._create_agent("a3", runtime="claude", capability="builder", state=AgentState.busy)

        subtask = Subtask.create("build feature")

        long_score = self.selector.score_agent(subtask, long_idle_agent)
        short_score = self.selector.score_agent(subtask, short_idle_agent)
        busy_score = self.selector.score_agent(subtask, busy_agent)

        # Long idle should score highest for idle time factor
        assert long_score.score > short_score.score
        # Busy agent should score lowest (idle_score = 0.0)
        assert busy_score.score < short_score.score

    def test_idle_time_unknown(self):
        agent = self._create_agent("a1", runtime="claude", capability="builder", idle_since=None)
        subtask = Subtask.create("build feature")

        score = self.selector.score_agent(subtask, agent)

        # Should get neutral score for unknown idle time (0.5)
        # Total: 0.4 (capability) + 0.3 (runtime) + 0.1 (files neutral) + 0.05 (idle neutral) = 0.85
        assert score.score >= 0.7


class TestScoringWeights:
    """Test that scoring weights are applied correctly."""

    def setup_method(self):
        self.temp_db = tempfile.NamedTemporaryFile(delete=False, suffix=".db")
        self.temp_db.close()
        self.store = Store(self.temp_db.name)
        self.store.migrate()
        self.selector = AgentSelector(self.store)

    def teardown_method(self):
        Path(self.temp_db.name).unlink(missing_ok=True)

    def test_capability_weight_dominates(self):
        """Capability match (0.4 weight) should have significant impact."""
        from horse_fish.dispatch.selector import CAPABILITY_WEIGHT

        assert CAPABILITY_WEIGHT == 0.4

    def test_runtime_weight(self):
        """Runtime preference (0.3 weight) should matter but less than capability."""
        from horse_fish.dispatch.selector import RUNTIME_WEIGHT

        assert RUNTIME_WEIGHT == 0.3

    def test_files_hint_weight(self):
        """Files hint overlap (0.2 weight) is a secondary factor."""
        from horse_fish.dispatch.selector import FILES_HINT_WEIGHT

        assert FILES_HINT_WEIGHT == 0.2

    def test_idle_time_weight(self):
        """Idle time (0.1 weight) is the smallest factor."""
        from horse_fish.dispatch.selector import IDLE_TIME_WEIGHT

        assert IDLE_TIME_WEIGHT == 0.1

    def test_weights_sum_to_one(self):
        """All weights should sum to 1.0."""
        from horse_fish.dispatch.selector import (
            CAPABILITY_WEIGHT,
            FILES_HINT_WEIGHT,
            IDLE_TIME_WEIGHT,
            RUNTIME_WEIGHT,
        )

        total = CAPABILITY_WEIGHT + RUNTIME_WEIGHT + FILES_HINT_WEIGHT + IDLE_TIME_WEIGHT
        assert abs(total - 1.0) < 0.0001  # Account for floating point precision
