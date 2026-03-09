"""Horse-fish CLI — agent swarm coordinator."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import click
from dotenv import load_dotenv

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

load_dotenv()

DB_PATH = ".horse-fish/state.db"


def _init_components(runtime: str, model: str | None, max_agents: int, planner_runtime: str | None = None):
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
    planner = Planner(runtime=planner_runtime or runtime, model=model if not planner_runtime else None)
    # Use user-specified model for agents; only fall back to planner default when no separate planner runtime
    effective_model = model or (planner.model if not planner_runtime else "")
    gates = ValidationGates()
    memory = MemoryStore()
    lesson_store = LessonStore(store)
    has_llm_key = os.environ.get("INCEPTION_API_KEY") or os.environ.get("DASHSCOPE_API_KEY")
    cognee_memory = CogneeMemory() if CogneeMemory and has_llm_key else None
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
@click.option("--planner-runtime", default=None, help="Runtime for planning (defaults to --runtime)")
def run(task: str, runtime: str, model: str | None, max_agents: int, planner_runtime: str | None):
    """Submit a task to the swarm."""
    orchestrator, store, _pool = _init_components(runtime, model, max_agents, planner_runtime)
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


_ENV_KEYS = [
    ("DASHSCOPE_API_KEY", "Pi runtime, Cognee fallback LLM", True),
    ("ZAI_API_KEY", "Droid runtime (Z.AI/GLM)", False),
    ("INCEPTION_API_KEY", "Cognee primary LLM (Mercury 2)", False),
    ("LANGFUSE_PUBLIC_KEY", "Langfuse observability", False),
    ("LANGFUSE_SECRET_KEY", "Langfuse observability", False),
]


@main.command("env-check")
def env_check():
    """Validate required environment keys are set."""
    all_ok = True
    for key, purpose, required in _ENV_KEYS:
        value = os.environ.get(key)
        if value:
            masked = value[:4] + "..." if len(value) > 4 else "***"
            click.echo(f"  ✓ {key}: {masked} ({purpose})")
        else:
            marker = "✗ MISSING" if required else "- not set"
            click.echo(f"  {marker} {key}: ({purpose})")
            if required:
                all_ok = False
    if not all_ok:
        click.echo("\nSome required keys are missing. Copy .env.example to .env and fill in values.")


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


def _format_duration(created_at: str | None, completed_at: str | None) -> str:
    """Format duration between two ISO timestamps."""
    if not created_at:
        return "N/A"
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return "N/A"
    if completed_at:
        try:
            completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            completed = datetime.now(UTC)
    else:
        completed = datetime.now(UTC)
    secs = int((completed - created).total_seconds())
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs // 3600}h {(secs % 3600) // 60}m"


@main.command()
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def stats(as_json: bool):
    """Show aggregate run statistics."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    store = Store(DB_PATH)
    store.migrate()

    try:
        data = store.fetch_run_stats()
        if as_json:
            click.echo(json.dumps(data, indent=2, default=str))
            return
        click.echo(f"Total runs: {data['total_runs']}")
        if data["by_state"]:
            parts = [f"{state}: {cnt}" for state, cnt in sorted(data["by_state"].items())]
            click.echo(f"By state:   {', '.join(parts)}")
        if data["avg_duration_secs"] is not None:
            secs = int(data["avg_duration_secs"])
            if secs < 60:
                dur = f"{secs}s"
            elif secs < 3600:
                dur = f"{secs // 60}m {secs % 60}s"
            else:
                dur = f"{secs // 3600}h {(secs % 3600) // 60}m"
            click.echo(f"Avg duration: {dur}")
        else:
            click.echo("Avg duration: N/A")
        if data["runtimes"]:
            click.echo("Runtimes:")
            for rt in data["runtimes"]:
                click.echo(f"  {rt['runtime']}: {rt['count']} subtask(s)")
        else:
            click.echo("Runtimes: none recorded")
    finally:
        store.close()


