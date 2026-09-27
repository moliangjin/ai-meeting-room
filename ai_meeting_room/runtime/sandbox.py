"""Provider-neutral sandbox runtime contract and Docker implementation."""

from __future__ import annotations

import os
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Sequence

from .egress import EgressGateway
from .errors import RuntimeErrorCode, RuntimeFailure


class SandboxState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    READY = "READY"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class SandboxUnavailable(RuntimeFailure):
    def __init__(self, message: str) -> None:
        super().__init__(RuntimeErrorCode.UNKNOWN, message)


@dataclass(frozen=True)
class SandboxAuditEvent:
    agent_id: str
    task_id: str | None
    timestamp: str
    event_type: str
    detail: str


@dataclass(frozen=True)
class SandboxSpec:
    name: str
    image: str
    worktree_path: str
    state_volume: str
    egress_network: str
    image_digest: str | None = None
    container_user: str = "1000:1000"
    workspace_path: str = "/workspace"
    home_path: str = "/home/mcode"
    memory: str = "4g"
    cpus: str = "2"
    pids_limit: int = 256
    agent_id: str = "minimax"
    proxy_url: str | None = "http://egress-gateway:3128"
    node_use_env_proxy: bool = False


class SandboxRuntimeAdapter(ABC):
    @abstractmethod
    def start(self) -> str: ...

    @abstractmethod
    def stop(self) -> None: ...

    @abstractmethod
    def interrupt(self) -> None: ...

    @abstractmethod
    def exec(self, command: Sequence[str]) -> subprocess.Popen[str]: ...

    @abstractmethod
    def get_status(self) -> str: ...

    @abstractmethod
    def health_check(self) -> bool: ...


