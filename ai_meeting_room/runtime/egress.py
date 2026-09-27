"""Deny-by-default egress policy contract.

The policy object is not itself a firewall. A SandboxRuntime must refuse to
start unless a healthy external gateway has been injected; there is no direct
network fallback in this module.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable
import subprocess


@dataclass(frozen=True)
class EgressRule:
    host: str
    ports: tuple[int, ...] = (443,)
    schemes: tuple[str, ...] = ("https",)
    reason: str = "explicitly approved runtime dependency"

    def __post_init__(self) -> None:
        if not self.host or self.host in {"*", "0.0.0.0/0"} or self.host.startswith("*.") and self.host.count(".") < 2:
            raise ValueError("egress host must be a concrete host or narrow subdomain")
        if any(port < 1 or port > 65535 for port in self.ports):
            raise ValueError("egress port out of range")


@dataclass(frozen=True)
class EgressAuditEvent:
    timestamp: str
    agent_id: str
    destination: str
    port: int
    decision: str
    reason: str


class EgressGateway:
    """External gateway boundary expected by the sandbox runtime."""

    def __init__(self, rules: Iterable[EgressRule] = (), audit_sink: Callable[[EgressAuditEvent], None] | None = None) -> None:
        self.rules = tuple(rules)
        self.audit_sink = audit_sink

    def health_check(self) -> bool:
        return False

    def decide(self, agent_id: str, destination: str, port: int, scheme: str = "https") -> bool:
        allowed = any(rule.host == destination and port in rule.ports and scheme in rule.schemes for rule in self.rules)
        event = EgressAuditEvent(datetime.now(timezone.utc).isoformat(), agent_id, destination, port, "ALLOW" if allowed else "DENY", "exact rule match" if allowed else "deny-by-default")
        if self.audit_sink:
            self.audit_sink(event)
        return allowed

    def policy_document(self) -> dict[str, object]:
        return {"default": "DENY", "rules": [asdict(rule) for rule in self.rules], "directInternetRoute": False}


class DockerEgressGateway(EgressGateway):
    """Health facade for a real sidecar proxy container.

    The sidecar is the enforcement point; this class never treats a missing
    sidecar as healthy and never exposes a direct-network fallback.
    """

    def __init__(self, container_name: str, rules: Iterable[EgressRule] = (), *, docker_executable: str = "/usr/local/bin/docker", audit_sink: Callable[[EgressAuditEvent], None] | None = None) -> None:
        super().__init__(rules, audit_sink)
        self.container_name = container_name
        self.docker_executable = docker_executable

    def health_check(self) -> bool:
        try:
            result = subprocess.run([self.docker_executable, "inspect", "--format", "{{.State.Running}}", self.container_name], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"
