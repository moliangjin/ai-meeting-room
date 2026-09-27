"""Health checks for the host container runtime, without fallback semantics."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class HostRuntimeHealth:
    daemon_reachable: bool
    vm_runtime_alive: bool
    docker_api_usable: bool
    storage_usable: bool
    network_subsystem_usable: bool
    status: str
    reason: str | None = None


class DockerHostRuntimeHealth:
    def __init__(self, docker_executable: str = "/usr/local/bin/docker") -> None:
        self.docker_executable = docker_executable

    def _run(self, args: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run([self.docker_executable, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            return None

    def check(self) -> HostRuntimeHealth:
        version = self._run(["version", "--format", "{{.Server.Version}}"], 10)
        info = self._run(["info", "--format", "{{.DockerRootDir}}"], 15)
        networks = self._run(["network", "ls", "--format", "{{.Name}}"], 15)
        daemon = bool(version and version.returncode == 0 and version.stdout.strip())
        api = bool(info and info.returncode == 0 and info.stdout.strip())
        storage = api
        network = bool(networks and networks.returncode == 0)
        alive = daemon and api
        status = "HEALTHY" if all((daemon, alive, api, storage, network)) else ("UNKNOWN" if not daemon else "ERROR")
        reason = None if status == "HEALTHY" else "docker daemon/API/storage/network health could not be fully confirmed"
        return HostRuntimeHealth(daemon, alive, api, storage, network, status, reason)
