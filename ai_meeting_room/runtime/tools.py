"""Controlled discovery of host tools used by real agent runtimes."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping


class ToolStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    MISSING = "MISSING"
    UNUSABLE = "UNUSABLE"


@dataclass(frozen=True)
class ToolResolution:
    name: str
    path: str | None
    version: str | None
    status: ToolStatus
    reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.status == ToolStatus.AVAILABLE

    def as_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "path": self.path,
            "version": self.version,
            "status": self.status.value,
            "reason": self.reason,
        }


class RuntimeToolResolver:
    """Resolve one canonical executable per runtime tool.

    Explicit product overrides win, followed by the launching process PATH,
    injected project paths, and a small allowlist of known install locations.
    The controlled PATH passed to CAO uses the same ordering so its Codex
    provider resolves the same CLI that Product preflight authenticated.
    """

    _COMMON_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin")
    _SAFE_ENV_KEYS = ("HOME", "SHELL", "USER", "LANG", "LC_ALL", "TERM", "TMPDIR")
    _OVERRIDE_ENV_KEYS = {
        "codex": "AI_MEETING_ROOM_CODEX_PATH",
        "cao-server": "AI_MEETING_ROOM_CAO_SERVER_PATH",
        "tmux": "AI_MEETING_ROOM_TMUX_PATH",
        "cao": "AI_MEETING_ROOM_CAO_CLI_PATH",
    }
    _TOOL_ORDER = ("codex", "cao-server", "tmux", "cao")

    def __init__(self, *, environment: Mapping[str, str] | None = None, extra_paths: tuple[str, ...] = ()) -> None:
        self.environment = dict(environment or os.environ)
        self.extra_paths = extra_paths

    @property
    def controlled_path_entries(self) -> tuple[str, ...]:
        candidates: list[str] = []
        # A configured executable must also take precedence when CAO resolves
        # a provider by command name from its inherited PATH.
        for name in self._TOOL_ORDER:
            configured = self.environment.get(self._OVERRIDE_ENV_KEYS[name])
            if configured and Path(configured).expanduser().is_absolute():
                candidates.append(str(Path(configured).expanduser().parent))
        # Finder launches often have a minimal PATH, but when the process does
        # provide entries they retain precedence over fallback locations.
        candidates.extend(self.environment.get("PATH", "").split(os.pathsep))
        candidates.extend(self.extra_paths)
        home = Path(self.environment.get("HOME") or Path.home()).expanduser()
        candidates.extend((
            str(home / ".local" / "bin"),
            str(home / ".hermes" / "node" / "bin"),
        ))
        candidates.extend(self._COMMON_BIN_DIRS)
        result: list[str] = []
        seen: set[str] = set()
        for raw in candidates:
            if not raw:
                continue
            path = str(Path(raw).expanduser())
            if path in seen:
                continue
            seen.add(path)
            if Path(path).is_dir():
                result.append(path)
        return tuple(result)

    @property
    def controlled_path(self) -> str:
        return os.pathsep.join(self.controlled_path_entries)

    def resolve(
        self,
        name: str,
        *,
        version_args: tuple[str, ...] = ("--version",),
        timeout_seconds: float = 5.0,
        environment_overrides: Mapping[str, str] | None = None,
    ) -> ToolResolution:
        resolved = self.resolve_path(name)
        if not resolved.usable:
            return resolved
        path = resolved.path
        assert path is not None
        try:
            result = subprocess.run(
                [path, *version_args],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_seconds,
                env=self.controlled_environment(environment_overrides),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return ToolResolution(name, path, None, ToolStatus.UNUSABLE, f"version probe failed: {type(exc).__name__}")
        version = " ".join(result.stdout.split())[:240] if result.stdout else None
        if result.returncode != 0:
            return ToolResolution(name, path, version, ToolStatus.UNUSABLE, f"version probe exit={result.returncode}")
        return ToolResolution(name, path, version, ToolStatus.AVAILABLE)

    def resolve_path(self, name: str) -> ToolResolution:
        """Resolve and validate an absolute executable path without running it."""
        override_key = self._OVERRIDE_ENV_KEYS.get(name)
        configured = self.environment.get(override_key) if override_key else None
        if configured is not None:
            candidate = Path(configured).expanduser()
            if not candidate.is_absolute():
                return ToolResolution(name, str(candidate), None, ToolStatus.UNUSABLE, "explicit executable path must be absolute")
            if candidate.name != name:
                return ToolResolution(name, str(candidate), None, ToolStatus.UNUSABLE, "explicit executable filename does not match tool")
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                return ToolResolution(name, str(candidate), None, ToolStatus.UNUSABLE, "explicit executable is unavailable")
            return ToolResolution(name, str(candidate.resolve()), None, ToolStatus.AVAILABLE)

        path = shutil.which(name, path=self.controlled_path)
        if not path:
            return ToolResolution(name, None, None, ToolStatus.MISSING, "executable not found on controlled PATH")
        candidate = Path(path)
        if not os.access(candidate, os.X_OK):
            return ToolResolution(name, str(candidate), None, ToolStatus.UNUSABLE, "executable permission is missing")
        return ToolResolution(name, str(candidate.resolve()), None, ToolStatus.AVAILABLE)

    def controlled_environment(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = {key: self.environment[key] for key in self._SAFE_ENV_KEYS if key in self.environment}
        env["PATH"] = self.controlled_path
        if extra:
            env.update({str(key): str(value) for key, value in extra.items()})
        return env