class DockerSandboxRuntime(SandboxRuntimeAdapter):
    """Docker-compatible runtime with no host fallback and no privileged mode."""

    def __init__(self, spec: SandboxSpec, gateway: EgressGateway, *, docker_executable: str = "docker") -> None:
        self.spec = spec
        self.gateway = gateway
        self.docker_executable = docker_executable
        self.state = SandboxState.STOPPED
        self.last_error: str | None = None
        self.audit_events: list[SandboxAuditEvent] = []
        self._intentional_stop = False
        self._client_env = {"PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin", "DOCKER_CLI_HINTS": "false"}

    def record_audit(self, event_type: str, *, agent_id: str = "", task_id: str | None = None, detail: str = "") -> None:
        self.audit_events.append(SandboxAuditEvent(agent_id, task_id, datetime.now(timezone.utc).isoformat(), event_type, detail[:512]))

    def build_run_command(self) -> list[str]:
        worktree = Path(self.spec.worktree_path).resolve()
        if not worktree.is_dir() or worktree == Path("/") or worktree == Path.home():
            raise ValueError("worktree must be an existing narrow directory")
        if not self.spec.image_digest or not self.spec.image_digest.startswith("sha256:"):
            raise ValueError("sandbox image digest must be pinned before start")
        command = [
            self.docker_executable, "run", "--detach", "--name", self.spec.name,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--pids-limit", str(self.spec.pids_limit), "--memory", self.spec.memory,
            "--cpus", self.spec.cpus, "--user", self.spec.container_user,
            "--network", self.spec.egress_network,
            "--mount", f"type=bind,src={worktree},dst={self.spec.workspace_path}",
            # MiniMax Code 0.4.2 takes an agent-storage migration lock on a
            # sibling of the data root (for example /home/mcode/.minimax.lock).
            # Keep HOME private and ephemeral while leaving .minimax as the
            # dedicated persistent volume; a read-only rootfs alone is not
            # compatible with that real CLI behavior.
            "--tmpfs", f"{self.spec.home_path}:rw,noexec,nosuid,size=64m,mode=1777",
            "--mount", f"type=volume,src={self.spec.state_volume},dst={self.spec.home_path}/.minimax",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=512m",
            "--tmpfs", "/run:rw,noexec,nosuid,size=64m",
            "--env", f"HOME={self.spec.home_path}",
            "--env", f"MINIMAX_DATA_DIR={self.spec.home_path}/.minimax",
            "--env", f"MAVIS_DATA_DIR={self.spec.home_path}/.minimax",
            "--env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "--env", f"AI_MEETING_AGENT_ID={self.spec.agent_id}",
        ]
        if self.spec.proxy_url:
            command.extend(["--env", f"HTTP_PROXY={self.spec.proxy_url}", "--env", f"HTTPS_PROXY={self.spec.proxy_url}"])
        if self.spec.node_use_env_proxy:
            command.extend(["--env", "NODE_USE_ENV_PROXY=1"])
        pinned_image = f"{self.spec.image}@{self.spec.image_digest}"
        command.extend(["--env", "NO_PROXY=localhost,127.0.0.1,egress-gateway", pinned_image])
        return command

    def _docker(self, args: Sequence[str], *, check: bool = True, timeout: float = 20) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run([self.docker_executable, *args], env=self._client_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=check, timeout=timeout)
        except (OSError, subprocess.SubprocessError) as exc:
            self.last_error = f"docker runtime unavailable: {type(exc).__name__}"
            raise SandboxUnavailable(self.last_error) from exc

    def start(self) -> str:
        self.state = SandboxState.STARTING
        self._intentional_stop = False
        if not self.gateway.health_check():
            self.state = SandboxState.UNKNOWN
            self.record_audit("network_denial", detail="egress gateway unhealthy")
            raise SandboxUnavailable("egress gateway is not healthy; direct-network fallback is forbidden")
        try:
            self._docker(["info"], timeout=10)
            existing = self._docker(["inspect", self.spec.name], check=False)
            if existing.returncode == 0:
                started = self._docker(["start", self.spec.name], check=False)
                if started.returncode != 0:
                    raise SandboxUnavailable("existing sandbox could not be restarted")
            else:
                self._docker(self.build_run_command()[1:])
        except (RuntimeFailure, ValueError, subprocess.CalledProcessError) as exc:
            self.state = SandboxState.ERROR if isinstance(exc, ValueError) else SandboxState.UNKNOWN
            self.last_error = str(exc)
            self.record_audit("runtime_exit", detail=type(exc).__name__)
            raise
        self.state = SandboxState.READY
        return self.spec.name

    def stop(self) -> None:
        self._intentional_stop = True
        self._docker(["stop", "--timeout", "5", self.spec.name], check=False)
        self.state = SandboxState.STOPPED
        self.record_audit("runtime_exit", detail="container stopped")

    def interrupt(self) -> None:
        # Stopping the container terminates PID 1 and every docker-exec child.
        # This intentionally sacrifices the current task, while preserving the
        # named state volume for a later explicit recovery start.
        self._intentional_stop = True
        self._docker(["stop", "--timeout", "2", self.spec.name], check=False)
        self.state = SandboxState.PAUSED
        self.record_audit("process_violation", detail="container process tree interrupted")

    def exec(self, command: Sequence[str]) -> subprocess.Popen[str]:
        if self.state not in {SandboxState.READY, SandboxState.RUNNING}:
            self.record_audit("filesystem_denial", detail="exec rejected because sandbox is not ready")
            raise SandboxUnavailable("sandbox is not ready; host execution fallback is forbidden")
        self.state = SandboxState.RUNNING
        return subprocess.Popen([self.docker_executable, "exec", "--workdir", self.spec.workspace_path, "--env", f"HOME={self.spec.home_path}", "--env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", self.spec.name, *command], env=self._client_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)

    def get_status(self) -> str:
        result = self._docker(["inspect", "--format", "{{.State.Running}}", self.spec.name], check=False)
        if result.returncode != 0:
            return SandboxState.UNKNOWN.value
        if result.stdout.strip() == "true":
            return SandboxState.RUNNING.value
        return SandboxState.PAUSED.value if self._intentional_stop else SandboxState.UNKNOWN.value

    def health_check(self) -> bool:
        if not self.gateway.health_check():
            return False
        try:
            return self._docker(["inspect", "--format", "{{.State.Running}}", self.spec.name], check=False).stdout.strip() == "true"
        except SandboxUnavailable:
            return False
