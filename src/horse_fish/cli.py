"""Horse-fish CLI — agent swarm coordinator."""

from __future__ import annotations

import asyncio
from pathlib import Path

import click

from horse_fish.agents.pool import AgentPool
from horse_fish.agents.tmux import TmuxManager
from horse_fish.agents.worktree import WorktreeManager
from horse_fish.memory.lessons import LessonStore
from horse_fish.memory.store import MemoryStore
from horse_fish.merge.queue import MergeQueue
from horse_fish.orchestrator.engine import Orchestrator
from horse_fish.planner.decompose import Planner
from horse_fish.store.db import Store
from horse_fish.validation.gates import ValidationGates

try:
    from horse_fish.memory.cognee_store import CogneeMemory
except ImportError:
    CogneeMemory = None  # type: ignore[assignment,misc]

DB_PATH = ".horse-fish/state.db"


def _init_components(runtime: str, model: str | None, max_agents: int):
    """Initialize all components needed for orchestration."""
    repo_root = str(Path.cwd())
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    store = Store(DB_PATH)
    store.migrate()
    tmux = TmuxManager()
    worktrees = WorktreeManager(repo_root)
    claude_md = Path.cwd() / "CLAUDE.md"
    project_context = claude_md.read_text() if claude_md.exists() else None
    pool = AgentPool(store, tmux, worktrees, project_context=project_context)
    planner = Planner(runtime=runtime, model=model)
    effective_model = planner.model  # resolved default if model was None
    gates = ValidationGates()
    memory = MemoryStore()
    lesson_store = LessonStore(store)
    cognee_memory = CogneeMemory() if CogneeMemory else None
    orchestrator = Orchestrator(
        pool=pool,
        planner=planner,
        gates=gates,
        runtime=runtime,
        model=effective_model,
        max_agents=max_agents,
        memory=memory,
        lesson_store=lesson_store,
        cognee_memory=cognee_memory,
        store=store,
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
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
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
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
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
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
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


@main.command()
def dash():
    """Live TUI dashboard (read-only)."""
    try:
        from horse_fish.dashboard.app import DashApp
    except ImportError:
        click.echo("Dashboard requires textual: pip install 'horse-fish[dashboard]'")
        return
    app = DashApp(db_path=DB_PATH)
    app.run()


@main.command()
@click.option("--runtime", default="pi", help="Runtime for smoke test agent")
@click.option("--model", default=None, help="Model override")
def smoke(runtime: str, model: str | None):
    """Run end-to-end smoke test with a real agent."""
    from horse_fish.smoke import (
        TASK_DESCRIPTION,
        cleanup,
        seed,
        verify_cognee,
        verify_lessons,
        verify_merge_commit,
        verify_test_passes,
    )

    repo_root = Path.cwd()
    results: list[tuple[str, bool, str]] = []

    # Phase 1: Seed
    click.echo("=== Phase 1: Seeding broken test ===")
    seed_sha = seed(repo_root)
    click.echo(f"Seeded at commit {seed_sha[:8]}")

    try:
        # Phase 2: Run pipeline
        click.echo("=== Phase 2: Running pipeline ===")
        orchestrator, store, pool = _init_components(runtime, model, max_agents=1)
        try:
            run_result = asyncio.run(orchestrator.run(TASK_DESCRIPTION))
            click.echo(f"Run {run_result.id}: {run_result.state}")
            for subtask in run_result.subtasks:
                click.echo(f"  [{subtask.state}] {subtask.description}")
        except Exception as exc:
            click.echo(f"Pipeline failed: {exc}")
            run_result = None
        finally:
            # Clean up agents regardless
            asyncio.run(pool.cleanup())
            store.close()

        # Phase 3: Verify
        click.echo("\n=== Phase 3: Verification ===")

        # Check 1: Pipeline completed
        if run_result and run_result.state.value == "completed":
            results.append(("pipeline_completed", True, "completed"))
        else:
            state = run_result.state.value if run_result else "no result"
            results.append(("pipeline_completed", False, state))

        # Check 2: Test passes on main
        passed, output = verify_test_passes(repo_root)
        results.append(("test_passes", passed, output.splitlines()[-1] if output.strip() else "no output"))

        # Check 3: Merge commit exists
        passed, log = verify_merge_commit(repo_root, seed_sha)
        results.append(("merge_commit", passed, log.splitlines()[0] if log.strip() else "no commits"))

        # Check 4: Cognee learned
        cognee_mem = CogneeMemory() if CogneeMemory else None
        passed, detail = asyncio.run(verify_cognee(cognee_mem))
        results.append(("cognee_learned", passed, detail))

        # Check 5: Lessons extracted
        if run_result:
            passed, detail = verify_lessons(run_result)
            results.append(("lessons_extracted", passed, detail))
        else:
            results.append(("lessons_extracted", False, "no run result"))

        # Phase 4: Report
        click.echo("\n=== Phase 4: Results ===")
        all_passed = True
        for name, passed, detail in results:
            status = "PASS" if passed else "FAIL"
            if not passed and "skipped" in detail:
                status = "SKIP"
            else:
                all_passed = all_passed and passed
            click.echo(f"  [{status}] {name}: {detail}")

        click.echo(f"\n{'ALL CHECKS PASSED' if all_passed else 'SOME CHECKS FAILED'}")

    finally:
        # Phase 5: Cleanup
        click.echo("\n=== Phase 5: Cleanup ===")
        cleanup(repo_root, seed_sha)
        click.echo("Cleaned up seed files")


if __name__ == "__main__":
    main()
