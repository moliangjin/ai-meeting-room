"""Safe runtime provenance shared by the Product Shell and diagnostics."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class RuntimeIdentity:
    """Immutable launch identity; no browser or credential data is included."""

    launch_session_id: str
    process_pid: int
    process_ppid: int
    process_started_at: str
    project_root: str
    source_root: str
    git_commit: str
    build_id: str

    @classmethod
    def create(cls, project_root: Path) -> "RuntimeIdentity":
        root = project_root.resolve()
        return cls(
            launch_session_id=os.environ.get("AI_MEETING_ROOM_LAUNCH_SESSION_ID", "UNKNOWN"),
            process_pid=os.getpid(),
            process_ppid=os.getppid(),
            process_started_at=_utc_now(),
            project_root=str(root),
            source_root=str(root),
            git_commit=os.environ.get("GIT_COMMIT", "UNKNOWN"),
            build_id=os.environ.get("APP_BUILD_ID", "UNTRACKED_WORKTREE"),
        )

    @staticmethod
    def module_metadata(paths: dict[str, Path]) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for name, raw_path in paths.items():
            path = raw_path.resolve()
            item: dict[str, str] = {"file": str(path), "sha256": "UNKNOWN"}
            try:
                if path.is_file():
                    item["sha256"] = _file_sha256(path)
            except OSError:
                pass
            result[name] = item
        return result

    def as_dict(
        self,
        *,
        registry: Any,
        host: Any,
        state: dict[str, Any],
        module_paths: dict[str, Path],
    ) -> dict[str, Any]:
        registry_info = registry.describe()
        return {
            "launchSessionId": self.launch_session_id,
            "pid": self.process_pid,
            "ppid": self.process_ppid,
            "startedAt": self.process_started_at,
            "projectRoot": self.project_root,
            "sourceRoot": self.source_root,
            "gitCommit": self.git_commit,
            "buildId": self.build_id,
            "registryInstanceId": registry_info.get("registryInstanceId", "UNKNOWN"),
            "hostInstanceId": str(getattr(host, "host_instance_id", "UNKNOWN")),
            "brainState": state.get("brainState", state.get("state", "UNKNOWN")),
            "authState": state.get("authState", "UNKNOWN"),
            "composerReady": bool(state.get("composerReady", False)),
            "browserConnected": bool(state.get("browserConnected", False)),
            "pageAlive": bool(state.get("pageAlive", False)),
            "boundPageId": state.get("boundPageId"),
            "runtimeOwner": registry_info.get("runtimeOwner", "UNKNOWN"),
            "ownerThreadId": state.get("ownerThreadId", getattr(host, "owner_thread_id", "UNKNOWN")),
            "threadMatrix": state.get("threadMatrix", {}),
            "threadAffinityError": state.get("threadAffinityError"),
            "processLocal": True,
            "moduleFiles": self.module_metadata(module_paths),
        }


__all__ = ["RuntimeIdentity"]
