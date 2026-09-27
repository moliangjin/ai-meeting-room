"""Secret storage boundary for Brain credentials.

Only the Keychain backend is used on macOS.  The application never stores a
credential in SQLite, a normal file, browser storage, or a report.
"""

from __future__ import annotations

import platform
import subprocess
from abc import ABC, abstractmethod


class SecretStoreError(RuntimeError):
    pass


class BrainSecretStore(ABC):
    @abstractmethod
    def save(self, secret: str) -> str: ...

    @abstractmethod
    def exists(self, reference: str) -> bool: ...

    @abstractmethod
    def resolve(self, reference: str) -> str: ...

    @abstractmethod
    def delete(self, reference: str) -> bool: ...


class MacOSKeychainSecretStore(BrainSecretStore):
    """Use the user's login Keychain through the macOS ``security`` tool."""

    service = "AI Meeting Room Brain Provider"
    account = "ai-meeting-room-brain"

    def __init__(self, *, runner=subprocess.run) -> None:
        self._runner = runner
        if platform.system() != "Darwin":
            raise SecretStoreError("macOS Keychain backend is unavailable on this OS")

    def _base(self, reference: str) -> list[str]:
        if not reference or any(char in reference for char in "\r\n"):
            raise SecretStoreError("invalid secret reference")
        return ["/usr/bin/security", "find-generic-password", "-a", self.account, "-s", f"{self.service}:{reference}"]

    def save(self, secret: str) -> str:
        if not isinstance(secret, str) or not secret:
            raise SecretStoreError("secret is required")
        reference = "brain-provider"
        # Keep the credential out of argv/process listings.  ``security``
        # prompts when -w is the final option, so provide it through stdin.
        command = ["/usr/bin/security", "add-generic-password", "-a", self.account, "-s", f"{self.service}:{reference}", "-U", "-w"]
        result = self._runner(command, input=secret + "\n", capture_output=True, text=True, check=False)
        if getattr(result, "returncode", 1) != 0:
            raise SecretStoreError("Keychain save failed")
        return reference

    def exists(self, reference: str) -> bool:
        result = self._runner(self._base(reference), capture_output=True, text=True, check=False)
        return getattr(result, "returncode", 1) == 0

    def resolve(self, reference: str) -> str:
        command = self._base(reference) + ["-w"]
        result = self._runner(command, capture_output=True, text=True, check=False)
        if getattr(result, "returncode", 1) != 0:
            raise SecretStoreError("credential is not configured")
        value = getattr(result, "stdout", "").strip()
        if not value:
            raise SecretStoreError("credential is not configured")
        return value

    def delete(self, reference: str) -> bool:
        command = ["/usr/bin/security", "delete-generic-password", "-a", self.account, "-s", f"{self.service}:{reference}"]
        result = self._runner(command, capture_output=True, text=True, check=False)
        return getattr(result, "returncode", 1) == 0


class UnavailableSecretStore(BrainSecretStore):
    def save(self, secret: str) -> str:
        raise SecretStoreError("no supported secure secret backend")

    def exists(self, reference: str) -> bool:
        return False

    def resolve(self, reference: str) -> str:
        raise SecretStoreError("no supported secure secret backend")

    def delete(self, reference: str) -> bool:
        return False


def default_brain_secret_store() -> BrainSecretStore:
    if platform.system() == "Darwin":
        return MacOSKeychainSecretStore()
    return UnavailableSecretStore()
