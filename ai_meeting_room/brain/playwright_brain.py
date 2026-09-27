"""Formal GPT Web Brain runtime backed by Playwright persistent context.

This module intentionally delegates browser process, context, and page
lifecycle to Playwright. It never reads cookies, storage state, tokens,
authorization headers, passwords, or verification data.
"""

from __future__ import annotations

import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .chatgpt_web import BrowserController


CHATGPT_URL = "https://chatgpt.com/"
PLAYWRIGHT_GPT_BRAIN_PROFILE = Path.home() / "Library" / "Application Support" / "AI Meeting Room" / "playwright-gpt-brain"
PLAYWRIGHT_BROWSER_RUNTIME = "PLAYWRIGHT_PERSISTENT_BROWSER_RUNTIME"


class PlaywrightBrainError(RuntimeError):
    pass


class PlaywrightBrainState:
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CHALLENGE_REQUIRED = "CHALLENGE_REQUIRED"
    READY = "READY"
    THINKING = "THINKING"
    WAITING_RESPONSE = "WAITING_RESPONSE"
    PARSING = "PARSING"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


class PlaywrightBrainProfile:
    """Own only the new Playwright persistent user-data directory."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else PLAYWRIGHT_GPT_BRAIN_PROFILE

    def create(self) -> Path:
        if self.path.name in {"Default", "Profile 1"}:
            raise PlaywrightBrainError("DEFAULT_CHROME_PROFILE_FORBIDDEN")
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.chmod(0o700)
        except OSError as exc:
            raise PlaywrightBrainError("PLAYWRIGHT_PROFILE_PERMISSION_FAILED") from exc
        return self.path

    def health_check(self) -> bool:
        try:
            return self.path.is_dir() and stat.S_IMODE(self.path.stat().st_mode) & 0o077 == 0
        except OSError:
            return False


@dataclass(frozen=True)
class ComposerTarget:
    """The owner-thread-only target used for both Composer checks and writes."""

    strategy: str
    locator: Any
    element_kind: str
    visible: bool
    enabled: bool
    editable: bool
    locator_count: int
    target_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    candidate_diagnostics: tuple[dict[str, Any], ...] = ()


class ComposerWriteDiagnosticError(RuntimeError):
    """Safe diagnostic wrapper that retains the underlying Playwright error."""

    def __init__(
        self,
        code: str,
        *,
        target: ComposerTarget,
        attempts: list[dict[str, Any]],
        failed_operation: str | None = None,
        fill_error: BaseException | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.target = target
        self.attempts = attempts
        self.failed_operation = failed_operation
        self.fill_error = fill_error


class ChatGPTPlaywrightDomAdapter:
    """DOM-only ChatGPT adapter using Playwright locators."""

    COMPOSER_SELECTORS = (
        '[data-testid="prompt-textarea"]',
        '#prompt-textarea',
        '[role="textbox"]',
        '[role="textbox"][contenteditable="true"]',
        'textarea',
        '[contenteditable="true"]',
        '[data-testid*="composer"] [contenteditable="true"]',
        '[data-testid*="composer"] textarea',
    )
    COMPOSER_CANDIDATE_GROUPS = (
        ("data-testid", ('[data-testid="prompt-textarea"]', '#prompt-textarea')),
        ("role=textbox", ('[role="textbox"]', '[role="textbox"][contenteditable="true"]')),
        ("textarea", ("textarea",)),
        ("contenteditable", ('[contenteditable="true"]',)),
        ("semantic container", ('[data-testid*="composer"] [contenteditable="true"]', '[data-testid*="composer"] textarea')),
    )
    APP_SHELL_SELECTORS = ("main", '[role="main"]', "nav", "#__next")
    LOGIN_SELECTORS = ('input[type="email"]', 'form[action*="login"]', 'a[href*="/auth/login"]')

    @staticmethod
    def _element_kind(metadata: dict[str, Any], strategy: str) -> str:
        tag_name = str(metadata.get("tagName") or "").upper()
        classes = {str(item).lower() for item in (metadata.get("classTokens") or [])}
        if "prosemirror" in classes or "ProseMirror".lower() in classes:
            return "PROSEMIRROR"
        if tag_name == "TEXTAREA" or "textarea" in strategy.lower():
            return "TEXTAREA"
        if tag_name == "INPUT" or "input" in strategy.lower():
            return "INPUT"
        if bool(metadata.get("contenteditable")) or "contenteditable" in strategy.lower():
            return "CONTENTEDITABLE"
        return "OTHER"

    @classmethod
    def _metadata(cls, candidate: Any) -> dict[str, Any]:
        try:
            metadata = candidate.evaluate(
                """node => ({
                  tagName: node.tagName || 'UNKNOWN',
                  role: node.getAttribute('role') || null,
                  contenteditable: node.getAttribute('contenteditable') === 'true' || node.isContentEditable === true,
                  dataTestId: node.getAttribute('data-testid') || null,
                  ariaLabel: node.getAttribute('aria-label') || null,
                  placeholder: node.getAttribute('placeholder') || null,
                  classTokens: Array.from(node.classList || []).slice(0, 20)
                })"""
            )
            result = metadata if isinstance(metadata, dict) else {}
            try:
                result["boundingBoxExists"] = candidate.bounding_box() is not None
            except Exception:
                result["boundingBoxExists"] = False
            return result
        except Exception:
            return {}

    @staticmethod
    def _count(locator: Any) -> int:
        try:
            return int(locator.count())
        except Exception:
            return 0

    @classmethod
    def _candidate_summary(cls, page: Any, group: str, selectors: tuple[str, ...]) -> dict[str, Any]:
        count = visible_count = editable_count = 0
        try:
            locator = page.locator(", ".join(selectors))
            count = min(cls._count(locator), 50)
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    visible = bool(candidate.is_visible())
                    editable = bool(candidate.is_editable()) if visible else False
                except Exception:
                    visible = False
                    editable = False
                visible_count += int(visible)
                editable_count += int(editable)
        except Exception:
            pass
        return {
            "group": group,
            "selectors": list(selectors),
            "count": count,
            "visibleCount": visible_count,
            "editableCount": editable_count,
        }

    @classmethod
    def candidate_diagnostics(cls, page: Any) -> tuple[dict[str, Any], ...]:
        return tuple(cls._candidate_summary(page, group, selectors) for group, selectors in cls.COMPOSER_CANDIDATE_GROUPS)

    @classmethod
    def resolve_composer(cls, page: Any) -> ComposerTarget | None:
        eligible_count: int | None = None
        candidate_diagnostics = cls.candidate_diagnostics(page)
        try:
            all_candidates = page.locator(", ".join(cls.COMPOSER_SELECTORS))
            eligible_count = 0
            for index in range(cls._count(all_candidates)):
                candidate = all_candidates.nth(index)
                enabled = bool(candidate.is_enabled()) if callable(getattr(candidate, "is_enabled", None)) else True
                if candidate.is_visible() and enabled and candidate.is_editable():
                    eligible_count += 1
        except Exception:
            # Compatibility with small test doubles and older locator shims.
            eligible_count = None
        for selector in cls.COMPOSER_SELECTORS:
            locator = page.locator(selector)
            count = cls._count(locator)
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    visible = bool(candidate.is_visible())
                    enabled = bool(candidate.is_enabled()) if callable(getattr(candidate, "is_enabled", None)) else True
                    editable = bool(candidate.is_editable())
                    if visible and enabled and editable:
                        metadata = cls._metadata(candidate)
                        return ComposerTarget(
                            strategy=selector,
                            locator=candidate,
                            element_kind=cls._element_kind(metadata, selector),
                            visible=visible,
                            enabled=enabled,
                            editable=editable,
                            locator_count=eligible_count if eligible_count is not None else count,
                            target_id=f"composer-target-{uuid4()}",
                            metadata=metadata,
                            candidate_diagnostics=candidate_diagnostics,
                        )
                except Exception:
                    continue
        return None

    @classmethod
    def find_composer(cls, page: Any) -> Any | None:
        """Compatibility shim; new code should retain the ComposerTarget."""
        target = cls.resolve_composer(page)
        return target.locator if target is not None else None

    @staticmethod
    def composer_text(target: ComposerTarget) -> str:
        try:
            value = target.locator.input_value()
        except Exception:
            try:
                value = target.locator.inner_text()
            except Exception:
                value = ""
        return str(value or "").replace("\r\n", "\n").replace("\r", "\n")

    @classmethod
    def write_composer(cls, target: ComposerTarget, prompt: str) -> str:
        return cls.write_composer_diagnostic(target, prompt)["operation"]

    @classmethod
    def write_composer_diagnostic(cls, target: ComposerTarget, prompt: str) -> dict[str, Any]:
        if not target.visible:
            raise ComposerWriteDiagnosticError("COMPOSER_NOT_VISIBLE", target=target, attempts=[])
        if not target.enabled or not target.editable:
            raise ComposerWriteDiagnosticError("COMPOSER_NOT_EDITABLE", target=target, attempts=[])
        try:
            current_count = int(target.locator.count())
        except Exception:
            current_count = 0
        if current_count != 1 or target.locator_count != 1:
            raise ComposerWriteDiagnosticError("COMPOSER_AMBIGUOUS", target=target, attempts=[])
        current_visible = bool(target.locator.is_visible())
        current_enabled = bool(target.locator.is_enabled()) if callable(getattr(target.locator, "is_enabled", None)) else target.enabled
        current_editable = bool(target.locator.is_editable())
        if not current_visible:
            raise ComposerWriteDiagnosticError("COMPOSER_NOT_VISIBLE", target=target, attempts=[])
        if not current_enabled:
            raise ComposerWriteDiagnosticError("COMPOSER_NOT_EDITABLE", target=target, attempts=[])
        if not current_editable:
            raise ComposerWriteDiagnosticError("COMPOSER_NOT_EDITABLE", target=target, attempts=[])
        attempts: list[dict[str, Any]] = []
        fill_error: BaseException | None = None
        operation = "locator.fill"
        try:
            target.locator.fill(prompt)
            attempts.append({"operation": "locator.fill", "result": "PASS"})
        except Exception as exc:
            fill_error = exc
            attempts.append({"operation": "locator.fill", "result": "FAIL", "error": exc})
            if target.element_kind not in {"CONTENTEDITABLE", "PROSEMIRROR"}:
                raise ComposerWriteDiagnosticError(
                    "COMPOSER_WRITE_FAILED", target=target, attempts=attempts,
                    failed_operation="locator.fill", fill_error=exc,
                ) from exc
            operation = "focus+select_all+press_sequentially"
            try:
                target.locator.focus()
                target.locator.press("ControlOrMeta+A")
                target.locator.press_sequentially(prompt)
                attempts.append({"operation": operation, "result": "PASS"})
            except Exception as fallback_exc:
                attempts.append({"operation": operation, "result": "FAIL", "error": fallback_exc})
                raise ComposerWriteDiagnosticError(
                    "COMPOSER_WRITE_FAILED", target=target, attempts=attempts,
                    failed_operation=operation, fill_error=exc,
                ) from fallback_exc
        if cls.composer_text(target) != prompt.replace("\r\n", "\n").replace("\r", "\n"):
            raise ComposerWriteDiagnosticError(
                "COMPOSER_WRITE_VERIFY_FAILED", target=target, attempts=attempts,
                failed_operation=operation,
            )
        return {"operation": operation, "attempts": attempts, "fillError": fill_error}

    @classmethod
    def _has_any(cls, page: Any, selectors: tuple[str, ...]) -> bool:
        return any(cls._count(page.locator(selector)) > 0 for selector in selectors)

    @classmethod
    def _has_login_text(cls, page: Any) -> bool:
        try:
            pattern = re.compile(r"sign in|log in|create account|continue with google|continue with apple|登录|注册|使用 google|使用 apple", re.I)
            for role in ("button", "link"):
                if page.get_by_role(role, name=pattern).count() > 0:
                    return True
            return False
        except Exception:
            return False

    @classmethod
    def status(cls, page: Any) -> str:
        try:
            if "challenges.cloudflare.com" in (page.url or ""):
                return "CHALLENGE_REQUIRED"
            if page.get_by_text(re.compile(r"verify you are human|checking your browser", re.I)).count() > 0:
                return "CHALLENGE_REQUIRED"
        except Exception:
            return "UNKNOWN"
        if cls._has_any(page, cls.LOGIN_SELECTORS) or cls._has_login_text(page):
            return "AUTH_REQUIRED"
        composer = cls.find_composer(page)
        app_shell = cls._has_any(page, cls.APP_SHELL_SELECTORS)
        if composer is not None and app_shell:
            return "AUTHENTICATED"
        try:
            return "LOADING" if page.evaluate("document.readyState") != "complete" or app_shell else "DOM_UNKNOWN"
        except Exception:
            return "UNKNOWN"


class ChatGPTPageResolver:
    """Resolve only HTTPS chatgpt.com pages in the persistent context."""

    @staticmethod
    def eligible(page: Any) -> bool:
        try:
            url = str(page.url or "")
            return url.startswith("https://chatgpt.com/") and "/auth/" not in url and "/login" not in url
        except Exception:
            return False

    def resolve(self, pages: list[Any]) -> Any | None:
        candidates: list[tuple[int, int, Any]] = []
        for index, page in enumerate(pages):
            if not self.eligible(page):
                continue
            status = ChatGPTPlaywrightDomAdapter.status(page)
            try:
                active = int(page.evaluate("document.visibilityState === 'visible'"))
            except Exception:
                active = 0
            candidates.append((int(status == "AUTHENTICATED"), active + index, page))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return candidates[0][2]


class PlaywrightBrainTransport(BrowserController):
    """BrowserController implemented with a Playwright persistent context."""

    def __init__(self, host: "PlaywrightBrainHost") -> None:
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


class PlaywrightBrainHost(BrowserController):
    """Lifecycle facade for the formal GPT Web Brain runtime."""

    def __init__(self, *, profile: PlaywrightBrainProfile | None = None, on_failure: Callable[[str, str], None] | None = None) -> None:
        self.profile = profile or PlaywrightBrainProfile()
        self.on_failure = on_failure
        self.state = PlaywrightBrainState.STOPPED
        self.auth_state = "UNKNOWN"
        self.last_error: str | None = None
        self.page: Any | None = None
        self.context: Any | None = None
        self.playwright: Any | None = None
        self.conversation_id: str | None = None
        self._baseline_assistant_count = 0
        self._last_failure: tuple[str, str] | None = None
        self.page_resolver = ChatGPTPageResolver()

    def _fail(self, state: str, reason: str) -> None:
        self.state = state
        self.last_error = reason
        signature = (state, reason)
        if signature != self._last_failure and self.on_failure is not None:
            self._last_failure = signature
            self.on_failure(state, reason)

    def _load_playwright(self) -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PlaywrightBrainError("PLAYWRIGHT_NOT_INSTALLED") from exc
        return sync_playwright().start()

    def connect(self) -> None:
        if self.context is not None:
            try:
                if self.context.pages:
                    return
            except Exception:
                self.close()
        self.profile.create()
        self.playwright = self._load_playwright()
        try:
            self.context = self.playwright.chromium.launch_persistent_context(
                str(self.profile.path),
                headless=False,
                channel="chrome",
            )
            self.ensure_chatgpt_page()
        except Exception:
            self.close()
            raise

    def ensure_chatgpt_page(self) -> Any:
        if self.context is None:
            raise PlaywrightBrainError("PLAYWRIGHT_CONTEXT_UNAVAILABLE")
        page = self.page_resolver.resolve(list(self.context.pages))
        if page is None:
            page = self.context.new_page()
            page.goto(CHATGPT_URL, wait_until="domcontentloaded")
        self.page = page
        return page

    def getPage(self) -> Any:
        return self.ensure_chatgpt_page()

    # Keep the public host surface readable to non-JavaScript callers while
    # retaining the camelCase names used by the existing bridge boundary.
    def get_page(self) -> Any:
        return self.getPage()

    def openBrain(self) -> dict[str, Any]:
        self.start()
        try:
            self.getPage().bring_to_front()
        except Exception:
            pass
        return self.status()

    def open(self) -> dict[str, Any]:
        return self.openBrain()

    def checkHealth(self) -> bool:
        try:
            return self.auth_status() == "AUTHENTICATED"
        except Exception:
            return False

    def recover(self) -> bool:
        self.close()
        self.start()
        return self.checkHealth()

    def start(self) -> None:
        self.state = PlaywrightBrainState.STARTING
        try:
            self.connect()
            self._apply_auth(self.auth_status())
        except Exception as exc:
            self._fail(PlaywrightBrainState.ERROR, str(exc) or type(exc).__name__)

    def _apply_auth(self, auth_state: str) -> str:
        self.auth_state = auth_state
        if auth_state == "AUTHENTICATED":
            self.state = PlaywrightBrainState.READY
            self._last_failure = None
        elif auth_state == "AUTH_REQUIRED":
            self._fail(PlaywrightBrainState.AUTH_REQUIRED, auth_state)
            try:
                self.getPage().bring_to_front()
            except Exception:
                pass
        elif auth_state == "CHALLENGE_REQUIRED":
            self._fail(PlaywrightBrainState.CHALLENGE_REQUIRED, auth_state)
            try:
                self.getPage().bring_to_front()
            except Exception:
                pass
        elif auth_state in {"LOADING", "DOM_UNKNOWN"}:
            self._fail(PlaywrightBrainState.UNKNOWN, auth_state)
        else:
            self._fail(PlaywrightBrainState.UNKNOWN, auth_state)
        return auth_state

    def auth_status(self) -> str:
        try:
            page = self.ensure_chatgpt_page()
            return ChatGPTPlaywrightDomAdapter.status(page)
        except PlaywrightBrainError:
            raise
        except Exception as exc:
            self._fail(PlaywrightBrainState.ERROR, "PLAYWRIGHT_BROWSER_LOST")
            raise PlaywrightBrainError("PLAYWRIGHT_BROWSER_LOST") from exc

    def open_conversation(self, conversation_id: str | None = None) -> str:
        page = self.ensure_chatgpt_page()
        if conversation_id:
            if not re.fullmatch(r"[A-Za-z0-9-]+", conversation_id):
                raise PlaywrightBrainError("INVALID_CONVERSATION_BINDING")
            self.conversation_id = conversation_id
            page.goto(f"https://chatgpt.com/c/{conversation_id}", wait_until="domcontentloaded")
        else:
            page.goto(CHATGPT_URL, wait_until="domcontentloaded")
            self.conversation_id = None
        self.page = self.page_resolver.resolve(list(self.context.pages)) if self.context else page
        return page.url

    def send_prompt(self, prompt: str) -> None:
        page = self.ensure_chatgpt_page()
        if self.auth_status() != "AUTHENTICATED":
            raise PlaywrightBrainError("AUTH_REQUIRED")
        composer = ChatGPTPlaywrightDomAdapter.find_composer(page)
        if composer is None:
            raise PlaywrightBrainError("COMPOSER_NOT_FOUND")
        self._baseline_assistant_count = page.locator('[data-message-author-role="assistant"]').count()
        composer.fill(prompt)
        composer.press("Enter")

    def wait_for_completion(self, timeout_seconds: float) -> None:
        page = self.ensure_chatgpt_page()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = self.auth_status()
            if status == "CHALLENGE_REQUIRED":
                raise PlaywrightBrainError("CHALLENGE_REQUIRED")
            if status != "AUTHENTICATED":
                raise PlaywrightBrainError("PLAYWRIGHT_BRAIN_AUTH_CHANGED")
            stop = page.get_by_role("button", name=re.compile(r"stop generating|停止生成", re.I))
            if stop.count() == 0 and page.locator('[data-message-author-role="assistant"]').count() > self._baseline_assistant_count:
                return
            page.wait_for_timeout(250)
        raise TimeoutError("ChatGPT Web response timeout")

    def read_response(self) -> str:
        page = self.ensure_chatgpt_page()
        articles = page.locator('[data-message-author-role="assistant"]')
        if articles.count() <= self._baseline_assistant_count:
            raise PlaywrightBrainError("RESPONSE_NOT_FOUND")
        return articles.last.inner_text()

    def close(self) -> None:
        context, playwright = self.context, self.playwright
        self.context = None
        self.playwright = None
        self.page = None
        try:
            if context is not None:
                context.close()
        finally:
            if playwright is not None:
                playwright.stop()
            self.state = PlaywrightBrainState.STOPPED
            self.auth_state = "UNKNOWN"

    def stop(self) -> None:
        """Compatibility lifecycle name used by Product Shell shutdown."""
        self.close()

    def status(self) -> dict[str, Any]:
        pages: list[Any] = []
        if self.context is not None:
            try:
                pages = list(self.context.pages)
            except Exception:
                pages = []
        selected = self.page_resolver.resolve(pages) if pages else self.page
        selected_url = str(getattr(selected, "url", "") or "") if selected is not None else ""
        parsed_path = selected_url.split("?", 1)[0].split("#", 1)[0] if selected_url else None
        return {
            "state": self.state,
            "authState": self.auth_state,
            "domRecognized": self.auth_state == "AUTHENTICATED",
            "conversationBindingId": self.conversation_id,
            "browserRuntime": PLAYWRIGHT_BROWSER_RUNTIME,
            "profilePersistent": True,
            "persistentUserDataDir": str(self.profile.path),
            "defaultChromeProfileAccess": "FORBIDDEN",
            "browserAlive": self.context is not None and bool(pages),
            "lastError": self.last_error,
            "pageTargetCount": len(pages),
            "chatgptTargetCount": sum(1 for page in pages if self.page_resolver.eligible(page)),
            "selectedTargetHostname": "chatgpt.com" if selected_url.startswith(CHATGPT_URL) else None,
            "selectedTargetPathname": parsed_path,
            "composerDetected": bool(selected is not None and ChatGPTPlaywrightDomAdapter.find_composer(selected)),
            "loginFormDetected": bool(selected is not None and ChatGPTPlaywrightDomAdapter._has_any(selected, ChatGPTPlaywrightDomAdapter.LOGIN_SELECTORS)),
            "challengeDetected": self.auth_state == "CHALLENGE_REQUIRED",
        }


__all__ = [
    "CHATGPT_URL",
    "PLAYWRIGHT_GPT_BRAIN_PROFILE",
    "PLAYWRIGHT_BROWSER_RUNTIME",
    "PlaywrightBrainError",
    "PlaywrightBrainState",
    "PlaywrightBrainProfile",
    "ChatGPTPlaywrightDomAdapter",
    "ChatGPTPageResolver",
    "PlaywrightBrainTransport",
    "PlaywrightBrainHost",
]
