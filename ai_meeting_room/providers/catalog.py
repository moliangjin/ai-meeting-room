"""Provider-neutral availability catalog used by Meeting creation and UI."""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum


class ProviderAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    BLOCKED = "BLOCKED"
    DEGRADED = "DEGRADED"


@dataclass(frozen=True)
class ProviderInfo:
    provider_id: str
    display_name: str
    version: str
    runtime: str
    authentication: str
    availability: ProviderAvailability
    reason: str | None = None
    enabled: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "providerId": self.provider_id,
            "displayName": self.display_name,
            "version": self.version,
            "runtime": self.runtime,
            "authentication": self.authentication,
            "health": self.availability.value,
            "reason": self.reason,
            "enabled": self.enabled,
        }


class ProviderCatalog:
    """Static compatibility facts plus runtime availability overrides."""

    def __init__(self) -> None:
        self._providers: dict[str, ProviderInfo] = {
            "codex": ProviderInfo(
                "codex", "Codex", "CAO-backed", "CAO", "authenticated CLI",
                ProviderAvailability.AVAILABLE, enabled=True,
            ),
            "minimax": ProviderInfo(
                "minimax", "MiniMax Code", "0.4.2", "CAO/sandbox",
                "persistent CLI login", ProviderAvailability.BLOCKED,
                reason="PROVIDER_NETWORK_INCOMPATIBILITY", enabled=False,
            ),
            "workbuddy": ProviderInfo("workbuddy", "Workbuddy", "UNKNOWN", "UNKNOWN", "UNKNOWN", ProviderAvailability.UNAVAILABLE, "not integrated"),
            "zcode": ProviderInfo("zcode", "Zcode", "UNKNOWN", "UNKNOWN", "UNKNOWN", ProviderAvailability.UNAVAILABLE, "not integrated"),
            "claude": ProviderInfo("claude", "Claude Code", "UNKNOWN", "UNKNOWN", "UNKNOWN", ProviderAvailability.UNAVAILABLE, "not integrated"),
            "qwen": ProviderInfo("qwen", "Qwen", "UNKNOWN", "UNKNOWN", "UNKNOWN", ProviderAvailability.UNAVAILABLE, "not integrated"),
        }

    def get(self, provider_id: str) -> ProviderInfo:
        return self._providers[provider_id.lower()]

    def all(self) -> tuple[ProviderInfo, ...]:
        return tuple(self._providers.values())

    def can_join(self, provider_id: str) -> bool:
        info = self.get(provider_id)
        return info.enabled and info.availability == ProviderAvailability.AVAILABLE

    def mark_blocked(self, provider_id: str, reason: str) -> None:
        """Reflect a failed runtime preflight without changing other providers."""
        key = provider_id.lower()
        info = self.get(key)
        self._providers[key] = replace(info, availability=ProviderAvailability.BLOCKED, reason=reason, enabled=False)

    def mark_available(self, provider_id: str) -> None:
        """Restore a provider after a fresh successful preflight."""
        key = provider_id.lower()
        info = self.get(key)
        self._providers[key] = replace(info, availability=ProviderAvailability.AVAILABLE, reason=None, enabled=True)
