"""Horse-fish CLI — agent swarm coordinator."""

import click


@click.group()
@click.version_option(version="0.1.0")
def main():
    """Horse-fish: agent swarm coordinator on top of overstory."""


@main.command()
@click.argument("task", type=str)
@click.option("--runtime", default="claude", help="Default runtime for agents")
def run(task: str, runtime: str):
    """Submit a task to the swarm."""
    click.echo(f"Submitting task: {task}")
    click.echo(f"Runtime: {runtime}")


if __name__ == "__main__":
    main()
