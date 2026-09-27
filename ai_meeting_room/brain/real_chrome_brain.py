"""User-owned Chrome Brain boundary.

The connector is deliberately injected. Product code never launches Chrome,
enumerates browser tabs, reads browser storage, or discovers a debugging
endpoint. An approved official Chrome connector supplies exactly one
user-authorized ChatGPT target and exposes only operations on that target.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from .chatgpt_web import BrowserController


CHATGPT_HOST = "chatgpt.com"
CHATGPT_URL = "https://chatgpt.com/"
REAL_CHROME_BROWSER_RUNTIME = "USER_OWNED_REAL_CHROME_SESSION"
DEDICATED_REAL_CHROME_PROFILE_NAME = "AI Meeting Room"
MCP_AUTOCONNECT_PACKAGE = "chrome-devtools-mcp@latest"
MCP_AUTOCONNECT_FLAG = "--autoConnect"
MIN_CHROME_VERSION = (144, 0, 0, 0)


class RealChromeBrainError(RuntimeError):
    pass


class RealChromeBrainState:
    STOPPED = "STOPPED"
    WAITING_USER_AUTHORIZATION = "WAITING_USER_AUTHORIZATION"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
    READY = "READY"
    THINKING = "THINKING"
    WAITING_RESPONSE = "WAITING_RESPONSE"
    PARSING = "PARSING"
    PAGE_LOST = "PAGE_LOST"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AuthorizedChatGPTTarget:
    """Opaque target reference returned after the user authorizes one tab."""

    target_reference: str
    url: str
    conversation_url: str | None = None
    browser_session_reference: str | None = None


class ChatGPTTargetPolicy:
    """Application policy for the one user-authorized target."""

    @staticmethod
    def allows(raw_url: str) -> bool:
        return is_allowed_chatgpt_url(raw_url)

    @staticmethod
    def describe() -> str:
        return "CHATGPT_ONLY"


def check_chrome_version(raw_version: str | None) -> str:
    """Return PASS/FAIL/UNKNOWN without probing or launching Chrome."""
    if not raw_version:
        return "UNKNOWN"
    match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?", str(raw_version))
    if not match:
        return "UNKNOWN"
    actual = tuple(int(part or 0) for part in match.groups())
    return "PASS" if actual >= MIN_CHROME_VERSION else "FAIL"


class OfficialChromeConnector(Protocol):
    """Adapter contract for Chrome's official user-authorized connector.

    The implementation may use Chrome DevTools MCP auto-connect or a later
    official mechanism. It must not return a list of tabs to this application;
    it returns only the one target selected by the user. The connector owns
    the profile-wide capability; this application enforces ChatGPT-only use.
    """

    def request_user_authorization(self) -> None: ...

    def authorized_chatgpt_target(self) -> AuthorizedChatGPTTarget | None: ...

    def target_status(self, target: AuthorizedChatGPTTarget) -> str: ...

    def open_conversation(self, target: AuthorizedChatGPTTarget, url: str) -> AuthorizedChatGPTTarget: ...

    def send_prompt(self, target: AuthorizedChatGPTTarget, prompt: str) -> None: ...

    def wait_for_completion(self, target: AuthorizedChatGPTTarget, timeout_seconds: float) -> None: ...

    def read_response(self, target: AuthorizedChatGPTTarget) -> str: ...

    def detach(self) -> None: ...


def is_allowed_chatgpt_url(raw_url: str) -> bool:
    try:
        parsed = urlparse(raw_url)
        if parsed.scheme != "https" or parsed.hostname != CHATGPT_HOST or parsed.username or parsed.password:
            return False
        path = parsed.path or "/"
        return path == "/" or path.startswith("/c/") or path.startswith("/g/")
    except Exception:
        return False


def is_allowed_conversation_id(conversation_id: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9-]+", conversation_id))


class RealChromeBrainTransport(BrowserController):
    """BrowserController over one explicitly authorized user Chrome target."""

    def __init__(self, host: "RealChromeBrainHost") -> None:
        self.host = host

    def connect(self) -> None:
        self.host.connect()

    def auth_status(self) -> str:
        return self.host.auth_status()

    def open_conversation(self, conversation_id: str | None = None) -> str:
        return self.host.open_conversation(conversation_id)

    def send_prompt(self, prompt: str) -> None:
        self.host.send_prompt(prompt)

    def wait_for_completion(self, timeout_seconds: float) -> None:
        self.host.wait_for_completion(timeout_seconds)

    def read_response(self) -> str:
        return self.host.read_response()

    def close(self) -> None:
        self.host.close()


class RealChromeBrainHost(BrowserController):
    """Fail-closed host for a single user-authorized ChatGPT tab."""

    def __init__(self, *, connector: OfficialChromeConnector | None = None, on_failure: Callable[[str, str], None] | None = None) -> None:
        self.connector = connector
        self.on_failure = on_failure
        self.state = RealChromeBrainState.STOPPED
        self.auth_state = "UNKNOWN"
        self.last_error: str | None = None
        self.target: AuthorizedChatGPTTarget | None = None
        self.conversation_url: str | None = None
        self._last_failure: tuple[str, str] | None = None
        self.chrome_version: str | None = None

    def _fail(self, state: str, reason: str) -> None:
        self.state = state
        self.last_error = reason
        signature = (state, reason)
        if signature != self._last_failure and self.on_failure is not None:
            self._last_failure = signature
            self.on_failure(state, reason)

    def attach_connector(self, connector: OfficialChromeConnector) -> dict[str, Any]:
        self.connector = connector
        return self.openBrain()

    def connect(self) -> None:
        if self.connector is None:
            # The real MCP connector is owned by Electron/Node. A Python
            # status/open compatibility call must remain passive and must not
            # claim that Chrome authorization is pending before the first
            # browser discovery call was dispatched by the official SDK.
            self.state = RealChromeBrainState.STOPPED
            return
        self.state = RealChromeBrainState.CONNECTING
        try:
            target = self.connector.authorized_chatgpt_target()
            if target is None:
                self.connector.request_user_authorization()
                self.state = RealChromeBrainState.WAITING_USER_AUTHORIZATION
                return
            if check_chrome_version(self.chrome_version) == "FAIL":
                self._fail(RealChromeBrainState.ERROR, "CHROME_VERSION_UNSUPPORTED")
                raise RealChromeBrainError("CHROME_VERSION_UNSUPPORTED")
            if not ChatGPTTargetPolicy.allows(target.url):
                raise RealChromeBrainError("NON_CHATGPT_TARGET_REJECTED")
            self.target = target
            self.auth_state = self.connector.target_status(target)
            self._apply_target_status(self.auth_state)
        except RealChromeBrainError:
            raise
        except Exception as exc:
            self._fail(RealChromeBrainState.ERROR, "REAL_CHROME_CONNECTOR_ERROR")
            raise RealChromeBrainError("REAL_CHROME_CONNECTOR_ERROR") from exc

    def _apply_target_status(self, status: str) -> None:
        self.auth_state = status
        if status == "AUTHENTICATED":
            self.state = RealChromeBrainState.READY
            self.last_error = None
            self._last_failure = None
        elif status == "CHALLENGE_REQUIRED":
            self._fail(RealChromeBrainState.CHALLENGE_REQUIRED, status)
        elif status == "AUTH_REQUIRED":
            self._fail(RealChromeBrainState.AUTH_REQUIRED, status)
        elif status in {"PAGE_LOST", "UNKNOWN"}:
            self._fail(RealChromeBrainState.PAGE_LOST if status == "PAGE_LOST" else RealChromeBrainState.UNKNOWN, status)
        else:
            self._fail(RealChromeBrainState.UNKNOWN, status)

    def getPage(self) -> AuthorizedChatGPTTarget:
        if self.target is None:
            raise RealChromeBrainError("CHATGPT_TARGET_NOT_BOUND")
        return self.target

    def get_page(self) -> AuthorizedChatGPTTarget:
        return self.getPage()

    def openBrain(self) -> dict[str, Any]:
        self.start()
        return self.status()

    def open(self) -> dict[str, Any]:
        return self.openBrain()

    def start(self) -> None:
        self.connect()

    def checkHealth(self) -> bool:
        try:
            return self.auth_status() == "AUTHENTICATED"
        except Exception:
            return False

    def recover(self) -> bool:
        self.close()
        self.start()
        return self.checkHealth()

    def auth_status(self) -> str:
        if self.connector is None or self.target is None:
            return self.auth_state
        try:
            current_target = self.connector.authorized_chatgpt_target()
            if current_target is None or current_target.target_reference != self.target.target_reference:
                self._fail(RealChromeBrainState.PAGE_LOST, "AUTHORIZED_CHATGPT_PAGE_LOST")
                return "PAGE_LOST"
            self.target = current_target
            if not ChatGPTTargetPolicy.allows(current_target.url):
                self._fail(RealChromeBrainState.ERROR, "NAVIGATION_AWAY_FROM_CHATGPT")
                return "NAVIGATION_AWAY_FROM_CHATGPT"
            status = self.connector.target_status(current_target)
            self._apply_target_status(status)
            return status
        except Exception as exc:
            self._fail(RealChromeBrainState.PAGE_LOST, "AUTHORIZED_CHATGPT_PAGE_LOST")
            raise RealChromeBrainError("AUTHORIZED_CHATGPT_PAGE_LOST") from exc

    def open_conversation(self, conversation_id: str | None = None) -> str:
        target = self.getPage()
        if self.connector is None:
            raise RealChromeBrainError("REAL_CHROME_CONNECTOR_UNAVAILABLE")
        url = CHATGPT_URL if conversation_id is None else f"{CHATGPT_URL}c/{conversation_id}"
        if conversation_id is not None and not is_allowed_conversation_id(conversation_id):
            raise RealChromeBrainError("INVALID_CONVERSATION_BINDING")
        if not ChatGPTTargetPolicy.allows(url):
            raise RealChromeBrainError("NON_CHATGPT_TARGET_REJECTED")
        self.target = self.connector.open_conversation(target, url)
        if not ChatGPTTargetPolicy.allows(self.target.url):
            self._fail(RealChromeBrainState.ERROR, "NAVIGATION_AWAY_FROM_CHATGPT")
            raise RealChromeBrainError("NAVIGATION_AWAY_FROM_CHATGPT")
        self.conversation_url = self.target.url
        return self.target.url

    def send_prompt(self, prompt: str) -> None:
        target = self.getPage()
        if self.connector is None or self.auth_status() != "AUTHENTICATED":
            raise RealChromeBrainError("CHATGPT_NOT_READY")
        self.state = RealChromeBrainState.THINKING
        try:
            self.connector.send_prompt(target, prompt)
        except Exception as exc:
            self._fail(RealChromeBrainState.ERROR, "PROMPT_SEND_FAILED")
            raise RealChromeBrainError("PROMPT_SEND_FAILED") from exc

    def wait_for_completion(self, timeout_seconds: float) -> None:
        target = self.getPage()
        if self.connector is None:
            raise RealChromeBrainError("REAL_CHROME_CONNECTOR_UNAVAILABLE")
        self.state = RealChromeBrainState.WAITING_RESPONSE
        try:
            self.connector.wait_for_completion(target, timeout_seconds)
        except TimeoutError:
            self._fail(RealChromeBrainState.ERROR, "CHATGPT_RESPONSE_TIMEOUT")
            raise
        except Exception as exc:
            self._fail(RealChromeBrainState.PAGE_LOST, "AUTHORIZED_CHATGPT_PAGE_LOST")
            raise RealChromeBrainError("AUTHORIZED_CHATGPT_PAGE_LOST") from exc

    def read_response(self) -> str:
        target = self.getPage()
        if self.connector is None:
            raise RealChromeBrainError("REAL_CHROME_CONNECTOR_UNAVAILABLE")
        try:
            self.state = RealChromeBrainState.PARSING
            value = self.connector.read_response(target)
            if not isinstance(value, str) or not value.strip():
                raise RealChromeBrainError("CHATGPT_RESPONSE_NOT_FOUND")
            self.state = RealChromeBrainState.READY
            return value
        except RealChromeBrainError:
            raise
        except Exception as exc:
            self._fail(RealChromeBrainState.ERROR, "CHATGPT_RESPONSE_READ_FAILED")
            raise RealChromeBrainError("CHATGPT_RESPONSE_READ_FAILED") from exc

    def close(self) -> None:
        connector = self.connector
        self.target = None
        self.conversation_url = None
        if connector is not None:
            try:
                connector.detach()
            except Exception:
                pass
        self.state = RealChromeBrainState.STOPPED
        self.auth_state = "UNKNOWN"

    def stop(self) -> None:
        self.close()

    def status(self) -> dict[str, Any]:
        target = self.target
        return {
            "state": self.state,
            "authState": self.auth_state,
            "browserRuntime": REAL_CHROME_BROWSER_RUNTIME,
            "chromeProfileStrategy": "DEDICATED_REAL_CHROME_PROFILE",
            "dedicatedProfileName": DEDICATED_REAL_CHROME_PROFILE_NAME,
            "defaultDailyChromeProfileAccess": "FORBIDDEN",
            "mcpScopeCapability": "PROFILE_WIDE",
            "applicationTabPolicy": ChatGPTTargetPolicy.describe(),
            "applicationCookieUsage": "FORBIDDEN_BY_POLICY",
            "applicationTokenUsage": "FORBIDDEN_BY_POLICY",
            "cookieReadByApp": "NONE",
            "tokenReadByApp": "NONE",
            "otherTabReadByApp": "NONE",
            "chromeVersionRequirement": "144+",
            "chromeVersionCheck": check_chrome_version(self.chrome_version),
            "mcpAutoConnect": f"{MCP_AUTOCONNECT_PACKAGE} {MCP_AUTOCONNECT_FLAG}",
            "userOwnedSession": True,
            "explicitUserAuthorization": target is not None,
            "browserSessionReference": target.browser_session_reference if target else None,
            "targetReference": target.target_reference if target else None,
            "conversationUrl": self.conversation_url or (target.conversation_url if target else None),
            "selectedTargetHostname": CHATGPT_HOST if target and is_allowed_chatgpt_url(target.url) else None,
            "chatgptTargetBound": bool(target and is_allowed_chatgpt_url(target.url)),
            "otherTabAccess": "FORBIDDEN",
            "cookieAccess": "NONE",
            "tokenAccess": "NONE",
            "lastError": self.last_error,
        }


__all__ = [
    "CHATGPT_HOST",
    "CHATGPT_URL",
    "REAL_CHROME_BROWSER_RUNTIME",
    "DEDICATED_REAL_CHROME_PROFILE_NAME",
    "MCP_AUTOCONNECT_PACKAGE",
    "MCP_AUTOCONNECT_FLAG",
    "MIN_CHROME_VERSION",
    "RealChromeBrainError",
    "RealChromeBrainState",
    "AuthorizedChatGPTTarget",
    "OfficialChromeConnector",
    "ChatGPTTargetPolicy",
    "RealChromeBrainTransport",
    "RealChromeBrainHost",
    "is_allowed_chatgpt_url",
    "check_chrome_version",
    "is_allowed_conversation_id",
]
