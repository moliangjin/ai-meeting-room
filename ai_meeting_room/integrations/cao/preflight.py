"""Fail-closed preflight for a real CAO/Codex runtime."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from ...runtime.tools import RuntimeToolResolver, ToolResolution


@dataclass(frozen=True)
class CaoPreflightResult:
    cao_url: str
    tools: dict[str, ToolResolution]
    cao_server_healthy: bool
    reason: str | None = None

    @property
    def usable(self) -> bool:
        return self.cao_server_healthy and all(tool.usable for tool in self.tools.values())

    def as_dict(self) -> dict[str, object]:
        return {
            "caoUrl": self.cao_url,
            "usable": self.usable,
            "caoServerHealthy": self.cao_server_healthy,
            "reason": self.reason,
            "tools": {name: tool.as_dict() for name, tool in self.tools.items()},
        }


class CaoRuntimePreflight:
    """Check CAO, tmux and Codex before a real Agent is admitted."""

    def __init__(
        self,
        cao_url: str,
        *,
        resolver: RuntimeToolResolver | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.cao_url = cao_url.rstrip("/")
        self.resolver = resolver or RuntimeToolResolver()
        self.timeout_seconds = timeout_seconds

    def check(self) -> CaoPreflightResult:
        tools = {
            # Product Runtime launches cao-server directly. Resolve it without
            # invoking its CLI because even a version/help command may
            # initialize CAO state before argument handling.
            "cao": self.resolver.resolve_path("cao-server"),
            "tmux": self.resolver.resolve("tmux", version_args=("-V",)),
            "codex": self.resolver.resolve("codex"),
        }
        healthy = False
        reason: str | None = None
        try:
            request = urllib.request.Request(f"{self.cao_url}/health", headers={"Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                healthy = response.status == 200
                payload = json.loads(response.read().decode("utf-8") or "{}")
                if healthy and isinstance(payload, dict) and payload.get("status") != "ok":
                    healthy = False
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            reason = "CAO_SERVER_UNHEALTHY"
        if not all(tool.usable for tool in tools.values()):
            reason = "MISSING_RUNTIME_DEPENDENCY"
        elif not healthy and reason is None:
            reason = "CAO_SERVER_UNHEALTHY"
        return CaoPreflightResult(self.cao_url, tools, healthy, reason)
