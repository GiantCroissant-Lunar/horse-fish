"""Horse-fish CLI — agent swarm coordinator."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.memory.store import MemoryStore
from horse_fish.merge.queue import MergeQueue
from horse_fish.orchestrator.engine import Orchestrator
from horse_fish.planner.decompose import Planner
from horse_fish.store.db import Store
from horse_fish.validation.gates import ValidationGates

DB_PATH = ".horse-fish/state.db"


def _init_components(runtime: str, model: str | None, max_agents: int):
    """Initialize all components needed for orchestration."""
    repo_root = str(Path.cwd())
    store = Store(DB_PATH)
    store.migrate()
    tmux = TmuxManager()
    worktrees = WorktreeManager(repo_root)
    pool = AgentPool(store, tmux, worktrees)
    planner = Planner(runtime=runtime, model=model)
    gates = ValidationGates()
    memory = MemoryStore()
    orchestrator = Orchestrator(
        pool=pool,
        planner=planner,
        gates=gates,
        runtime=runtime,
        model=model or "",
        max_agents=max_agents,
        memory=memory,
    )
    return orchestrator, store, pool


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Horse-fish: agent swarm coordinator."""


@main.command()
@click.argument("task", type=str)
@click.option("--runtime", default="claude", help="Default runtime for agents")
@click.option("--model", default=None, help="Model override")
@click.option("--max-agents", default=3, type=int, help="Max concurrent agents")
def run(task: str, runtime: str, model: str | None, max_agents: int):
    """Submit a task to the swarm."""
    orchestrator, store, _pool = _init_components(runtime, model, max_agents)
    try:
        result = asyncio.run(orchestrator.run(task))
        click.echo(f"Run {result.id}: {result.state}")
        for subtask in result.subtasks:
            click.echo(f"  [{subtask.state}] {subtask.description}")
    finally:
        store.close()


@main.command()
def status():
    """Show active runs, agents, subtask progress."""
    store = Store(DB_PATH)
    store.migrate()
    try:
        agents = store.fetchall("SELECT id, name, runtime, state, task_id FROM agents")
        if not agents:
            click.echo("No active agents.")
            return
        click.echo(f"{'Name':<20} {'Runtime':<10} {'State':<8} {'Task'}")
        click.echo("-" * 60)
        for row in agents:
            click.echo(f"{row['name']:<20} {row['runtime']:<10} {row['state']:<8} {row['task_id'] or '-'}")
    finally:
        store.close()


@main.command()
def clean():
    """Kill all agents, remove worktrees, reset state."""
    repo_root = str(Path.cwd())
    store = Store(DB_PATH)
    store.migrate()
    tmux = TmuxManager()
    worktrees = WorktreeManager(repo_root)
    pool = AgentPool(store, tmux, worktrees)
    try:
        released = asyncio.run(pool.cleanup())
        click.echo(f"Released {released} agents.")
    finally:
        store.close()


@main.command()
@click.argument("run_id", required=False)
@click.option("--dry-run", is_flag=True, help="Show pending merges without merging")
@click.option("--force", is_flag=True, help="Merge even if validation gates fail")
def merge(run_id: str | None, dry_run: bool, force: bool):
    """Process merge queue for a run."""
    repo_root = str(Path.cwd())
    store = Store(DB_PATH)
    store.migrate()
    worktrees = WorktreeManager(repo_root)
    merge_queue = MergeQueue(worktrees, store)

    try:
        if dry_run:
            pending = asyncio.run(merge_queue.pending())
            if not pending:
                click.echo("No pending merges in queue.")
                return
            click.echo(f"{'Subtask':<20} {'Agent':<20} {'Branch':<30} {'Priority':<10} {'Created'}")
            click.echo("-" * 100)
            for entry in pending:
                sid = entry["subtask_id"]
                aname = entry["agent_name"]
                branch = entry["branch"]
                prio = entry["priority"]
                created = entry["created_at"]
                click.echo(f"{sid:<20} {aname:<20} {branch:<30} {prio:<10} {created}")
        else:
            results = asyncio.run(merge_queue.process())
            if not results:
                click.echo("No pending merges to process.")
                return
            click.echo("Merge results:")
            for result in results:
                status = "✓ merged" if result.success else "✗ conflict"
                click.echo(f"  [{status}] {result.subtask_id} ({result.branch})")
                if result.conflict_files:
                    click.echo(f"    Conflicts: {', '.join(result.conflict_files)}")
    finally:
        store.close()


@main.command()
@click.option("--agent", default=None, help="Show logs for a specific agent")
@click.option("--lines", default=20, type=int, help="Number of lines to show per agent")
def logs(agent, lines):
    """View agent tmux output."""
    tmux = TmuxManager()
    if agent:
        output = asyncio.run(tmux.capture_pane(agent))
        if output is None:
            click.echo(f"Agent '{agent}' not found or no output available.")
            return
        tail = "\n".join(output.splitlines()[-lines:]) if output.strip() else "(empty)"
        click.echo(f"--- {agent} ---")
        click.echo(tail)
    else:
        sessions = asyncio.run(tmux.list_sessions())
        hf_sessions = [s for s in sessions if s.startswith("hf-")]
        if not hf_sessions:
            click.echo("No active horse-fish agents.")
            return
        for session in hf_sessions:
            output = asyncio.run(tmux.capture_pane(session))
            if output is None:
                continue
            tail = "\n".join(output.splitlines()[-lines:]) if output.strip() else "(empty)"
            click.echo(f"--- {session} ---")
            click.echo(tail)
            click.echo()


if __name__ == "__main__":
    main()
