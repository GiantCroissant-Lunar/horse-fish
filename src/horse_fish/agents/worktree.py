"""Git worktree manager for agent isolation."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field


class WorktreeInfo(BaseModel):
    """Information about a git worktree."""

    path: str
    branch: str
    name: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class WorktreeError(Exception):
    """Base exception for worktree operations."""

    pass


class WorktreeManager:
    """Manages git worktrees for agent isolation."""

    def __init__(self, repo_root: str, base_dir: str = ".horse-fish/worktrees") -> None:
        """Initialize the worktree manager.

        Args:
            repo_root: Path to the root of the git repository.
            base_dir: Relative directory under repo_root where worktrees are stored.
        """
        self.repo_root = Path(repo_root).resolve()
        self.base_dir = self.repo_root / base_dir

    async def _run_git(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
    ) -> tuple[int, str, str]:
        """Run a git command asynchronously.

        Args:
            args: Git command arguments.
            cwd: Working directory for the command.
            check: If True, raise WorktreeError on non-zero exit.

        Returns:
            Tuple of (returncode, stdout, stderr).

        Raises:
            WorktreeError: If check=True and command fails.
        """
        cmd = ["git", *args]
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or self.repo_root,
        )
        stdout, stderr = await process.communicate()
        stdout_str = stdout.decode().strip()
        stderr_str = stderr.decode().strip()

        if check and process.returncode != 0:
            raise WorktreeError(f"Git command failed: {' '.join(cmd)}\n{stderr_str}")

        return process.returncode, stdout_str, stderr_str

    async def create(self, name: str, base_branch: str = "main") -> WorktreeInfo:
        """Create a new worktree with a dedicated branch.

        Args:
            name: Name for this worktree (used in path and branch naming).
            base_branch: Branch to base the new worktree on.

        Returns:
            WorktreeInfo for the created worktree.

        Raises:
            WorktreeError: If worktree creation fails.
        """
        branch = f"horse-fish/{name}"
        path = self.base_dir / name

        # Remove existing worktree path if it exists
        if path.exists():
            await self._run_git("worktree", "remove", str(path), "--force", check=False)
            # Also clean up the directory if it still exists
            if path.exists():
                import shutil

                shutil.rmtree(path, ignore_errors=True)

        # Delete branch if it already exists
        await self._run_git("branch", "-D", branch, check=False)

        # Create the worktree
        await self._run_git(
            "worktree",
            "add",
            str(path),
            "-b",
            branch,
            base_branch,
        )

        return WorktreeInfo(
            path=str(path),
            branch=branch,
            name=name,
        )

    async def merge(self, name: str, auto_commit: bool = True) -> bool:
        """Merge a worktree's branch into the base branch.

        Args:
            name: Name of the worktree to merge.
            auto_commit: If True, stage and commit uncommitted changes first.

        Returns:
            True on success, False if there are merge conflicts.
        """
        branch = f"horse-fish/{name}"
        path = self.base_dir / name

        # Auto-commit uncommitted changes if requested
        if auto_commit:
            try:
                # Use --all to capture new (untracked) files and deletions
                await self._run_git(
                    "add",
                    "--all",
                    cwd=path,
                )
                await self._run_git(
                    "commit",
                    "-m",
                    f"Auto-commit before merge: {name}",
                    "--no-verify",
                    cwd=path,
                )
            except WorktreeError:
                # No changes to commit or commit failed, continue
                pass

        # Checkout main and merge
        try:
            await self._run_git("checkout", "main")
            await self._run_git("merge", "--no-ff", branch, "-m", f"Merge {branch}")
            return True
        except WorktreeError as e:
            if "conflict" in str(e).lower():
                # Abort the merge attempt
                await self._run_git("merge", "--abort", check=False)
                return False
            raise

    async def remove(self, name: str) -> None:
        """Remove a worktree and its associated branch.

        Args:
            name: Name of the worktree to remove.

        Raises:
            WorktreeError: If removal fails.
        """
        branch = f"horse-fish/{name}"
        path = self.base_dir / name

        # Remove the worktree
        if path.exists():
            await self._run_git("worktree", "remove", str(path), "--force", check=False)

        # Delete the branch
        await self._run_git("branch", "-D", branch, check=False)

    async def list_worktrees(self) -> list[WorktreeInfo]:
        """List all worktrees managed by horse-fish.

        Returns:
            List of WorktreeInfo for horse-fish worktrees.
        """
        try:
            _, stdout, _ = await self._run_git("worktree", "list", "--porcelain")
        except WorktreeError:
            return []

        worktrees: list[WorktreeInfo] = []
        current_worktree: dict[str, str] = {}

        for line in stdout.split("\n"):
            line = line.strip()
            if not line:
                # End of a worktree block
                if current_worktree and "branch" in current_worktree:
                    branch = current_worktree["branch"]
                    # Only include horse-fish branches
                    if "horse-fish/" in branch:
                        branch_name = branch.split("/")[-1]
                        worktree_name = branch_name
                        worktrees.append(
                            WorktreeInfo(
                                path=current_worktree.get("worktree", ""),
                                branch=branch,
                                name=worktree_name,
                            )
                        )
                current_worktree = {}
            elif line.startswith("worktree "):
                current_worktree["worktree"] = line[9:]
            elif line.startswith("branch "):
                branch = line[7:]
                # Strip refs/heads/ prefix if present
                if branch.startswith("refs/heads/"):
                    branch = branch[11:]
                current_worktree["branch"] = branch
            elif line.startswith("HEAD "):
                current_worktree["head"] = line[5:]

        # Handle last worktree block if not followed by blank line
        if current_worktree and "branch" in current_worktree:
            branch = current_worktree["branch"]
            if "horse-fish/" in branch:
                branch_name = branch.split("/")[-1]
                worktree_name = branch_name
                worktrees.append(
                    WorktreeInfo(
                        path=current_worktree.get("worktree", ""),
                        branch=branch,
                        name=worktree_name,
                    )
                )

        return worktrees

    async def cleanup(self, max_age_hours: int = 24) -> int:
        """Remove worktrees older than the specified age.

        Args:
            max_age_hours: Maximum age in hours before a worktree is removed.

        Returns:
            Number of worktrees removed.
        """
        worktrees = await self.list_worktrees()
        cutoff = datetime.now(UTC).timestamp() - (max_age_hours * 3600)
        removed = 0

        for wt in worktrees:
            path = Path(wt.path)
            if not path.exists():
                continue

            # Check the modification time of the worktree directory
            try:
                stat = path.stat()
                if stat.st_mtime < cutoff:
                    await self.remove(wt.name)
                    removed += 1
            except OSError:
                continue

        # Prune any stale worktree references
        await self._run_git("worktree", "prune", check=False)

        return removed

    async def get_diff(self, name: str, base_branch: str = "main") -> str:
        """Get the diff of all changes in a worktree (uncommitted + committed).

        Checks both uncommitted changes and new commits on the branch vs base.

        Args:
            name: Name of the worktree.
            base_branch: Branch to compare against for committed changes.

        Returns:
            Diff string showing all changes (uncommitted + committed ahead of base).
        """
        path = self.base_dir / name
        try:
            # First check for uncommitted changes
            await self._run_git("add", "-N", ".", cwd=path, check=False)
            _, uncommitted, _ = await self._run_git("diff", "HEAD", cwd=path, check=False)
            if uncommitted:
                return uncommitted

            # If no uncommitted changes, check for commits ahead of base branch
            branch = f"horse-fish/{name}"
            _, committed, _ = await self._run_git("diff", f"{base_branch}...{branch}", cwd=path, check=False)
            return committed
        except WorktreeError:
            return ""

    async def __aenter__(self) -> Self:
        """Async context manager entry."""
        return self

    async def __aexit__(self, *args: object) -> None:
        """Async context manager exit."""
        pass