@main.command()
@click.argument("run_id", required=False)
@click.option("--recent", default=10, type=int, help="Show last N runs")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON")
def report(run_id: str | None, recent: int, as_json: bool):
    """Show run history and details."""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    store = Store(DB_PATH)
    store.migrate()

    try:
        if run_id:
            _report_detail(store, run_id, as_json)
        else:
            _report_recent(store, recent, as_json)
    finally:
        store.close()


def _report_recent(store: Store, limit: int, as_json: bool) -> None:
    query = """
    SELECT r.id, r.task, r.state, r.complexity, r.created_at, r.completed_at,
           COUNT(s.id) as subtask_count
    FROM runs r LEFT JOIN subtasks s ON r.id = s.run_id
    GROUP BY r.id ORDER BY r.created_at DESC LIMIT ?
    """
    runs = store.fetchall(query, (limit,))
    if not runs:
        click.echo("No runs found.")
        return
    if as_json:
        click.echo(json.dumps(runs, indent=2, default=str))
        return
    click.echo(f"{'ID':<10} {'State':<12} {'Complexity':<10} {'Duration':<10} {'Tasks':<6} {'Description'}")
    click.echo("-" * 90)
    for r in runs:
        rid = r["id"][:8]
        duration = _format_duration(r["created_at"], r["completed_at"])
        task = (r["task"] or "")[:35]
        if len(r["task"] or "") > 35:
            task += "..."
        click.echo(
            f"{rid:<10} {r['state'] or '':<12} {r['complexity'] or '-':<10} "
            f"{duration:<10} {r['subtask_count']:<6} {task}"
        )
    click.echo(f"\nShowing {len(runs)} run(s)")


def _report_detail(store: Store, run_id: str, as_json: bool) -> None:
    run = store.fetch_run(run_id)
    if not run:
        click.echo(f"Run '{run_id}' not found.")
        return
    subtasks = store.fetch_subtasks(run["id"])
    if as_json:
        click.echo(json.dumps({"run": run, "subtasks": subtasks}, indent=2, default=str))
        return
    duration = _format_duration(run["created_at"], run["completed_at"])
    click.echo(f"Run: {run['id']}")
    click.echo(f"Task: {run['task']}")
    click.echo(f"State: {run['state']}  Complexity: {run['complexity'] or '-'}  Duration: {duration}")
    click.echo(f"Created: {run['created_at']}  Completed: {run['completed_at'] or 'in-progress'}")
    if subtasks:
        click.echo(f"\nSubtasks ({len(subtasks)}):")
        click.echo(f"  {'ID':<10} {'State':<10} {'Agent':<16} {'Retries':<8} {'Description'}")
        click.echo(f"  {'-' * 80}")
        for s in subtasks:
            sid = s["id"][:8]
            agent = (s["agent_id"] or "-")[:14]
            click.echo(f"  {sid:<10} {s['state']:<10} {agent:<16} {s['retry_count']:<8} {s['description'][:40]}")
    else:
        click.echo("\nNo subtasks.")


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
@click.option("--record", is_flag=True, help="Record session with asciinema")
def dash(record: bool):
    """Live TUI dashboard (read-only)."""
    try:
        from horse_fish.dashboard.app import DashApp
    except ImportError:
        click.echo("Dashboard requires textual: pip install 'horse-fish[dashboard]'")
        return

    if record:
        import os
        import shutil
        from datetime import datetime

        if not shutil.which("asciinema"):
            click.echo("asciinema not found. Install with: brew install asciinema")
            return

        recordings_dir = Path("recordings")
        recordings_dir.mkdir(exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        cast_file = recordings_dir / f"dash-{timestamp}.cast"
        click.echo(f"Recording to {cast_file}")
        os.execvp("asciinema", ["asciinema", "rec", "--command", "hf dash", str(cast_file)])
    else:
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
