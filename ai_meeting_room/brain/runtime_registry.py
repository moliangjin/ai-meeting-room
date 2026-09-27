"""Single-instance registry for the formal browser Brain runtime."""

from __future__ import annotations

import os
from typing import Any, Callable
from uuid import uuid4


FORMAL_RUNTIME_TYPE = "PLAYWRIGHT_ATTACHED_REAL_CHROME"


class FormalBrainRuntimeRegistry:
    """Return one process-local formal Host for every Product entry point.

    This registry is deliberately process-local.  The Product Shell process is
    the sole owner of Playwright objects; Electron only forwards commands to
    this process over its loopback HTTP interface.
    """

    def __init__(
        self,
        host: Any,
        *,
        runtime_owner: str = "PRODUCT_SHELL",
        bridge_factory: Callable[[Any], Any] | None = None,
    ) -> None:
        self._host = host
        self._bridge_factory = bridge_factory
        self._wrapped_bridge: Any | None = None
        self.runtime_type = FORMAL_RUNTIME_TYPE
        self.registry_instance_id = f"registry-{uuid4()}"
        self.process_pid = os.getpid()
        self.parent_pid = os.getppid()
        self.runtime_owner = runtime_owner
        self._annotate_host()

    def _annotate_host(self) -> None:
        """Attach safe ownership metadata to the already-created Host."""
        for name, value in {
            "registry_instance_id": self.registry_instance_id,
            "process_pid": self.process_pid,
            "parent_pid": self.parent_pid,
            "runtime_owner": self.runtime_owner,
        }.items():
            try:
                setattr(self._host, name, value)
            except Exception:
                # A custom injected host may be immutable; the registry still
                # remains authoritative for its own identity.
                pass

    def get(self) -> Any:
        return self._host

    @property
    def wrapped_bridge(self) -> Any | None:
        return self._wrapped_bridge

    def connect(self, *, connect_attempt_id: str | None = None) -> dict[str, Any]:
        """Connect the Host, then initialize its optional provider-neutral Brain wrapper."""
        self._host.openBrain(connect_attempt_id=connect_attempt_id)
        if self._bridge_factory is not None:
            bridge = self._wrapped_bridge
            if bridge is None:
                bridge = self._bridge_factory(self._host)
            bridge.start()
            self._wrapped_bridge = bridge
        return self._host.status()

    def connect_url_only_for_diagnostics(self) -> dict[str, Any]:
        """Reconnect the same formal Host without invoking page-content resolvers."""
        return self._host.connect_url_only_for_diagnostics(ensure_if_missing=True)

    @property
    def host_instance_id(self) -> str:
        return str(getattr(self._host, "host_instance_id", "UNKNOWN"))

    def describe(self) -> dict[str, Any]:
        return {
            "formalRuntimeType": self.runtime_type,
            "registryInstanceId": self.registry_instance_id,
            "hostInstanceId": self.host_instance_id,
            "processPid": self.process_pid,
            "parentPid": self.parent_pid,
            "runtimeOwner": self.runtime_owner,
            "ownerThreadId": getattr(self._host, "owner_thread_id", "UNKNOWN"),
            "hostClass": type(self._host).__name__,
            "processLocal": True,
        }


class FormalBrainRuntimeService:
    """The sole Product Shell service boundary for the formal Brain runtime."""

    def __init__(self, registry: FormalBrainRuntimeRegistry) -> None:
        self.registry = registry

    def connect(self, *, connect_attempt_id: str | None = None) -> dict[str, Any]:
        return self.registry.connect(connect_attempt_id=connect_attempt_id)

    def connect_url_only_for_diagnostics(self) -> dict[str, Any]:
        return self.registry.connect_url_only_for_diagnostics()

    def ensure_chatgpt_page(self) -> dict[str, Any]:
        """Ensure a ChatGPT page through the one Registry-owned formal Host."""
        return self.registry.get().ensure_chatgpt_page()

    def bring_bound_page_to_front(self) -> dict[str, Any]:
        """Foreground the current Registry-owned Host's already-bound Page."""
        return self.registry.get().bring_bound_page_to_front()

    def get_chatgpt_structural_diagnostics(self) -> dict[str, Any]:
        """Read the fixed ChatGPT structural schema from the current bound Host."""
        return self.registry.get().get_chatgpt_structural_diagnostics()

    def connect_and_composer_probe(
        self,
        probe_text: str,
        *,
        allow_send: bool = False,
        connect_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        host = self.registry.get()
        return host.run_atomic_connect_and_composer_probe(
            probe_text,
            allow_send=allow_send,
            connect_attempt_id=connect_attempt_id,
        )

    def readonly_identity(self) -> dict[str, Any]:
        host = self.registry.get()
        identity = self.registry.describe()
        snapshot = host.runtime_identity_snapshot()
        return {
            **identity,
            "boundPageId": snapshot.get("boundPageId"),
            "ownerThreadId": snapshot.get("ownerThreadId"),
        }
