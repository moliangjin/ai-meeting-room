"""Provider-neutral Brain completion boundary.

The provider owns network and credential lookup.  The rest of the application
sees only a safe health record and response text; credentials are never part
of configuration objects, persistence, or diagnostics.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse


class BrainProviderError(RuntimeError):
    """Safe, non-secret provider failure."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class BrainProviderHealth:
    healthy: bool
    status: str
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"healthy": self.healthy, "status": self.status, "reason": self.reason}


@dataclass(frozen=True)
class BrainProviderConfig:
    """Public provider configuration; ``authentication_reference`` is a name.

    It is deliberately not a secret value.  The referenced environment
    variable is read only for the duration of a request and is never returned
    by ``as_public_dict``.
    """

    provider: str = "openai-compatible"
    model: str = ""
    endpoint: str = ""
    authentication_reference: str = "OPENAI_API_KEY"
    timeout: float = 60.0
    max_retries: int = 0
    enabled: bool = True

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "BrainProviderConfig":
        env = os.environ if environ is None else environ
        enabled = env.get("AIMR_BRAIN_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"}
        try:
            timeout = max(1.0, float(env.get("AIMR_BRAIN_TIMEOUT", "60")))
        except ValueError:
            timeout = 60.0
        try:
            retries = max(0, min(3, int(env.get("AIMR_BRAIN_MAX_RETRIES", "0"))))
        except ValueError:
            retries = 0
        return cls(
            provider=env.get("AIMR_BRAIN_PROVIDER", "openai-compatible").strip().lower(),
            model=env.get("AIMR_BRAIN_MODEL", "").strip(),
            endpoint=env.get("AIMR_BRAIN_ENDPOINT", "").strip(),
            authentication_reference=env.get("AIMR_BRAIN_AUTH_REFERENCE", "OPENAI_API_KEY").strip(),
            timeout=timeout,
            max_retries=retries,
            enabled=enabled,
        )

    def has_credential(self, environ: Mapping[str, str] | None = None) -> bool:
        env = os.environ if environ is None else environ
        return bool(self.authentication_reference and env.get(self.authentication_reference))

    def as_public_dict(self, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model or "UNKNOWN",
            "endpointConfigured": bool(self.endpoint),
            "authenticationReference": self.authentication_reference or "UNKNOWN",
            "credentialPresent": self.has_credential(environ),
            "timeout": self.timeout,
            "maxRetries": self.max_retries,
            "enabled": self.enabled,
        }


class BrainProvider:
    """Small interface for formal Brain providers."""

    def health_check(self) -> BrainProviderHealth:
        raise NotImplementedError

    def complete(self, prompt: str) -> str:
        raise NotImplementedError


class OpenAICompatibleProvider(BrainProvider):
    """One configurable OpenAI-compatible chat-completions provider.

    The endpoint is always supplied by configuration.  No vendor hostname,
    API key, or fallback provider is hardcoded here.
    """

    def __init__(
        self,
        config: BrainProviderConfig,
        *,
        environ: Mapping[str, str] | None = None,
        opener: Any = urllib.request.urlopen,
    ) -> None:
        self.config = config
        self._environ = os.environ if environ is None else environ
        self._opener = opener

    def health_check(self) -> BrainProviderHealth:
        if not self.config.enabled:
            return BrainProviderHealth(False, "DISABLED", "provider disabled")
        if self.config.provider != "openai-compatible":
            return BrainProviderHealth(False, "UNSUPPORTED", "provider type is not supported")
        if not self.config.model:
            return BrainProviderHealth(False, "CONFIG_REQUIRED", "model is not configured")
        parsed = urlparse(self.config.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return BrainProviderHealth(False, "CONFIG_REQUIRED", "endpoint is not configured")
        if not self.config.authentication_reference or not self.config.has_credential(self._environ):
            return BrainProviderHealth(False, "AUTH_REQUIRED", "credential is not available")
        return BrainProviderHealth(True, "READY")

    def complete(self, prompt: str) -> str:
        if not isinstance(prompt, str) or not prompt.strip():
            raise BrainProviderError("INVALID_REQUEST", "prompt is required")
        health = self.health_check()
        if not health.healthy:
            raise BrainProviderError(health.status, health.reason)

        # Lookup by name only.  Do not retain the value in config or return it
        # in an exception/report.
        secret = self._environ[self.config.authentication_reference]
        body = json.dumps({
            "model": self.config.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }).encode("utf-8")
        request = urllib.request.Request(
            self.config.endpoint,
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {secret}"},
            method="POST",
        )
        for attempt in range(self.config.max_retries + 1):
            try:
                with self._opener(request, timeout=self.config.timeout) as response:
                    raw = response.read()
                return self._extract_text(raw)
            except urllib.error.HTTPError as exc:
                if exc.code == 401 or exc.code == 403:
                    raise BrainProviderError("AUTH_REQUIRED", "provider authentication failed") from None
                if exc.code == 429:
                    raise BrainProviderError("RATE_LIMITED", "provider rate limited") from None
                if exc.code >= 500 and attempt < self.config.max_retries:
                    continue
                raise BrainProviderError("PROVIDER_ERROR", f"provider returned HTTP {exc.code}") from None
            except (TimeoutError, socket.timeout):
                if attempt < self.config.max_retries:
                    continue
                raise BrainProviderError("TIMEOUT", "provider request timed out") from None
            except urllib.error.URLError:
                if attempt < self.config.max_retries:
                    continue
                raise BrainProviderError("NETWORK_ERROR", "provider network request failed") from None
            except BrainProviderError:
                raise
            except (json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
                raise BrainProviderError("INVALID_RESPONSE", "provider response was not compatible") from None

        raise BrainProviderError("PROVIDER_ERROR")

    @staticmethod
    def _extract_text(raw: bytes) -> str:
        payload = json.loads(raw.decode("utf-8"))
        choices = payload["choices"]
        content = choices[0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise BrainProviderError("INVALID_RESPONSE", "provider response contained no text")
        return content
