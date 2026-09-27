"""Validation and persistence-facing models for Brain Provider settings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from .provider import BrainProviderConfig


class BrainProviderConfigError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class ValidatedBrainProviderConfig:
    config: BrainProviderConfig
    secret_reference: str

    def as_public_dict(self, credential_configured: bool) -> dict[str, Any]:
        return {
            "provider": self.config.provider,
            "model": self.config.model,
            "baseUrl": self.config.endpoint,
            "timeout": self.config.timeout,
            "enabled": self.config.enabled,
            "secretReference": self.secret_reference,
            "credentialConfigured": credential_configured,
        }


class BrainProviderConfigValidator:
    allowed_providers = frozenset({"openai-compatible"})

    @classmethod
    def validate(cls, payload: Mapping[str, Any], *, credential_configured: bool) -> ValidatedBrainProviderConfig:
        provider = str(payload.get("provider") or "openai-compatible").strip().lower()
        model = str(payload.get("model") or "").strip()
        base_url = str(payload.get("baseUrl") or payload.get("endpoint") or "").strip()
        secret_reference = str(payload.get("secretReference") or "brain-provider").strip()
        try:
            timeout = float(payload.get("timeout", 60))
        except (TypeError, ValueError):
            raise BrainProviderConfigError("CONFIG_REQUIRED", "timeout must be a number") from None
        enabled = bool(payload.get("enabled", True))
        if provider not in cls.allowed_providers:
            raise BrainProviderConfigError("CONFIG_REQUIRED", "unsupported provider")
        if not model:
            raise BrainProviderConfigError("CONFIG_REQUIRED", "model is required")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise BrainProviderConfigError("CONFIG_REQUIRED", "base URL must be an HTTP(S) URL")
        if not 1 <= timeout <= 600:
            raise BrainProviderConfigError("CONFIG_REQUIRED", "timeout must be between 1 and 600 seconds")
        if not secret_reference or any(char in secret_reference for char in "\r\n"):
            raise BrainProviderConfigError("CONFIG_REQUIRED", "invalid secret reference")
        if not credential_configured:
            raise BrainProviderConfigError("CONFIG_REQUIRED", "credential is required")
        return ValidatedBrainProviderConfig(
            BrainProviderConfig(provider, model, base_url, "", timeout, 0, enabled),
            secret_reference,
        )
