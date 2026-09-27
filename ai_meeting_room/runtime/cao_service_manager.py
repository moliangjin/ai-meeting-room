"""Explicit, fail-closed lifecycle recovery for the local CAO server."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

from ..product.operations import ProductDataPaths
from .tools import RuntimeToolResolver


class CaoServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _health_payload(url: str, timeout_seconds: float = 1.0) -> dict[str, object] | None:
    try:
        request = urllib.request.Request(f"{url.rstrip('/')}/health", headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            value = json.loads(response.read().decode("utf-8") or "{}")
            if response.status == 200 and isinstance(value, dict) and value.get("status") == "ok":
                return value
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None
    return None


def _port_is_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


class CaoServiceManager:
    """Manage one loopback CAO service as a persistent Product Runtime.

    Recovery is an explicit UI action. Healthy services are reused; an
    occupied but unhealthy port is never killed or overwritten. Services
    started here use the product runtime directory for CAO, XDG, and temporary
    state and are deliberately left alive when Product Shell exits, so a
    window restart cannot tear down a CAO session in use by another task.
    """

    ownership_mode = "PRODUCT_RUNTIME_MANAGED_PERSISTENT"

    def __init__(
        self,
        base_url: str,
        paths: ProductDataPaths,
        *,
        environment: Mapping[str, str] | None = None,
        health_probe: Callable[[str], dict[str, object] | None] = _health_payload,
        port_probe: Callable[[str, int], bool] = _port_is_open,
        executable_lookup: Callable[[str, str], str | None] | None = None,
        tool_resolver: RuntimeToolResolver | None = None,
        popen_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
        startup_timeout_seconds: float = 12.0,
        poll_interval_seconds: float = 0.2,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.paths = paths
        self.environment = dict(os.environ if environment is None else environment)
        self.tool_resolver = tool_resolver or RuntimeToolResolver(environment=self.environment)
        self.health_probe = health_probe
        self.port_probe = port_probe
        self.popen_factory = popen_factory
        self.startup_timeout_seconds = startup_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self._owned_process: subprocess.Popen[bytes] | None = None
        self._parsed = urlparse(self.base_url)
        self._host = self._parsed.hostname or ""
        self._port = self._parsed.port or (443 if self._parsed.scheme == "https" else 80)
        self._executable_lookup = executable_lookup

    def _validate_endpoint(self) -> None:
        if (
            self._parsed.scheme != "http"
            or self._host not in {"127.0.0.1", "localhost", "::1"}
            or self._parsed.username is not None
            or self._parsed.password is not None
            or self._parsed.query
            or self._parsed.fragment
            or self._parsed.path not in {"", "/"}
        ):
            raise CaoServiceError("CAO_LOCAL_ONLY_REQUIRED")

    def _launch_environment(self, cao_home: Path, temp_root: Path) -> dict[str, str]:
        env = self.tool_resolver.controlled_environment()
        # Codex CLI's existing local login remains available via the user's
        # HOME; no credentials are copied. Secret-bearing environment values
        # are not forwarded as an alternate authentication mechanism.
        env = {
            key: value for key, value in env.items()
            if not any(marker in key.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "COOKIE", "PASSWORD", "AUTHORIZATION"))
        }
        env.update({
            "CAO_HOME_DIR": str(cao_home),
            "CAO_TMP_DIR": str(temp_root),
            "TMPDIR": str(temp_root),
            "TMP": str(temp_root),
            "TEMP": str(temp_root),
            "TMUX_TMPDIR": str(self.paths.runtime / "tmux"),
            "XDG_CONFIG_HOME": str(self.paths.runtime / "xdg-config"),
            "XDG_CACHE_HOME": str(self.paths.runtime / "xdg-cache"),
            "XDG_DATA_HOME": str(self.paths.runtime / "xdg-data"),
        })
        return env

    def recover(self) -> dict[str, object]:
        self._validate_endpoint()
        payload = self.health_probe(self.base_url)
        if payload is not None:
            return self._ready_result("REUSED_HEALTHY", payload)
        if self.port_probe(self._host, self._port):
            raise CaoServiceError("CAO_PORT_CONFLICT")

        if self._executable_lookup is not None:
            executable = self._executable_lookup("cao-server", path=self.tool_resolver.controlled_path)
        else:
            resolution = self.tool_resolver.resolve_path("cao-server")
            executable = resolution.path if resolution.usable else None
        if not executable or not os.access(executable, os.X_OK):
            raise CaoServiceError("CAO_SERVER_EXECUTABLE_UNAVAILABLE")

        cao_home = self.paths.runtime / "cao"
        temp_root = self.paths.runtime / "tmp"
        tmux_root = self.paths.runtime / "tmux"
        for directory in (
            cao_home, temp_root, tmux_root,
            self.paths.runtime / "xdg-config", self.paths.runtime / "xdg-cache",
            self.paths.runtime / "xdg-data",
        ):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(cao_home, 0o700)
        except OSError:
            pass
        command_host = "127.0.0.1" if self._host == "localhost" else self._host
        try:
            self._owned_process = self.popen_factory(
                [executable, "--host", command_host, "--port", str(self._port), "--terminal", "tmux"],
                cwd=str(self.paths.root),
                env=self._launch_environment(cao_home, temp_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except OSError:
            raise CaoServiceError("CAO_START_FAILED") from None

        deadline = time.monotonic() + max(0.0, self.startup_timeout_seconds)
        while True:
            payload = self.health_probe(self.base_url)
            if payload is not None:
                return self._ready_result("STARTED_BY_PRODUCT", payload)
            if self._owned_process.poll() is not None:
                raise CaoServiceError("CAO_START_FAILED")
            if time.monotonic() >= deadline:
                break
            time.sleep(max(0.01, self.poll_interval_seconds))
        raise CaoServiceError("CAO_START_TIMEOUT")

    def _ready_result(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        backend = payload.get("terminal_backend")
        normalized_backend = str(backend).lower() if backend else "UNKNOWN"
        if normalized_backend != "tmux":
            raise CaoServiceError("CAO_TERMINAL_BACKEND_MISMATCH" if normalized_backend != "UNKNOWN" else "CAO_TERMINAL_BACKEND_UNKNOWN")
        return {
            "status": "READY",
            "action": action,
            "ownershipMode": self.ownership_mode,
            "terminalBackend": normalized_backend,
            "processPid": self._owned_process.pid if self._owned_process is not None else None,
            "persistent": True,
        }
