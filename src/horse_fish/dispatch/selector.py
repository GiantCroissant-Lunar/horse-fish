"""Agent selection logic for assigning subtasks to the best available agent."""

from __future__ import annotations

from dataclasses import dataclass

from horse_fish.models import AgentSlot, Subtask
from horse_fish.store.db import Store


@dataclass
class AgentScore:
    """Score for an agent's suitability for a subtask."""

    agent_id: str
    runtime: str
    capability: str
    score: float  # 0.0-1.0, higher is better


# Runtime speed rankings for scoring
RUNTIME_SPEED: dict[str, float] = {
    "claude": 1.0,
    "pi": 0.7,
    "copilot": 0.4,
    "opencode": 0.3,
}

# Scoring weights
CAPABILITY_WEIGHT = 0.4
RUNTIME_WEIGHT = 0.3
FILES_HINT_WEIGHT = 0.2
IDLE_TIME_WEIGHT = 0.1


class AgentSelector:
    """Selects the best available agent for a subtask based on multiple scoring factors."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def select(self, subtask: Subtask, available_agents: list[AgentSlot]) -> AgentSlot | None:
        """
        Score each available agent and return the best match, or None if none suitable.

        Scoring factors:
        1. Capability match (builder for code tasks, reviewer for review tasks) — weight 0.4
        2. Runtime preference (prefer faster runtimes) — weight 0.3
        3. Files hint overlap with agent's previous work — weight 0.2
        4. Idle time (prefer agents idle longest) — weight 0.1
        """
        if not available_agents:
            return None

        scored = [self.score_agent(subtask, agent) for agent in available_agents]
        scored.sort(key=lambda x: x.score, reverse=True)

        # Find the agent with the highest score
        best_score = scored[0]
        for agent in available_agents:
            if agent.id == best_score.agent_id:
                return agent

        return None

    def score_agent(self, subtask: Subtask, agent: AgentSlot) -> AgentScore:
        """Calculate individual agent score based on multiple factors."""
        # 1. Capability match (0.4 weight)
        capability_score = self._score_capability(subtask, agent)

        # 2. Runtime preference (0.3 weight)
        runtime_score = self._score_runtime(agent)

        # 3. Files hint overlap (0.2 weight)
        files_score = self._score_files_hint(subtask, agent)

        # 4. Idle time (0.1 weight)
        idle_score = self._score_idle_time(agent)

        total_score = (
            capability_score * CAPABILITY_WEIGHT
            + runtime_score * RUNTIME_WEIGHT
            + files_score * FILES_HINT_WEIGHT
            + idle_score * IDLE_TIME_WEIGHT
        )

        return AgentScore(
            agent_id=agent.id,
            runtime=agent.runtime,
            capability=agent.capability,
            score=total_score,
        )

    def rank(self, subtask: Subtask, available_agents: list[AgentSlot]) -> list[AgentScore]:
        """Return all agents ranked by score descending."""
        scored = [self.score_agent(subtask, agent) for agent in available_agents]
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored

    def _score_capability(self, subtask: Subtask, agent: AgentSlot) -> float:
        """
        Score capability match.

        Returns 1.0 if agent capability matches the subtask needs, 0.0 otherwise.
        For now, we use a simple heuristic:
        - "builder" capability for general coding tasks
        - "reviewer" capability for review tasks
        """
        description_lower = subtask.description.lower()

        # Determine expected capability from subtask description
        if any(word in description_lower for word in ["review", "check", "verify", "audit"]):
            expected_capability = "reviewer"
        else:
            expected_capability = "builder"

        # Exact match gets full score
        if agent.capability == expected_capability:
            return 1.0

        # Partial match: lead can do anything
        if agent.capability == "lead":
            return 0.8

        # scout can do builder tasks
        if agent.capability == "scout" and expected_capability == "builder":
            return 0.6

        return 0.0

    def _score_runtime(self, agent: AgentSlot) -> float:
        """Score based on runtime speed ranking."""
        return RUNTIME_SPEED.get(agent.runtime, 0.5)

    def _score_files_hint(self, subtask: Subtask, agent: AgentSlot) -> float:
        """
        Score based on files hint overlap with agent's previous work.

        For now, we check if the agent has worked on similar files before.
        This would ideally query the store for agent history.
        """
        if not subtask.files_hint:
            # No file hints, return neutral score
            return 0.5

        # Query store for agent's previous work on similar files
        # This is a simplified implementation - in production, this would be more sophisticated
        try:
            # Check if agent has a worktree_path that overlaps with files_hint
            if agent.worktree_path:
                for hint in subtask.files_hint:
                    if hint in agent.worktree_path or agent.worktree_path in hint:
                        return 1.0

            # Query the store for previous tasks on similar files
            # This is a placeholder for more sophisticated file affinity tracking
            result = self.store.fetchone(
                "SELECT COUNT(*) as count FROM subtasks WHERE agent = ? AND files_hint LIKE ?",
                (agent.id, f"%{subtask.files_hint[0]}%") if subtask.files_hint else (agent.id, ""),
            )
            if result and result.get("count", 0) > 0:
                return 0.8

        except Exception:
            # If store query fails, return neutral score
            pass

        return 0.3

    def _score_idle_time(self, agent: AgentSlot) -> float:
        """
        Score based on idle time.

        Prefer agents that have been idle longest.
        Returns 1.0 for agents idle > 1 hour, scaling down to 0.0 for just-idled agents.
        """
        from datetime import UTC, datetime

        if agent.state != "idle":
            return 0.0

        if agent.idle_since is None:
            return 0.5  # Unknown idle time, neutral score

        now = datetime.now(UTC)
        idle_duration = (now - agent.idle_since).total_seconds()

        # Scale: 0 at 0 seconds, 1.0 at 3600 seconds (1 hour)
        return min(1.0, idle_duration / 3600)
