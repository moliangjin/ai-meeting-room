"""Runtime-side defense-in-depth controls.

These controls are intentionally explicit about their limit: a child CLI that can
run arbitrary shell commands still needs an OS/container sandbox for hard denial.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from .errors import SecurityPolicyViolation


class SideEffectPolicy:
    DENY_BY_DEFAULT = "DENY_BY_DEFAULT"
    SOFT_ENFORCEMENT_ONLY = "SOFT_ENFORCEMENT_ONLY"
    _UNSAFE = ("external_message", "email", "feishu", "slack", "webhook", "upload", "remote_api_write", "install_software", "system_config", "credential_operation", "unknown")
    _COMMAND_DENY = ("lark-cli", "feishu", "slack", "discord", "telegram", "curl", "wget", "nc", "netcat", "ssh", "scp")

    def __init__(self, *, mode: str = DENY_BY_DEFAULT) -> None:
        self.mode = mode
        self.enforcement_level = self.SOFT_ENFORCEMENT_ONLY
        self.hard_enforcement = False

    def allows(self, category: str, *, explicitly_allowed: set[str] | None = None) -> bool:
        if category in self._UNSAFE or category == "unknown":
            return category in (explicitly_allowed or set())
        return category in (explicitly_allowed or set())

    def assert_local_command_allowed(self, command: str, workspace: str | Path) -> None:
        lowered = command.lower()
        tokens = []
        try:
            tokens = [token.lower() for token in shlex.split(command)]
        except ValueError as exc:
            raise SecurityPolicyViolation("cannot parse command safely") from exc
        if any(needle in lowered for needle in self._COMMAND_DENY) or any(token in {"git", "npm", "pip", "uv"} and i + 1 < len(tokens) and tokens[i + 1] in {"push", "install", "publish"} for i, token in enumerate(tokens)):
            raise SecurityPolicyViolation("command is denied by side-effect policy")
        if "http://" in lowered or "https://" in lowered:
            raise SecurityPolicyViolation("remote URL is denied by side-effect policy")
        if not self.is_path_within(workspace, workspace):
            raise SecurityPolicyViolation("invalid workspace")

    @staticmethod
    def is_path_within(root: str | Path, target: str | Path) -> bool:
        try:
            Path(target).resolve().relative_to(Path(root).resolve())
            return True
        except ValueError:
            return False


class EnvironmentSanitizer:
    ALLOWED = frozenset({"HOME", "PATH", "TERM", "LANG", "LC_ALL", "TMPDIR", "MINIMAX_DATA_DIR", "MAVIS_DATA_DIR", "XDG_CONFIG_HOME", "NO_COLOR"})
    SAFE_SYSTEM_PATH = ("/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin")

    def __init__(self, *, home: str | Path | None = None, safe_bin: str | Path | None = None, extra_allowed: set[str] | None = None) -> None:
        self.home = str(home or os.path.expanduser("~"))
        self.safe_bin = str(safe_bin) if safe_bin else None
        self.allowed = self.ALLOWED | frozenset(extra_allowed or set())

    def build_env(self, *, cwd: str | Path, extra: dict[str, str] | None = None) -> dict[str, str]:
        extra = extra or {}
        disallowed = set(extra) - set(self.allowed)
        if disallowed:
            raise ValueError(f"environment keys are not allowlisted: {sorted(disallowed)}")
        path_parts = ([self.safe_bin] if self.safe_bin else []) + list(self.SAFE_SYSTEM_PATH)
        env = {"HOME": self.home, "PATH": os.pathsep.join(path_parts), "TERM": "xterm-256color", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "NO_COLOR": "1"}
        env.update({key: value for key, value in extra.items() if value is not None})
        env["AI_MEETING_ROOM_RUNTIME"] = "minimax_exec"
        env["AI_MEETING_ROOM_WORKSPACE"] = str(Path(cwd).resolve())
        return env

    def sanitized_env_names(self, *, cwd: str | Path, extra: dict[str, str] | None = None) -> tuple[str, ...]:
        return tuple(sorted(self.build_env(cwd=cwd, extra=extra)))
