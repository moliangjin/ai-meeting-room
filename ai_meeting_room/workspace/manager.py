"""Git worktree manager; physical writes never share one working directory."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..models import ChangeSet


class WorkspaceError(RuntimeError):
    pass


class WorkspaceManager:
    def __init__(self, repository: str | Path, worktree_root: str | Path | None = None) -> None:
        self.repository = Path(repository).expanduser().resolve()
        self.worktree_root = Path(worktree_root or self.repository / ".ai-meeting-worktrees").expanduser().resolve()
        self._worktrees: dict[str, Path] = {}
        if not (self.repository / ".git").exists():
            raise WorkspaceError(f"not a git repository: {self.repository}")

    def _run(self, *args: str, cwd: Path | None = None) -> str:
        process = subprocess.run(args, cwd=cwd or self.repository, text=True, capture_output=True)
        if process.returncode != 0:
            raise WorkspaceError(process.stderr.strip() or f"command failed: {' '.join(args)}")
        return process.stdout

    def create_agent_worktree(self, agent_id: str, task_id: str, *, baseline: str = "HEAD") -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{agent_id}-{task_id}").strip("-")
        path = (self.worktree_root / safe).resolve()
        if self.worktree_root not in path.parents:
            raise WorkspaceError("worktree path escaped configured root")
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        branch = f"ai-meeting/{safe}"
        self._run("git", "worktree", "add", "-b", branch, str(path), baseline)
        self._worktrees[f"{agent_id}:{task_id}"] = path
        return path

    def remove_agent_worktree(self, agent_id: str, task_id: str) -> None:
        key = f"{agent_id}:{task_id}"
        path = self._worktrees.get(key)
        if path is None:
            raise WorkspaceError(f"unknown worktree: {key}")
        # The target is an exact registry-owned agent worktree; force is
        # required to clean an uncommitted task sandbox without touching the
        # canonical repository or any sibling worktree.
        self._run("git", "worktree", "remove", "--force", str(path))
        self._worktrees.pop(key, None)

    def get_diff(self, path: str | Path, baseline: str = "HEAD") -> str:
        worktree = Path(path).expanduser().resolve()
        self._assert_known_or_repository(worktree)
        return self._run("git", "diff", baseline, "--", cwd=worktree)

    def get_status(self, path: str | Path) -> str:
        worktree = Path(path).expanduser().resolve()
        self._assert_known_or_repository(worktree)
        return self._run("git", "status", "--short", cwd=worktree)

    def create_change_set(self, agent_id: str, task_id: str, path: str | Path, branch: str, commit: str | None = None) -> ChangeSet:
        diff = self.get_diff(path)
        files = tuple(line[3:] for line in self.get_status(path).splitlines() if len(line) >= 4)
        return ChangeSet(agent_id, task_id, branch, commit, diff, files)

    def health_check(self) -> bool:
        try:
            return self._run("git", "rev-parse", "--show-toplevel").strip() == str(self.repository)
        except WorkspaceError:
            return False

    def _assert_known_or_repository(self, path: Path) -> None:
        if path != self.repository and self.worktree_root not in path.parents:
            raise WorkspaceError(f"path outside workspace manager: {path}")
