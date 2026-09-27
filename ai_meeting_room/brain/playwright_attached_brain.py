"""Formal GPT Web Brain runtime attached to an already-running Chrome.

This boundary never launches Chromium or Chrome.  It connects to the user's
already-running, dedicated Chrome through Playwright CDP, selects one HTTPS
ChatGPT page using URL metadata only, and reads the DOM only on that selected
page for the read-only Composer health check.
"""

from __future__ import annotations

import asyncio
import re
import hashlib
import hmac
import inspect
import json
import os
import secrets
import traceback
import time
import threading
from contextlib import contextmanager
from typing import Any, Callable
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from ..data_paths import default_product_data_root
from .chatgpt_web import BrowserController
from .playwright_brain import ChatGPTPlaywrightDomAdapter, ComposerWriteDiagnosticError
from .chatgpt_page_selection import (
    ChatGPTAuthenticatedFingerprint,
    ChatGPTPageFingerprint,
    ChatGPTPageResolver as ScoredChatGPTPageResolver,
    ChatGPTPageScorer,
    SELECTION_POLICY,
)
from .dedicated_cdp import (
    DEDICATED_CHROME_USER_DATA_DIR,
    DedicatedChromeEndpointResolver,
    DevToolsActivePortRecord,
    tcp_connect_probe,
)


PLAYWRIGHT_CHANNEL = "chrome"
PLAYWRIGHT_LEGACY_CDP_URL = "http://127.0.0.1:9222"
# Kept as a compatibility name for callers/tests; the formal value is now a
# Playwright Chromium channel, never the legacy HTTP URL.
PLAYWRIGHT_CDP_ENDPOINT = PLAYWRIGHT_CHANNEL
PLAYWRIGHT_FORMAL_CONNECTION = "PLAYWRIGHT_CONNECT_OVER_CDP_EXACT_WS"
FORMAL_ATTACH_MODE = "EXACT_DEDICATED_CDP"
FORMAL_ENDPOINT_SOURCE = "DEVTOOLS_ACTIVE_PORT"
# R26's successful Product Shell diagnostic used this exact Playwright
# timeout.  Keep the formal path argument-parity explicit and separate from
# the legacy/configurable timeout used by compatibility paths.
FORMAL_EXACT_CONNECT_TIMEOUT_MS = 10000
PLAYWRIGHT_ATTACHED_BROWSER_RUNTIME = "PLAYWRIGHT_ATTACH_EXISTING_REAL_CHROME"
CHATGPT_URL = "https://chatgpt.com/"
CONNECT_LOG_DIR = default_product_data_root() / "logs"
CONNECT_DEBUG_LOG = CONNECT_LOG_DIR / "brain-connect-debug.log"
CONNECT_STACK_LOG = CONNECT_LOG_DIR / "brain-connect-stack.log"
COMPOSER_PROBE_LOG = CONNECT_LOG_DIR / "composer-probe.log"
RUNTIME_LIFECYCLE_LOG = CONNECT_LOG_DIR / "runtime-lifecycle.log"

# This is intentionally a bounded boolean/metadata probe.  It never returns
# body text, message text, input values, or browser storage.
_PAGE_FINGERPRINT_SCRIPT = r'''() => {
  const visible = (node) => {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const box = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || '1') !== 0 && box.width > 0 && box.height > 0;
  };
  const enabled = (node) => !node.disabled && node.getAttribute('aria-disabled') !== 'true';
  const unique = (selectors) => {
    const nodes = new Set();
    selectors.forEach((selector) => document.querySelectorAll(selector).forEach((node) => nodes.add(node)));
    return [...nodes];
  };
  // A ChatGPT Composer is commonly inside a form and its send control may be
  // type=submit.  That is not login evidence; only explicit login controls
  // belong in this fingerprint.
  const loginNodes = unique([
    'input[type="email"]', 'form[action*="login"]', 'a[href*="/auth/login"]'
  ]);
  const loginUiDetected = loginNodes.some(visible) ||
    [...document.querySelectorAll('button, a')].some((node) => {
      if (!visible(node)) return false;
      const label = `${node.getAttribute('aria-label') || ''} ${node.getAttribute('title') || ''} ${node.textContent || ''}`.toLowerCase();
      return /sign in|log in|create account|continue with google|continue with apple|登录|注册|使用 google|使用 apple/.test(label);
    });
  const composerNodes = unique([
    '[data-testid="prompt-textarea"]', '#prompt-textarea', '[role="textbox"]',
    'textarea', '[contenteditable="true"]',
    '[data-testid*="composer"] [contenteditable="true"]',
    '[data-testid*="composer"] textarea'
  ]);
  const visibleComposer = composerNodes.filter(visible);
  const editableComposer = visibleComposer.filter((node) => enabled(node) &&
    (node.isContentEditable || node.tagName === 'TEXTAREA' || node.tagName === 'INPUT' || node.getAttribute('role') === 'textbox'));
  const appShell = unique(['main', '[role="main"]', 'nav', '#__next', '[data-testid*="sidebar"]']).some(visible);
  return {
    visibilityState: document.visibilityState || 'UNKNOWN',
    documentReadyState: document.readyState || 'UNKNOWN',
    loginUiDetected,
    authenticatedShellDetected: appShell && !loginUiDetected,
    composerCandidateCount: composerNodes.length,
    visibleComposerCount: visibleComposer.length,
    editableComposerCount: editableComposer.length
  };
}'''

# Fixed-scope, read-only ChatGPT structural inventory. This script is kept
# separate from the legacy auth fingerprint because it must never inspect
# page text, form values, HTML, browser storage, or credentials.
_STRUCTURAL_DIAGNOSTIC_SCRIPT = r'''() => {
  const visible = (node) => {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const box = node.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity || '1') !== 0 && box.width > 0 && box.height > 0;
  };
  const editable = (node) => !node.disabled && node.getAttribute('aria-disabled') !== 'true' &&
    (node.isContentEditable || node.tagName === 'TEXTAREA' || node.tagName === 'INPUT' ||
     node.getAttribute('role') === 'textbox');
  const count = (selector) => [...document.querySelectorAll(selector)];
  const summarize = (probeId, selector) => {
    const nodes = count(selector);
    const visibleNodes = nodes.filter(visible);
    return {
      probe_id: probeId,
      count: nodes.length,
      visible_count: visibleNodes.length,
      editable_count: visibleNodes.filter(editable).length
    };
  };
  const groups = {
    APP_SHELL_PROBES: [
      ['main', 'main'], ['role-main', '[role="main"]'], ['nav', 'nav'],
      ['next-root', '#__next'], ['sidebar-testid', '[data-testid*="sidebar"]']
    ],
    COMPOSER_PROBES: [
      ['prompt-testid', '[data-testid="prompt-textarea"]'],
      ['prompt-id', '#prompt-textarea'], ['role-textbox', '[role="textbox"]'],
      ['role-textbox-contenteditable', '[role="textbox"][contenteditable="true"]'],
      ['textarea', 'textarea'], ['contenteditable', '[contenteditable="true"]'],
      ['composer-contenteditable', '[data-testid*="composer"] [contenteditable="true"]'],
      ['composer-textarea', '[data-testid*="composer"] textarea']
    ],
    AUTHENTICATED_PROBES: [
      ['main', 'main'], ['role-main', '[role="main"]'], ['nav', 'nav'],
      ['next-root', '#__next'], ['sidebar-testid', '[data-testid*="sidebar"]']
    ],
    LOGIN_PROBES: [
      ['email-input', 'input[type="email"]'], ['login-form', 'form[action*="login"]'],
      ['login-link', 'a[href*="/auth/login"]'], ['login-testid', '[data-testid*="login"]']
    ],
    CHALLENGE_PROBES: [
      ['challenge-testid', '[data-testid*="challenge"]'],
      ['challenge-frame', 'iframe[src*="challenge"]'],
      ['captcha-frame', 'iframe[src*="captcha"]'],
      ['captcha-input', 'input[name*="captcha"]']
    ],
    ERROR_PROBES: [
      ['role-alert', '[role="alert"]'], ['error-testid', '[data-testid*="error"]'],
      ['error-status', '[data-status="error"]']
    ]
  };
  const probes = {};
  for (const [group, entries] of Object.entries(groups)) {
    probes[group] = entries.map(([probeId, selector]) => summarize(probeId, selector));
  }
  const editableControls = count('textarea, input:not([type="hidden"]), [contenteditable="true"], [role="textbox"]');
  return {
    document_ready_state: document.readyState,
    body_present: Boolean(document.body),
    main_element_count: count('main').length,
    form_count: count('form').length,
    textarea_count: count('textarea').length,
    contenteditable_true_count: count('[contenteditable="true"]').length,
    role_textbox_count: count('[role="textbox"]').length,
    iframe_count: count('iframe').length,
    dialog_count: count('dialog, [role="dialog"]').length,
    button_count: count('button').length,
    visible_textarea_count: count('textarea').filter(visible).length,
    visible_contenteditable_count: count('[contenteditable="true"]').filter(visible).length,
    visible_role_textbox_count: count('[role="textbox"]').filter(visible).length,
    visible_editable_form_control_count: editableControls.filter((node) => visible(node) && editable(node)).length,
    form_with_editable_descendant_count: count('form').filter((form) =>
      form.querySelector('textarea, input:not([type="hidden"]), [contenteditable="true"], [role="textbox"]') !== null
    ).length,
    probe_results: probes
  };
}'''

_STRUCTURAL_PROBE_IDS: dict[str, tuple[str, ...]] = {
    "APP_SHELL_PROBES": ("main", "role-main", "nav", "next-root", "sidebar-testid"),
    "COMPOSER_PROBES": (
        "prompt-testid", "prompt-id", "role-textbox", "role-textbox-contenteditable",
        "textarea", "contenteditable", "composer-contenteditable", "composer-textarea",
    ),
    "AUTHENTICATED_PROBES": ("main", "role-main", "nav", "next-root", "sidebar-testid"),
    "LOGIN_PROBES": ("email-input", "login-form", "login-link", "login-testid"),
    "CHALLENGE_PROBES": ("challenge-testid", "challenge-frame", "captcha-frame", "captcha-input"),
    "ERROR_PROBES": ("role-alert", "error-testid", "error-status"),
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: Any, limit: int = 500) -> str:
    """Keep diagnostics useful without persisting credentials or page bodies."""
    text = str(value or "")
    text = re.sub(r"(?i)(authorization|cookie|token|secret|password|api[_-]?key|refresh[_-]?token)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    text = re.sub(r"(?i)([?&](?:code|token|access_token|id_token|auth|session)[^=]*=)[^&#\s]+", r"\1[REDACTED]", text)
    # The browser UUID in a DevTools WebSocket URL is runtime identity, not
    # useful acceptance evidence.  Keep the error shape while redacting it.
    text = re.sub(r"(/devtools/browser/)[A-Za-z0-9-]+", r"\1[REDACTED]", text)
    return text[:limit]


def _stack_id(stack: str) -> str:
    return hashlib.sha256(stack.encode("utf-8", errors="replace")).hexdigest()[:16]


def _append_jsonl(path: Path, value: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")
    except OSError:
        # Diagnostics must never turn a real connection failure into a new one.
        pass


def chromium_channel_support() -> dict[str, Any]:
    """Probe channel support without starting a browser or connecting to Chrome."""
    result: dict[str, Any] = {
        "channel": PLAYWRIGHT_CHANNEL,
        "isChromiumChannelName": False,
        "resolveChannelEndpoint": False,
        "apiAvailable": False,
        "supported": False,
    }
    try:
        import playwright
        from playwright.sync_api import sync_playwright
        bundle = Path(playwright.__file__).parent / "driver" / "package" / "lib" / "coreBundle.js"
        source = bundle.read_text(encoding="utf-8", errors="ignore")
        result["isChromiumChannelName"] = 'function isChromiumChannelName' in source and '["chrome"' in source
        result["resolveChannelEndpoint"] = "async function resolveChannelEndpoint" in source
        with sync_playwright() as runtime:
            result["apiAvailable"] = hasattr(runtime.chromium, "connect_over_cdp")
    except Exception:
        return result
    result["supported"] = all((result["isChromiumChannelName"], result["resolveChannelEndpoint"], result["apiAvailable"]))
    return result


class PlaywrightAttachedBrainError(RuntimeError):
    def __init__(self, message: str, *, code: str | None = None, stage: str | None = None, cause: BaseException | None = None) -> None:
        super().__init__(message)
        self.code = code or message
        self.stage = stage
        self.cause = cause
        self.original_name = type(cause).__name__ if cause is not None else type(self).__name__
        raw_stack = "".join(traceback.format_exception(cause)) if cause is not None else "".join(traceback.format_stack(limit=12))
        self.stack = _safe_text(raw_stack, 12000)
        self.stack_id = _stack_id(self.stack)

    def envelope(self, attempt_id: str) -> dict[str, Any]:
        cause = self.cause
        return {
            "code": self.code,
            "stage": self.stage or "UNKNOWN",
            "name": self.original_name,
            "message": _safe_text(self.cause or self),
            "stackId": self.stack_id,
            "attemptId": attempt_id,
            "cause": {"name": type(cause).__name__, "message": _safe_text(cause)} if cause is not None else None,
        }


class PlaywrightAttachedBrainState:
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCOVERING_CHATGPT = "DISCOVERING_CHATGPT"
    CHECKING_COMPOSER = "CHECKING_COMPOSER"
    READY = "READY"
    PAGE_LOST = "PAGE_LOST"
    BROWSER_LOST = "BROWSER_LOST"
    ERROR = "ERROR"


def is_allowed_chatgpt_url(raw_url: str) -> bool:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(str(raw_url or ""))
        if parsed.scheme != "https" or parsed.hostname != "chatgpt.com" or parsed.username or parsed.password:
            return False
        path = parsed.path or "/"
        return path == "/" or path.startswith("/c/") or path.startswith("/g/")
    except Exception:
        return False


class PlaywrightChatGPTPageResolver(ScoredChatGPTPageResolver):
    """Resolve HTTPS ChatGPT pages using safe auth/composer fingerprints."""

    @staticmethod
    def eligible(page: Any) -> bool:
        try:
            return is_allowed_chatgpt_url(str(page.url or ""))
        except Exception:
            return False

    def resolve(self, pages: list[Any]) -> Any | None:
        return super().resolve(pages)


class PlaywrightAttachedBrainTransport(BrowserController):
    def __init__(self, host: "PlaywrightAttachedBrainHost") -> None:
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


class PlaywrightAttachedBrainHost(BrowserController):
    """Fail-closed lifecycle for a user-owned existing Chrome instance."""

    # Compatibility marker for the public business error envelope:
    # code="COMPOSER_NOT_FOUND" remains distinct from IPC failures.

    def __init__(
        self,
        *,
        cdp_endpoint: str = PLAYWRIGHT_CHANNEL,
        channel: str = PLAYWRIGHT_CHANNEL,
        playwright_factory: Callable[[], Any] | None = None,
        on_failure: Callable[[str, str], None] | None = None,
        cdp_preflight: Callable[[str], dict[str, Any]] | None = None,
        connect_timeout_ms: int | None = None,
        dedicated_user_data_dir: Path | str | None = None,
        endpoint_resolver: Callable[[], DevToolsActivePortRecord] | None = None,
        attach_mode: str | None = None,
        endpoint_source: str | None = None,
    ) -> None:
        self.channel = channel
        self.cdp_endpoint = cdp_endpoint
        # The formal route resolves the user-owned dedicated Chrome endpoint
        # afresh from DevToolsActivePort for every attach.  The legacy URL mode
        # remains available only for explicit compatibility/test injection.
        self.connection_mode = "EXACT_WS" if cdp_endpoint == PLAYWRIGHT_CHANNEL else "LEGACY_URL"
        self.dedicated_user_data_dir = Path(dedicated_user_data_dir or DEDICATED_CHROME_USER_DATA_DIR)
        self.endpoint_resolver = endpoint_resolver
        self._injected_endpoint_resolver = endpoint_resolver is not None
        self.attach_mode = attach_mode or (FORMAL_ATTACH_MODE if self.connection_mode == "EXACT_WS" else "LEGACY_EXPLICIT_URL")
        self.endpoint_source = endpoint_source or (FORMAL_ENDPOINT_SOURCE if self.connection_mode == "EXACT_WS" else "EXPLICIT_URL")
        self._injected_playwright = playwright_factory is not None
        self.playwright_factory = playwright_factory
        self.on_failure = on_failure
        self.state = PlaywrightAttachedBrainState.DISCONNECTED
        self.auth_state = "UNKNOWN"
        self.last_error: str | None = None
        self.playwright: Any | None = None
        self.browser: Any | None = None
        self.context: Any | None = None
        self.page: Any | None = None
        self.page_resolver = PlaywrightChatGPTPageResolver()
        self.page_candidates: list[dict[str, Any]] = []
        self.selected_candidate_index: int | None = None
        self._baseline_assistant_count = 0
        self._last_failure: tuple[str, str] | None = None
        self._browser_disconnected = False
        self.connect_attempt_id: str | None = None
        self.brain_connection_id: str | None = None
        self.host_instance_id = f"host-{uuid4()}"
        self.registry_instance_id: str = "UNKNOWN"
        self.process_pid = os.getpid()
        self.parent_pid = os.getppid()
        self.runtime_owner = "PRODUCT_SHELL"
        self.owner_thread_id = threading.get_ident()
        self._thread_operation_matrix: dict[str, dict[str, Any]] = {}
        self.thread_affinity_error: dict[str, Any] | None = None
        self.bound_page_id: str | None = None
        self._bound_page_object: Any | None = None
        self._diagnostic_fingerprint_salt = secrets.token_bytes(32)
        self._diagnostic_bound_page_object: Any | None = None
        self._diagnostic_bound_main_frame_object: Any | None = None
        self._diagnostic_bound_context_object: Any | None = None
        self._connect_lock = threading.Lock()
        self._ensure_chatgpt_page_lock = threading.Lock()
        self._dev_probe_lock = threading.Lock()
        self._atomic_probe_lock_held = False
        self._closing = False
        self._runtime_lifecycle_events: list[dict[str, Any]] = []
        self._runtime_lifecycle_counts: dict[str, int] = {
            "registryResetCount": 0,
            "hostReplacementCount": 0,
            "boundPageRebindCount": 0,
        }
        self._connect_in_progress = False
        self.attempt_started_at: str | None = None
        self.attempt_finished_at: str | None = None
        self.attempt_duration_ms: int | None = None
        self._stage_started_at: dict[str, str] = {}
        self._stage_started_clock: dict[str, float] = {}
        self.last_error_details: dict[str, Any] | None = None
        self.trace: list[dict[str, Any]] = []
        self._poc_request: dict[str, Any] | None = None
        self._poc_bound_page: Any | None = None
        self._poc_bound_page_id: str | None = None
        self._poc_composer_target: Any | None = None
        self._poc_metrics: dict[str, Any] = {
            "composerWrite": False,
            "composerStrategy": None,
            "composerElementKind": None,
            "composerWriteOperation": None,
            "chatgptSend": False,
            "chatgptSendConfirmed": False,
            "responseCompletion": False,
            "responseBoundary": False,
            "chatgptResponseCapture": False,
        }
        self.cdp_preflight = cdp_preflight
        configured_timeout = connect_timeout_ms
        if configured_timeout is None:
            try:
                configured_timeout = int(os.environ.get("AI_MEETING_ROOM_CHROME_CONNECT_TIMEOUT_MS", "30000"))
            except ValueError:
                configured_timeout = 30000
        self.connect_timeout_ms = max(1000, int(configured_timeout))
        if self.connection_mode == "EXACT_WS" and self.endpoint_resolver is None:
            self.endpoint_resolver = DedicatedChromeEndpointResolver(self.dedicated_user_data_dir).resolve
        self.diagnostics: dict[str, Any] = {
            "cdpHttpPreflight": "NOT_RUN",
            "cdpHttpStatus": None,
            "jsonVersionShape": "UNKNOWN",
            "playwrightLoad": "NOT_RUN",
            "connectOverCdp": "NOT_RUN",
            "browserConnected": False,
            "lastSuccessfulStage": None,
            "lastSuccessStage": None,
            "lastFailureStage": None,
            "versions": self._version_diagnostics(),
            "channelSupport": chromium_channel_support(),
            "connectTimeoutMs": self.connect_timeout_ms,
            "formalEffectiveConnectOptions": None,
            "formalCdpPort": None,
            "formalEndpointFreshness": "NOT_READ",
            "formalTcpProbe": None,
            "formalConnectFailure": None,
        }

    @staticmethod
    def _version_diagnostics() -> dict[str, str]:
        result = {
            "python": os.sys.version.split()[0],
            "playwright": "UNKNOWN",
            "playwrightCore": "UNKNOWN",
            "node": os.environ.get("NODE_VERSION", "UNKNOWN"),
            "electron": os.environ.get("ELECTRON_VERSION", "UNKNOWN"),
            "chrome": os.environ.get("CHROME_VERSION", "UNKNOWN"),
        }
        try:
            from importlib.metadata import version
            result["playwright"] = version("playwright")
            try:
                result["playwrightCore"] = version("playwright-core")
            except Exception:
                try:
                    import playwright
                    bundled = Path(playwright.__file__).parent / "driver" / "package" / "package.json"
                    result["playwrightCore"] = str(json.loads(bundled.read_text(encoding="utf-8"))["version"])
                except Exception:
                    result["playwrightCore"] = "NOT_INSTALLED"
        except Exception:
            result["playwright"] = "NOT_INSTALLED"
        return result

    def _trace(self, stage: str, result: str, *, code: str | None = None, error: BaseException | None = None) -> None:
        timestamp = _utc_now()
        if result in {"START", "RECEIVED"}:
            self._stage_started_at[stage] = timestamp
            self._stage_started_clock[stage] = time.monotonic()
        elif stage not in self._stage_started_at and result not in {"CHANNEL_MODE_NO_HTTP_PREFLIGHT", "PREFLIGHT_PASS"}:
            self._stage_started_at[stage] = timestamp
            self._stage_started_clock[stage] = time.monotonic()
        item: dict[str, Any] = {
            "attemptId": self.connect_attempt_id,
            "timestamp": timestamp,
            "stage": stage,
            "result": result,
            "stageStartedAt": self._stage_started_at.get(stage, timestamp),
        }
        if result not in {"START", "RECEIVED", "CHANNEL_MODE_NO_HTTP_PREFLIGHT", "PREFLIGHT_PASS"}:
            item["stageFinishedAt"] = timestamp
            started = self._stage_started_clock.get(stage)
            if started is not None:
                item["durationMs"] = max(0, round((time.monotonic() - started) * 1000))
        if code:
            item["errorCode"] = code
        if error:
            item["errorName"] = type(error).__name__
            item["safeMessage"] = _safe_text(error)
        self.trace.append(item)
        _append_jsonl(CONNECT_DEBUG_LOG, item)

    def _runtime_lifecycle_event(
        self,
        event: str,
        reason_code: str,
        *,
        caller: str | None = None,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        """Record a safe, attributable lifecycle event.

        This intentionally records identity and provenance only.  It never
        records browser storage, credentials, URLs with query strings, or
        page/chat content.
        """
        stack = "".join(traceback.format_stack(limit=16))
        item: dict[str, Any] = {
            "timestamp": _utc_now(),
            "event": str(event),
            "reason": str(reason_code),
            "processPid": int(getattr(self, "process_pid", os.getpid())),
            "threadId": threading.get_ident(),
            "registryInstanceId": str(getattr(self, "registry_instance_id", "UNKNOWN")),
            "hostInstanceId": str(getattr(self, "host_instance_id", "UNKNOWN")),
            "boundPageId": getattr(self, "bound_page_id", None),
            "caller": caller or "PlaywrightAttachedBrainHost",
            "stackId": _stack_id(_safe_text(stack, 12000)),
        }
        if error is not None:
            item["errorName"] = type(error).__name__
        events = getattr(self, "_runtime_lifecycle_events", None)
        if not isinstance(events, list):
            events = []
            self._runtime_lifecycle_events = events
        events.append(item)
        del events[:-100]
        _append_jsonl(RUNTIME_LIFECYCLE_LOG, item)
        return item

    def lifecycle_snapshot(self) -> dict[str, Any]:
        """Return safe lifecycle evidence for diagnostics and acceptance tests."""
        counts = getattr(self, "_runtime_lifecycle_counts", {})
        return {
            "counts": {
                "registryResetCount": int(counts.get("registryResetCount", 0)),
                "hostReplacementCount": int(counts.get("hostReplacementCount", 0)),
                "boundPageRebindCount": int(counts.get("boundPageRebindCount", 0)),
            },
            "events": [dict(event) for event in getattr(self, "_runtime_lifecycle_events", [])],
            "lastEvent": dict(getattr(self, "_runtime_lifecycle_events", [])[-1]) if getattr(self, "_runtime_lifecycle_events", []) else None,
        }

    def _runtime_lifecycle_event_once(
        self,
        event: str,
        reason_code: str,
        *,
        caller: str,
        error: BaseException | None = None,
    ) -> dict[str, Any] | None:
        last = getattr(self, "_runtime_lifecycle_events", [])
        if last and last[-1].get("event") == event and last[-1].get("reason") == reason_code:
            return None
        return self._runtime_lifecycle_event(event, reason_code, caller=caller, error=error)

    def runtime_identity_snapshot(self) -> dict[str, Any]:
        """Capture the formal Host identity without inspecting page content."""
        return {
            "registryInstanceId": str(getattr(self, "registry_instance_id", "UNKNOWN")),
            "hostInstanceId": str(getattr(self, "host_instance_id", "UNKNOWN")),
            "boundPageId": getattr(self, "bound_page_id", None),
            "ownerThreadId": getattr(self, "owner_thread_id", "UNKNOWN"),
            "processPid": int(getattr(self, "process_pid", os.getpid())),
        }

    def _capture_diagnostic_binding_baseline(self, page: Any, context: Any) -> None:
        """Keep opaque object references for lifecycle comparison only."""
        try:
            main_frame = page.main_frame
        except Exception:
            main_frame = None
        self._diagnostic_bound_page_object = page
        self._diagnostic_bound_main_frame_object = main_frame
        self._diagnostic_bound_context_object = context

    def _diagnostic_fingerprint(self, kind: str, value: Any) -> str:
        if value is None:
            return "UNKNOWN"
        payload = f"{kind}:{id(value)}".encode("ascii")
        return hmac.new(self._diagnostic_fingerprint_salt, payload, hashlib.sha256).hexdigest()[:24]

    @staticmethod
    def _diagnostic_host_class(url: Any) -> str:
        from urllib.parse import urlsplit

        try:
            raw = str(url or "")
            parsed = urlsplit(raw)
            hostname = (parsed.hostname or "").lower().rstrip(".")
        except Exception:
            return "OTHER"
        if raw in {"about:blank", "about:srcdoc", ""} or not hostname:
            return "BLANK"
        if hostname == "chatgpt.com" or hostname.endswith(".chatgpt.com"):
            return "CHATGPT"
        if hostname == "openai.com" or hostname.endswith(".openai.com"):
            return "OPENAI"
        if any(marker in hostname for marker in ("challenge", "captcha", "arkoselabs", "hcaptcha", "recaptcha")):
            return "CHALLENGE"
        return "OTHER"

    @staticmethod
    def _diagnostic_count(value: Any) -> int:
        if isinstance(value, bool):
            return int(value)
        try:
            return max(0, min(100000, int(value)))
        except (TypeError, ValueError, OverflowError):
            return 0

    @classmethod
    def _sanitize_probe_rows(cls, raw: Any, group: str) -> dict[str, dict[str, Any]]:
        expected_ids = _STRUCTURAL_PROBE_IDS[group]
        source = raw if isinstance(raw, list) else []
        observed: dict[str, dict[str, Any]] = {}
        for row in source:
            if not isinstance(row, dict):
                continue
            probe_id = row.get("probe_id")
            if probe_id not in expected_ids:
                continue
            count = cls._diagnostic_count(row.get("count"))
            visible_count = min(count, cls._diagnostic_count(row.get("visible_count")))
            editable_count = min(visible_count, cls._diagnostic_count(row.get("editable_count")))
            observed[str(probe_id)] = {
                "probe_id": str(probe_id),
                "present": count > 0,
                "count": count,
                "visible_count": visible_count,
                "editable_count": editable_count,
            }
        return observed

    @staticmethod
    def _diagnostic_probe_rows(rows: dict[str, dict[str, Any]], group: str) -> list[dict[str, Any]]:
        return [
            rows.get(probe_id, {
                "probe_id": probe_id,
                "present": False,
                "count": 0,
                "visible_count": 0,
                "editable_count": 0,
            })
            for probe_id in _STRUCTURAL_PROBE_IDS[group]
        ]

    def _classify_structural_root_cause(self, result: dict[str, Any]) -> tuple[str, str, list[str]]:
        evidence: list[str] = []
        if result["PAGE_INSTANCE_CHANGED_SINCE_BIND"]:
            return "BOUND_PAGE_REPLACED", "HIGH", ["PAGE_INSTANCE_CHANGED_SINCE_BIND=true"]
        if not result["CURRENT_PAGE_STILL_IN_CONTEXT"] or result["CONTEXT_CHANGED_SINCE_BIND"]:
            return "PAGE_CONTEXT_MISMATCH", "HIGH", [
                f"CURRENT_PAGE_STILL_IN_CONTEXT={str(result['CURRENT_PAGE_STILL_IN_CONTEXT']).lower()}",
                f"CONTEXT_CHANGED_SINCE_BIND={str(result['CONTEXT_CHANGED_SINCE_BIND']).lower()}",
            ]
        if result["PAGE_URL_HOST_CLASS"] not in {"CHATGPT", "OPENAI"}:
            return "PAGE_NAVIGATED_AWAY", "HIGH", [f"PAGE_URL_HOST_CLASS={result['PAGE_URL_HOST_CLASS']}"]

        challenge = sum(row["visible_count"] for row in result["CHALLENGE_PROBES"])
        if challenge:
            return "CHALLENGE_OR_INTERSTITIAL", "HIGH", [f"CHALLENGE_PROBES.visible_count={challenge}"]

        main_frame_composer = next(
            (item for item in result["COMPOSER_FRAME_EVIDENCE"] if item["is_main_frame"]), None
        )
        other_frame_composer = sum(
            item["visible_editable_composer_count"]
            for item in result["COMPOSER_FRAME_EVIDENCE"]
            if not item["is_main_frame"]
        )
        main_editable = (main_frame_composer or {}).get("visible_editable_composer_count", 0)
        if other_frame_composer and not main_editable:
            return "COMPOSER_IN_DIFFERENT_FRAME", "HIGH", [
                f"MAIN_FRAME_EDITABLE_COMPOSERS={main_editable}",
                f"OTHER_FRAME_EDITABLE_COMPOSERS={other_frame_composer}",
            ]

        candidate_count = sum(row["count"] for row in result["COMPOSER_PROBES"])
        visible_count = sum(row["visible_count"] for row in result["COMPOSER_PROBES"])
        editable_count = sum(row["editable_count"] for row in result["COMPOSER_PROBES"])
        semantic_visible = (
            result["VISIBLE_TEXTAREA_COUNT"]
            + result["VISIBLE_CONTENTEDITABLE_COUNT"]
            + result["VISIBLE_ROLE_TEXTBOX_COUNT"]
        )
        semantic_editable = result["VISIBLE_EDITABLE_FORM_CONTROL_COUNT"]
        if challenge:
            return "CHALLENGE_OR_INTERSTITIAL", "HIGH", [f"CHALLENGE_PROBES.visible_count={challenge}"]
        if candidate_count and not visible_count:
            return "COMPOSER_PRESENT_BUT_HIDDEN", "HIGH", [
                f"COMPOSER_PROBES.count={candidate_count}", "COMPOSER_PROBES.visible_count=0"
            ]
        if visible_count and not editable_count:
            return "COMPOSER_PRESENT_BUT_NOT_EDITABLE", "HIGH", [
                f"COMPOSER_PROBES.visible_count={visible_count}", "COMPOSER_PROBES.editable_count=0"
            ]
        if semantic_editable and not editable_count:
            return "COMPOSER_SELECTOR_DRIFT", "HIGH", [
                f"VISIBLE_EDITABLE_FORM_CONTROL_COUNT={semantic_editable}",
                f"COMPOSER_PROBES.editable_count={editable_count}",
            ]
        app_shell_visible = sum(row["visible_count"] for row in result["APP_SHELL_PROBES"])
        authenticated_visible = sum(row["visible_count"] for row in result["AUTHENTICATED_PROBES"])
        if result["AUTH_STATE"] == "AUTHENTICATED" and not authenticated_visible:
            return "AUTH_FINGERPRINT_SELECTOR_DRIFT", "MEDIUM", [
                "AUTH_STATE=AUTHENTICATED", "AUTHENTICATED_PROBES.visible_count=0"
            ]
        if result["BODY_PRESENT"] and not app_shell_visible:
            return "APP_SHELL_SELECTOR_DRIFT", "MEDIUM", [
                "BODY_PRESENT=true", "APP_SHELL_PROBES.visible_count=0"
            ]
        if editable_count and result["COMPOSER_READY"] is False:
            return "RESOLVER_LOGIC_BUG", "MEDIUM", [
                f"COMPOSER_PROBES.editable_count={editable_count}", "COMPOSER_READY=false"
            ]
        if not candidate_count and not semantic_visible:
            return "CHATGPT_UI_NO_COMPOSER_PRESENT", "MEDIUM", [
                "COMPOSER_PROBES.count=0", f"VISIBLE_SEMANTIC_EDITABLE_COUNT={semantic_visible}"
            ]
        if not editable_count and semantic_editable:
            return "COMPOSER_SELECTOR_DRIFT", "MEDIUM", [
                f"VISIBLE_EDITABLE_FORM_CONTROL_COUNT={semantic_editable}",
                f"COMPOSER_PROBES.editable_count={editable_count}",
            ]
        evidence.extend((
            f"COMPOSER_PROBES.count={candidate_count}",
            f"COMPOSER_PROBES.visible_count={visible_count}",
            f"COMPOSER_PROBES.editable_count={editable_count}",
            f"DOCUMENT_READY_STATE={result['DOCUMENT_READY_STATE']}",
        ))
        return "UNKNOWN", "LOW", evidence

    def get_chatgpt_structural_diagnostics(self) -> dict[str, Any]:
        """Read fixed structural metadata from the already-bound ChatGPT page only."""
        self._record_owner_thread("get_chatgpt_structural_diagnostics")
        page = self.page
        context = self.context
        browser = self.browser
        baseline_page = self._diagnostic_bound_page_object
        baseline_frame = self._diagnostic_bound_main_frame_object
        baseline_context = self._diagnostic_bound_context_object
        if page is None or context is None or browser is None or baseline_page is None:
            raise PlaywrightAttachedBrainError(
                "Formal bound page is unavailable for structural diagnostics",
                code="FORMAL_DOM_DIAGNOSTIC_BOUND_PAGE_UNAVAILABLE",
                stage="FORMAL_DOM_DIAGNOSTIC",
            )
        try:
            main_frame = page.main_frame
            frames = list(page.frames)
            if main_frame not in frames:
                frames.insert(0, main_frame)
            if not frames:
                frames = [main_frame]
            frame_results: list[tuple[Any, dict[str, Any]]] = []
            for frame in frames[:100]:
                raw = frame.evaluate(_STRUCTURAL_DIAGNOSTIC_SCRIPT)
                if not isinstance(raw, dict):
                    raise TypeError("invalid fixed diagnostic result")
                frame_results.append((frame, raw))
            if not frame_results:
                raise TypeError("no frame diagnostic result")
        except PlaywrightAttachedBrainError:
            raise
        except Exception as exc:
            # Never place browser error strings in the response: some drivers
            # include page-derived values in exception text.
            raise PlaywrightAttachedBrainError(
                "Fixed structural DOM read failed",
                code="FORMAL_DOM_DIAGNOSTIC_READ_FAILED",
                stage="FORMAL_DOM_DIAGNOSTIC",
            ) from exc

        main_result = next((raw for frame, raw in frame_results if frame is main_frame), frame_results[0][1])
        aggregate: dict[str, dict[str, dict[str, Any]]] = {
            group: {} for group in _STRUCTURAL_PROBE_IDS
        }
        frame_evidence: list[dict[str, Any]] = []
        frame_host_classes: list[str] = []
        for frame_index, (frame, raw) in enumerate(frame_results):
            host_class = self._diagnostic_host_class(getattr(frame, "url", ""))
            frame_host_classes.append(host_class)
            raw_groups = raw.get("probe_results") if isinstance(raw.get("probe_results"), dict) else {}
            sanitized_groups: dict[str, dict[str, dict[str, Any]]] = {}
            for group in _STRUCTURAL_PROBE_IDS:
                sanitized_groups[group] = self._sanitize_probe_rows(raw_groups.get(group), group)
                for probe_id, row in sanitized_groups[group].items():
                    total = aggregate[group].setdefault(probe_id, {
                        "probe_id": probe_id,
                        "present": False,
                        "count": 0,
                        "visible_count": 0,
                        "editable_count": 0,
                    })
                    total["count"] = min(100000, total["count"] + row["count"])
                    total["visible_count"] = min(100000, total["visible_count"] + row["visible_count"])
                    total["editable_count"] = min(100000, total["editable_count"] + row["editable_count"])
                    total["present"] = total["count"] > 0
            composer_rows = sanitized_groups["COMPOSER_PROBES"].values()
            frame_evidence.append({
                "frame_index": frame_index,
                "is_main_frame": frame is main_frame,
                "host_class": host_class,
                "candidate_count": sum(row["count"] for row in composer_rows),
                "visible_editable_composer_count": sum(row["editable_count"] for row in composer_rows),
            })

        probe_output = {
            group: self._diagnostic_probe_rows(aggregate[group], group)
            for group in _STRUCTURAL_PROBE_IDS
        }
        try:
            is_closed = bool(page.is_closed())
        except Exception:
            is_closed = True
        try:
            current_context_pages = list(context.pages)
            page_in_context = any(candidate is page for candidate in current_context_pages)
        except Exception:
            current_context_pages = []
            page_in_context = False
        try:
            contexts = list(browser.contexts)
            page_count = sum(len(list(item.pages)) for item in contexts)
        except Exception:
            contexts = []
            page_count = 0
        current_page_in_context = page_in_context and any(item is context for item in contexts)

        def count_from(raw: dict[str, Any], key: str) -> int:
            return self._diagnostic_count(raw.get(key))

        page_url_class = self._diagnostic_host_class(getattr(page, "url", ""))
        main_frame_url_class = self._diagnostic_host_class(getattr(main_frame, "url", ""))
        ready_state = str(main_result.get("document_ready_state", "UNKNOWN"))
        if ready_state not in {"loading", "interactive", "complete"}:
            ready_state = "UNKNOWN"
        cached_candidate = next(
            (item for item in self.page_candidates if item.get("selectedCandidate")), {}
        )
        cached_composer = self._diagnostic_count(cached_candidate.get("editableComposerCount")) > 0
        brain_state = getattr(self.state, "value", self.state)
        result: dict[str, Any] = {
            "DIAGNOSTIC_SCHEMA_VERSION": "P3A_STRUCTURAL_V1",
            "PAGE_INSTANCE_FINGERPRINT": self._diagnostic_fingerprint("page", page),
            "MAIN_FRAME_FINGERPRINT": self._diagnostic_fingerprint("main-frame", main_frame),
            "BROWSER_CONTEXT_FINGERPRINT": self._diagnostic_fingerprint("context", context),
            "PAGE_URL_HOST_CLASS": page_url_class,
            "DOCUMENT_READY_STATE": ready_state,
            "MAIN_FRAME_HOST_CLASS": main_frame_url_class,
            "FRAME_COUNT": len(frames),
            "FRAME_HOST_CLASSES": frame_host_classes,
            "BODY_PRESENT": bool(main_result.get("body_present", False)),
            "MAIN_ELEMENT_COUNT": count_from(main_result, "main_element_count"),
            "FORM_COUNT": count_from(main_result, "form_count"),
            "TEXTAREA_COUNT": count_from(main_result, "textarea_count"),
            "CONTENTEDITABLE_TRUE_COUNT": count_from(main_result, "contenteditable_true_count"),
            "ROLE_TEXTBOX_COUNT": count_from(main_result, "role_textbox_count"),
            "IFRAME_COUNT": count_from(main_result, "iframe_count"),
            "DIALOG_COUNT": count_from(main_result, "dialog_count"),
            "BUTTON_COUNT": count_from(main_result, "button_count"),
            "VISIBLE_TEXTAREA_COUNT": count_from(main_result, "visible_textarea_count"),
            "VISIBLE_CONTENTEDITABLE_COUNT": count_from(main_result, "visible_contenteditable_count"),
            "VISIBLE_ROLE_TEXTBOX_COUNT": count_from(main_result, "visible_role_textbox_count"),
            "VISIBLE_EDITABLE_FORM_CONTROL_COUNT": count_from(main_result, "visible_editable_form_control_count"),
            "FORM_WITH_EDITABLE_DESCENDANT_COUNT": count_from(main_result, "form_with_editable_descendant_count"),
            **probe_output,
            "COMPOSER_FRAME_EVIDENCE": frame_evidence,
            "CURRENT_PAGE_STILL_IN_CONTEXT": current_page_in_context,
            "BOUND_PAGE_IS_CLOSED": is_closed,
            "PAGE_INSTANCE_CHANGED_SINCE_BIND": page is not baseline_page,
            "MAIN_FRAME_CHANGED_SINCE_BIND": main_frame is not baseline_frame,
            "CONTEXT_CHANGED_SINCE_BIND": context is not baseline_context,
            "PAGE_COUNT_CURRENT": page_count,
            "CONTEXT_COUNT_CURRENT": len(contexts),
            "AUTH_STATE": str(self.auth_state or "UNKNOWN"),
            "COMPOSER_READY": bool(brain_state == PlaywrightAttachedBrainState.READY and cached_composer),
            "BRAIN_STATE": str(brain_state),
        }
        root_cause, confidence, evidence = self._classify_structural_root_cause(result)
        result["COMPOSER_ROOT_CAUSE_CLASS"] = root_cause
        result["ROOT_CAUSE_CONFIDENCE"] = confidence
        result["ROOT_CAUSE_EVIDENCE"] = evidence
        return result

    @contextmanager
    def _dev_probe_lock_context(self):
        """Allow the atomic operation to hold the probe lock across both stages."""
        if getattr(self, "_atomic_probe_lock_held", False):
            yield
            return
        with self._dev_probe_lock:
            yield

    def _write_stack(self, error: PlaywrightAttachedBrainError) -> None:
        _append_jsonl(CONNECT_STACK_LOG, {
            "attemptId": self.connect_attempt_id,
            "timestamp": _utc_now(),
            "stackId": error.stack_id,
            "stage": error.stage,
            "errorCode": error.code,
            "stack": error.stack,
        })

    def _record_owner_thread(self, operation_name: str) -> int:
        """Record and enforce the single thread allowed to touch sync Playwright."""
        actual_thread_id = threading.get_ident()
        entry = {
            "callerThreadId": actual_thread_id,
            "playwrightThreadId": actual_thread_id,
            "ownerThreadId": self.owner_thread_id,
        }
        self._thread_operation_matrix[operation_name] = entry
        if actual_thread_id != self.owner_thread_id:
            diagnostic = {
                "expectedThreadId": self.owner_thread_id,
                "actualThreadId": actual_thread_id,
                "operationName": operation_name,
            }
            self.thread_affinity_error = diagnostic
            error = PlaywrightAttachedBrainError(
                "Playwright operation called from a non-owner thread",
                code="PLAYWRIGHT_THREAD_AFFINITY_VIOLATION",
                stage=operation_name,
            )
            self.last_error_details = {**error.envelope(self.connect_attempt_id or "UNKNOWN"), **diagnostic}
            raise error
        return actual_thread_id

    def _fail(
        self,
        state: str,
        reason: str,
        *,
        stage: str = "UNKNOWN",
        cause: BaseException | None = None,
        caller: str | None = None,
    ) -> None:
        self.state = state
        self.last_error = reason
        error = PlaywrightAttachedBrainError(reason, code=reason, stage=stage, cause=cause)
        self.last_error_details = error.envelope(self.connect_attempt_id or "UNKNOWN")
        self.diagnostics["lastFailureStage"] = stage
        if getattr(self, "connection_mode", None) == "EXACT_WS" and self.diagnostics.get("formalConnectFailure") is None:
            started = self._stage_started_clock.get(stage) or self._stage_started_clock.get("T5_CONNECT_OVER_CDP_START") or time.monotonic()
            self._record_formal_failure(
                stage=self._formal_failure_stage(stage),
                error=cause or error,
                started=started,
                stack_id=error.stack_id,
            )
        self._trace(stage, "FAIL", code=reason, error=cause or error)
        self._write_stack(error)
        self._finish_attempt()
        loss_reason = {
            "PLAYWRIGHT_BROWSER_DISCONNECTED": "BROWSER_DISCONNECTED",
            "CHATGPT_PAGE_NOT_FOUND": "BOUND_PAGE_CLOSED",
            "CHATGPT_PAGE_URL_INVALID": "BOUND_PAGE_NAVIGATED_AWAY",
            "DEV_PROBE_PAGE_LOST": "BOUND_PAGE_CLOSED",
            "DEV_PROBE_PAGE_URL_INVALID": "BOUND_PAGE_NAVIGATED_AWAY",
        }.get(reason)
        if loss_reason is None and state in {
            PlaywrightAttachedBrainState.BROWSER_LOST,
            PlaywrightAttachedBrainState.PAGE_LOST,
        }:
            loss_reason = "UNKNOWN_RUNTIME_DESTRUCTION"
        if loss_reason is not None and not getattr(self, "_closing", False):
            self._runtime_lifecycle_event(
                "RUNTIME_LOSS_EVENT",
                loss_reason,
                caller=caller or stage,
                error=cause,
            )
        signature = (state, reason)
        if signature != self._last_failure and self.on_failure is not None:
            self._last_failure = signature
            self.on_failure(state, reason)

    def _mark_success(self, stage: str) -> None:
        # Keep both spellings during the compatibility period; the public
        # diagnostic name is lastSuccessfulStage.
        self.diagnostics["lastSuccessfulStage"] = stage
        self.diagnostics["lastSuccessStage"] = stage

    @staticmethod
    def _error_errno(error: BaseException) -> str:
        value = getattr(error, "errno", None)
        if isinstance(value, int):
            return str(value)
        upper = _safe_text(error, 1000).upper()
        for name in ("EPERM", "EACCES", "ECONNREFUSED", "ETIMEDOUT", "ENETUNREACH", "EHOSTUNREACH"):
            if name in upper:
                return name
        return "UNKNOWN"

    @staticmethod
    def _error_syscall(error: BaseException) -> str:
        syscall = getattr(error, "syscall", None)
        return _safe_text(syscall, 80) if syscall else "UNKNOWN"

    def _formal_connect_options(self) -> dict[str, Any]:
        """Return the redacted effective options used by the formal call."""
        if self.connection_mode != "EXACT_WS":
            return {
                "endpointKind": "LEGACY_URL",
                "endpointSource": self.endpoint_source,
                "timeoutMs": self.connect_timeout_ms,
                "isLocal": "NOT_PASSED",
                "noDefaults": "NOT_PASSED",
                "headers": "NOT_PASSED",
                "slowMo": "NOT_PASSED",
            }
        return {
            "endpointKind": "EXACT_WS",
            "endpointSource": FORMAL_ENDPOINT_SOURCE,
            "timeoutMs": FORMAL_EXACT_CONNECT_TIMEOUT_MS,
            "isLocal": True,
            "noDefaults": True,
            "headers": "NOT_PASSED",
            "slowMo": "NOT_PASSED",
        }

    def _record_formal_failure(
        self,
        *,
        stage: str,
        error: BaseException,
        started: float,
        stack_id: str | None = None,
    ) -> None:
        details = {
            "stage": stage,
            "errorType": type(error).__name__,
            "errorMessage": _safe_text(error, 1000),
            "errno": self._error_errno(error),
            "syscall": self._error_syscall(error),
            "durationMs": max(0, round((time.monotonic() - started) * 1000)),
            "stackId": stack_id or "UNKNOWN",
        }
        # The first failure is the acceptance evidence.  Preserve it even if
        # cleanup/status inspection later produces a secondary exception.
        if self.diagnostics.get("formalConnectFailure") is None:
            self.diagnostics["formalConnectFailure"] = details

    @staticmethod
    def _formal_failure_stage(stage: str) -> str:
        mapping = {
            "T5_CONNECT_OVER_CDP_START": "CONNECT_OVER_CDP",
            "T6_CONNECT_OVER_CDP_RESULT": "CONNECT_OVER_CDP",
            "T7_CONTEXTS_START": "GET_CONTEXT",
            "T7_CONTEXTS_RESULT": "GET_CONTEXT",
            "T8_CONTEXTS_RESULT": "GET_CONTEXT",
            "T9_PAGES_START": "ENUMERATE_PAGES",
            "T10_PAGES_RESULT": "ENUMERATE_PAGES",
            "T11_CHATGPT_RESOLVER_START": "RESOLVE_CHATGPT_PAGE",
            "T12_CHATGPT_RESOLVER_RESULT": "RESOLVE_CHATGPT_PAGE",
            "T13_COMPOSER_CHECK_START": "COMPOSER_CHECK",
            "T13_COMPOSER_CHECK": "COMPOSER_CHECK",
            "T14_AUTH_STATE_RESULT": "AUTH_CHECK",
        }
        return mapping.get(stage, stage)

    def _load_playwright(self) -> Any:
        if self.playwright_factory is not None:
            return self.playwright_factory()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise PlaywrightAttachedBrainError("Playwright module could not be loaded", code="PLAYWRIGHT_LOAD_FAILED", stage="T4_PLAYWRIGHT_MODULE_READY", cause=exc) from exc
        versions = self._version_diagnostics()
        if versions["playwright"] not in {"UNKNOWN", "NOT_INSTALLED"} and versions["playwrightCore"] not in {"UNKNOWN", "NOT_INSTALLED"} and versions["playwright"] != versions["playwrightCore"]:
            raise PlaywrightAttachedBrainError("Playwright package versions do not match", code="PLAYWRIGHT_PACKAGE_VERSION_MISMATCH", stage="T4_PLAYWRIGHT_MODULE_READY")
        return sync_playwright().start()

    def _assert_formal_configuration(self) -> None:
        if self.connection_mode != "EXACT_WS":
            return
        if (
            self.attach_mode != FORMAL_ATTACH_MODE
            or self.endpoint_source != FORMAL_ENDPOINT_SOURCE
            or self.dedicated_user_data_dir != DEDICATED_CHROME_USER_DATA_DIR
            or self.endpoint_resolver is None
        ):
            raise PlaywrightAttachedBrainError(
                "Formal dedicated Chrome configuration is missing or invalid",
                code="FORMAL_ATTACH_CONFIGURATION_INVALID",
                stage="T3_BRAIN_CONNECT_START",
            )

    def _preflight_cdp(self) -> dict[str, Any]:
        if self.cdp_preflight is not None:
            return self.cdp_preflight(self.cdp_endpoint)
        request = Request(self.cdp_endpoint.rstrip("/") + "/json/version", method="GET")
        try:
            with urlopen(request, timeout=2.0) as response:
                status = int(getattr(response, "status", None) or response.getcode())
                raw = response.read()
        except (OSError, URLError) as exc:
            raise PlaywrightAttachedBrainError("CDP endpoint is unreachable", code="CDP_ENDPOINT_UNREACHABLE", stage="T5_CONNECT_OVER_CDP_START", cause=exc) from exc
        try:
            shape = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlaywrightAttachedBrainError("CDP version response is invalid", code="CDP_VERSION_ENDPOINT_INVALID", stage="T5_CONNECT_OVER_CDP_START", cause=exc) from exc
        if status != 200 or not isinstance(shape, dict) or not isinstance(shape.get("webSocketDebuggerUrl"), str) or not shape["webSocketDebuggerUrl"]:
            raise PlaywrightAttachedBrainError("CDP version response has invalid shape", code="CDP_VERSION_ENDPOINT_INVALID", stage="T5_CONNECT_OVER_CDP_START")
        browser_version = shape.get("Browser")
        if isinstance(browser_version, str):
            self.diagnostics["versions"]["chrome"] = _safe_text(browser_version, 120)
        self.diagnostics["cdpHttpStatus"] = status
        self.diagnostics["jsonVersionShape"] = "VALID_WITH_WEBSOCKET_URL"
        return {"status": status, "shape": "VALID_WITH_WEBSOCKET_URL"}

    def _wire_browser(self) -> None:
        if self.browser is None or not hasattr(self.browser, "on"):
            return
        self.browser.on("disconnected", lambda *_args: self._on_browser_disconnect())

    def _on_browser_disconnect(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._browser_disconnected = True
        self._fail(
            PlaywrightAttachedBrainState.BROWSER_LOST,
            "PLAYWRIGHT_BROWSER_DISCONNECTED",
            stage="T6_BROWSER_DISCONNECTED",
            caller="PlaywrightAttachedBrainHost._on_browser_disconnect",
        )

    def _wire_page(self, page: Any) -> None:
        if not hasattr(page, "on"):
            return
        page.on("close", lambda *_args: self._on_page_close())
        page.on("framenavigated", lambda *_args: self._on_page_navigation())

    def _on_page_close(self) -> None:
        if getattr(self, "_closing", False):
            return
        self._fail(
            PlaywrightAttachedBrainState.PAGE_LOST,
            "CHATGPT_PAGE_NOT_FOUND",
            stage="T10_PAGES_RESULT",
            caller="PlaywrightAttachedBrainHost._on_page_close",
        )

    def _on_page_navigation(self) -> None:
        if getattr(self, "_closing", False):
            return
        if self.page is None:
            return
        try:
            if not is_allowed_chatgpt_url(str(self.page.url or "")):
                self._fail(
                    PlaywrightAttachedBrainState.PAGE_LOST,
                    "CHATGPT_PAGE_URL_INVALID",
                    stage="T10_PAGES_RESULT",
                    caller="PlaywrightAttachedBrainHost._on_page_navigation",
                )
        except Exception:
            self._fail(
                PlaywrightAttachedBrainState.PAGE_LOST,
                "CHATGPT_PAGE_NOT_FOUND",
                stage="T10_PAGES_RESULT",
                caller="PlaywrightAttachedBrainHost._on_page_navigation",
            )

    def _contexts(self) -> list[Any]:
        if self.browser is None or self._browser_disconnected:
            if not getattr(self, "_closing", False):
                self._runtime_lifecycle_event_once(
                    "RUNTIME_LOSS_EVENT",
                    "BROWSER_DISCONNECTED",
                    caller="PlaywrightAttachedBrainHost._contexts",
                )
            raise PlaywrightAttachedBrainError("Browser is not connected", code="PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T7_CONTEXTS_START")
        try:
            return list(self.browser.contexts)
        except Exception as exc:
            self._fail(PlaywrightAttachedBrainState.BROWSER_LOST, "PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T7_CONTEXTS_START", cause=exc)
            raise PlaywrightAttachedBrainError("Browser contexts could not be read", code="PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T7_CONTEXTS_START", cause=exc) from exc

    @staticmethod
    def _safe_page_title(page: Any) -> str:
        """Keep a generic title only; ChatGPT conversation titles are content."""
        try:
            title = page.title() if callable(getattr(page, "title", None)) else ""
            return "ChatGPT" if "chatgpt" in str(title).lower() else "UNKNOWN"
        except Exception:
            return "UNKNOWN"

    @staticmethod
    def _page_fingerprint(page: Any, index: int) -> dict[str, Any]:
        from urllib.parse import urlsplit

        raw_url = str(getattr(page, "url", "") or "")
        parsed = urlsplit(raw_url)
        fingerprint: dict[str, Any] = {
            "candidateIndex": index,
            "pathname": parsed.path or "/",
            "pageTitle": PlaywrightAttachedBrainHost._safe_page_title(page),
            "isClosed": False,
            "visibilityState": "UNKNOWN",
            "documentReadyState": "UNKNOWN",
            "loginUiDetected": False,
            "authenticatedShellDetected": False,
            "composerCandidateCount": 0,
            "visibleComposerCount": 0,
            "editableComposerCount": 0,
        }
        try:
            closed = getattr(page, "is_closed", None)
            fingerprint["isClosed"] = bool(closed()) if callable(closed) else False
        except Exception:
            fingerprint["isClosed"] = True
        try:
            inspected = page.evaluate(_PAGE_FINGERPRINT_SCRIPT)
            if isinstance(inspected, dict):
                for key in (
                    "visibilityState", "documentReadyState", "loginUiDetected",
                    "authenticatedShellDetected", "composerCandidateCount",
                    "visibleComposerCount", "editableComposerCount",
                ):
                    if key in inspected:
                        fingerprint[key] = inspected[key]
                return fingerprint
        except Exception:
            pass

        # Test doubles and older Playwright shims may not support the bounded
        # evaluate.  The fallback still reads only selectors and document
        # readiness, never page content.
        try:
            visibility = page.evaluate("document.visibilityState")
            fingerprint["visibilityState"] = visibility if visibility in {"visible", "hidden"} else "UNKNOWN"
        except Exception:
            pass
        try:
            ready = page.evaluate("document.readyState")
            fingerprint["documentReadyState"] = ready if ready in {"loading", "interactive", "complete"} else "UNKNOWN"
        except Exception:
            pass
        fingerprint["loginUiDetected"] = (
            ChatGPTPlaywrightDomAdapter._has_any(page, ChatGPTPlaywrightDomAdapter.LOGIN_SELECTORS)
            or ChatGPTPlaywrightDomAdapter._has_login_text(page)
        )
        fingerprint["authenticatedShellDetected"] = (
            ChatGPTPlaywrightDomAdapter._has_any(page, ChatGPTPlaywrightDomAdapter.APP_SHELL_SELECTORS)
            and not fingerprint["loginUiDetected"]
        )
        seen: set[int] = set()
        seen_refs: list[Any] = []
        for selector in ChatGPTPlaywrightDomAdapter.COMPOSER_SELECTORS:
            try:
                locator = page.locator(selector)
                count = min(ChatGPTPlaywrightDomAdapter._count(locator), 50)
                fingerprint["composerCandidateCount"] += count
                for item_index in range(count):
                    candidate = locator.nth(item_index)
                    marker = id(candidate)
                    if marker in seen:
                        continue
                    seen.add(marker)
                    # Keep test doubles and locator wrappers alive while the
                    # selector sweep runs; otherwise CPython may reuse an
                    # object id and hide a later real composer candidate.
                    seen_refs.append(candidate)
                    try:
                        if candidate.is_visible():
                            fingerprint["visibleComposerCount"] += 1
                            if candidate.is_editable():
                                fingerprint["editableComposerCount"] += 1
                    except Exception:
                        continue
            except Exception:
                continue
        return fingerprint

    def _find_chatgpt_page(self) -> tuple[Any | None, Any | None]:
        self._record_owner_thread("page_resolver")
        contexts = self._contexts()
        context_pages: list[tuple[Any, Any]] = []
        for context in contexts:
            try:
                pages = list(context.pages)
            except Exception as exc:
                self._fail(PlaywrightAttachedBrainState.BROWSER_LOST, "PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T9_PAGES_START", cause=exc)
                raise PlaywrightAttachedBrainError("Browser pages could not be read", code="PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T9_PAGES_START", cause=exc) from exc
            context_pages.extend((context, page) for page in pages)
        pages = [page for _context, page in context_pages]
        selected, diagnostics = self.page_resolver.resolve_with_diagnostics(pages, self._page_fingerprint)
        loading = next((item for item in diagnostics if item.get("selectedCandidate") and item.get("category") == "AUTHENTICATED_LOADING"), None)
        if loading is not None and selected is not None:
            # Give a hydrating authenticated page one event-driven opportunity
            # to expose its Composer.  There is no fixed sleep and no message
            # or page-content read in this wait.
            wait_for_load_state = getattr(selected, "wait_for_load_state", None)
            if callable(wait_for_load_state):
                try:
                    wait_for_load_state("domcontentloaded", timeout=1500)
                except Exception:
                    pass
                selected, diagnostics = self.page_resolver.resolve_with_diagnostics(pages, self._page_fingerprint)
        self.page_candidates = diagnostics
        self.selected_candidate_index = next(
            (item["candidateIndex"] for item in diagnostics if item.get("selectedCandidate")), None
        )
        previous_page = getattr(self, "_bound_page_object", None)
        resolved_page_id = f"page-{self.selected_candidate_index}" if self.selected_candidate_index is not None else None
        if previous_page is not None and selected is not None and selected is not previous_page:
            counts = getattr(self, "_runtime_lifecycle_counts", {})
            counts["boundPageRebindCount"] = int(counts.get("boundPageRebindCount", 0)) + 1
            self._runtime_lifecycle_counts = counts
            self._runtime_lifecycle_event(
                "BOUND_PAGE_REBIND",
                "RECONNECT_REPLACEMENT",
                caller="PlaywrightAttachedBrainHost._find_chatgpt_page",
            )
        self.bound_page_id = resolved_page_id
        self._bound_page_object = selected
        if selected is None:
            self._diagnostic_bound_page_object = None
            self._diagnostic_bound_main_frame_object = None
            self._diagnostic_bound_context_object = None
            return None, None
        selected_context = next((context for context, page in context_pages if page is selected), None)
        if selected_context is not None:
            self._capture_diagnostic_binding_baseline(selected, selected_context)
        return selected_context, selected

    def connect(self, connect_attempt_id: str | None = None) -> None:
        self._record_owner_thread("connect")
        # The guarded operation below retains the formal self.channel /
        # connect_over_cdp path in _connect_unlocked; this wrapper only adds
        # single-flight protection around that existing connection operation.
        with self._connect_lock:
            if self._connect_in_progress:
                raise PlaywrightAttachedBrainError(
                    "A connection attempt is already in progress",
                    code="CONNECT_OPERATION_IN_PROGRESS",
                    stage="T2_IPC_HANDLER_ENTER",
                )
            self._connect_in_progress = True
        try:
            return self._connect_unlocked(connect_attempt_id=connect_attempt_id)
        finally:
            with self._connect_lock:
                self._connect_in_progress = False

    def connect_url_only_for_diagnostics(self, *, ensure_if_missing: bool = True) -> dict[str, Any]:
        """Attach through the formal exact-WS path and bind by URL metadata only.

        This deliberately bypasses the legacy page fingerprint and all auth / Composer
        resolvers.  It exists for privacy-bounded runtime diagnostics, not Brain use.
        """
        self._record_owner_thread("url_only_diagnostic_connect")
        with self._connect_lock:
            if self._connect_in_progress:
                raise PlaywrightAttachedBrainError(
                    "A connection attempt is already in progress",
                    code="CONNECT_OPERATION_IN_PROGRESS",
                    stage="T2_IPC_HANDLER_ENTER",
                )
            self._connect_in_progress = True
        try:
            if self._has_live_browser_transport():
                return self._url_only_rebind_connected_browser(ensure_if_missing=ensure_if_missing)
            return self._connect_unlocked(
                url_only_rebind=True,
                ensure_if_missing=ensure_if_missing,
            )
        finally:
            with self._connect_lock:
                self._connect_in_progress = False

    def _url_only_rebind_connected_browser(self, *, ensure_if_missing: bool) -> dict[str, Any]:
        """URL-only rebinding when the same formal Browser transport is already live."""
        self._record_owner_thread("url_only_rebind_connected_browser")
        contexts = self._contexts()
        if not contexts:
            raise PlaywrightAttachedBrainError(
                "No formal Browser context is available",
                code="PLAYWRIGHT_CONTEXT_NOT_FOUND",
                stage="T7_CONTEXTS_RESULT",
            )
        return self._bind_chatgpt_page_by_url(contexts, ensure_if_missing=ensure_if_missing)

    def _bind_chatgpt_page_by_url(
        self,
        contexts: list[Any],
        *,
        ensure_if_missing: bool,
    ) -> dict[str, Any]:
        """Select a ChatGPT page using only its allowlisted URL and liveness metadata."""
        context_pages: list[tuple[Any, Any]] = []
        for context in contexts:
            try:
                pages = list(context.pages)
            except Exception as exc:
                raise PlaywrightAttachedBrainError(
                    "Formal Browser pages could not be enumerated",
                    code="PLAYWRIGHT_PAGES_UNAVAILABLE",
                    stage="T9_PAGES_START",
                    cause=exc,
                ) from exc
            context_pages.extend((context, page) for page in pages)

        chatgpt_pages: list[tuple[Any, Any, int]] = []
        for index, (context, page) in enumerate(context_pages):
            try:
                closed = bool(page.is_closed()) if callable(getattr(page, "is_closed", None)) else False
                raw_url = str(getattr(page, "url", "") or "")
            except Exception:
                continue
            if not closed and is_allowed_chatgpt_url(raw_url):
                chatgpt_pages.append((context, page, index))

        action = "REUSED_EXISTING_PAGE"
        if chatgpt_pages:
            selected_context, selected_page, selected_index = chatgpt_pages[0]
        elif ensure_if_missing:
            selected_context = contexts[0]
            try:
                selected_page = selected_context.new_page()
                selected_page.goto(CHATGPT_URL, wait_until="domcontentloaded")
                if bool(getattr(selected_page, "is_closed", lambda: False)()) or not is_allowed_chatgpt_url(
                    str(getattr(selected_page, "url", "") or "")
                ):
                    raise ValueError("new page URL is outside the allowlist")
            except Exception as exc:
                raise PlaywrightAttachedBrainError(
                    "Formal Browser could not ensure a ChatGPT page",
                    code="CHATGPT_PAGE_ENSURE_FAILED",
                    stage="ENSURE_CHATGPT_PAGE",
                    cause=exc,
                ) from exc
            try:
                selected_index = context_pages.index((selected_context, selected_page))
            except ValueError:
                selected_index = len(context_pages)
            chatgpt_pages.append((selected_context, selected_page, selected_index))
            action = "CREATED_NEW_PAGE"
        else:
            raise PlaywrightAttachedBrainError(
                "No allowlisted ChatGPT page exists in the formal Browser",
                code="CHATGPT_PAGE_NOT_FOUND",
                stage="T12_URL_ONLY_REBIND",
            )

        previous_page = getattr(self, "_bound_page_object", None)
        self.context = selected_context
        self.page = selected_page
        self._bound_page_object = selected_page
        self.bound_page_id = f"page-{selected_index}"
        self.page_candidates = []
        self.selected_candidate_index = selected_index
        self.auth_state = "UNKNOWN"
        self.state = PlaywrightAttachedBrainState.CONNECTED
        self._browser_disconnected = False
        self.diagnostics["browserConnected"] = True
        self.diagnostics["contextCount"] = len(contexts)
        self.diagnostics["pageCount"] = len(context_pages) + int(action == "CREATED_NEW_PAGE")
        self._capture_diagnostic_binding_baseline(selected_page, selected_context)
        if selected_page is not previous_page:
            self._wire_page(selected_page)

        return {
            "action": action,
            "browserConnected": True,
            "connectionMode": PLAYWRIGHT_FORMAL_CONNECTION,
            "attachMode": self.attach_mode,
            "endpointSource": self.endpoint_source,
            "formalCdpPort": self.diagnostics.get("formalCdpPort"),
            "formalEndpointFreshness": self.diagnostics.get("formalEndpointFreshness"),
            "formalTcpProbe": dict(self.diagnostics.get("formalTcpProbe") or {}),
            "pageAlive": True,
            "boundPageId": self.bound_page_id,
            "contextCount": len(contexts),
            "pageCount": len(context_pages) + int(action == "CREATED_NEW_PAGE"),
            "chatgptPageCount": len(chatgpt_pages),
            "selectedTargetHostname": "chatgpt.com",
            "authState": "UNKNOWN",
            "brainState": PlaywrightAttachedBrainState.CONNECTED,
            "runtimeIdentity": self.runtime_identity_snapshot(),
        }

    def _has_live_browser_transport(self) -> bool:
        if self.browser is None or self.playwright is None or self._browser_disconnected:
            return False
        is_connected = getattr(self.browser, "is_connected", None)
        if not callable(is_connected):
            return False
        try:
            return bool(is_connected())
        except Exception:
            return False

    def _resume_page_discovery_on_connected_browser(self, connect_attempt_id: str | None = None) -> None:
        """Reuse the existing Playwright/CDP transport after a page-resolution miss."""
        self._record_owner_thread("resume_page_discovery")
        self.connect_attempt_id = connect_attempt_id or str(uuid4())
        self.attempt_started_at = _utc_now()
        self.attempt_finished_at = None
        self.attempt_duration_ms = None
        self._stage_started_at = {}
        self._stage_started_clock = {}
        self.trace = []
        self.last_error = None
        self.last_error_details = None
        self.auth_state = "UNKNOWN"
        self._browser_disconnected = False
        self.state = PlaywrightAttachedBrainState.CONNECTED
        self.diagnostics["browserConnected"] = True
        self.diagnostics["browserTransportReused"] = True
        self._trace("T0_UI_CLICK", "RECEIVED")
        self._trace("T3_BRAIN_CONNECT_START", "REUSE_EXISTING_FORMAL_BROWSER")

        contexts = self._contexts()
        if not contexts:
            self._fail(PlaywrightAttachedBrainState.ERROR, "PLAYWRIGHT_CONTEXT_NOT_FOUND", stage="T7_CONTEXTS_RESULT")
            raise PlaywrightAttachedBrainError("No browser context found", code="PLAYWRIGHT_CONTEXT_NOT_FOUND", stage="T7_CONTEXTS_RESULT")
        self.diagnostics["contextCount"] = len(contexts)
        self.state = PlaywrightAttachedBrainState.DISCOVERING_CHATGPT
        self._trace("T11_CHATGPT_RESOLVER_START", "START")
        self.context, self.page = self._find_chatgpt_page()
        self.diagnostics["pageCount"] = sum(len(list(context.pages)) for context in contexts)
        self._trace("T10_PAGES_ENUM_RESULT", "PASS")
        self._mark_success("T10_PAGES_RESULT")
        if self.page is None or self.context is None:
            self._fail(PlaywrightAttachedBrainState.ERROR, "CHATGPT_PAGE_NOT_FOUND", stage="T12_CHATGPT_RESOLVER_RESULT")
            raise PlaywrightAttachedBrainError("ChatGPT page not found (CHATGPT_TARGET_NOT_FOUND)", code="CHATGPT_PAGE_NOT_FOUND", stage="T12_CHATGPT_RESOLVER_RESULT")
        self._trace("T12_CHATGPT_RESOLVER_RESULT", "PASS")
        self._wire_page(self.page)
        self.state = PlaywrightAttachedBrainState.CHECKING_COMPOSER
        self._trace("T13_COMPOSER_CHECK_START", "START")
        health = self.live_poc_health_check()
        self.auth_state = str(health.get("authState") or "UNKNOWN")
        failure_code = health.get("failureCode")
        if health.get("precheckResult") == "PASS":
            self.state = PlaywrightAttachedBrainState.READY
            self.last_error = None
            self._last_failure = None
            self._mark_success("T15_SUCCESS")
            self._trace("T15_SUCCESS", "PASS")
            self._finish_attempt()
            return
        code = str(failure_code or "DOM_UNKNOWN")
        stage = "T13_COMPOSER_CHECK" if code in {"COMPOSER_NOT_FOUND", "PAGE_LOADING"} else "T14_AUTH_STATE_RESULT"
        self._fail(PlaywrightAttachedBrainState.ERROR, code, stage=stage)
        raise PlaywrightAttachedBrainError(f"ChatGPT page is not ready ({code})", code=code, stage=stage)

    def _connect_unlocked(
        self,
        connect_attempt_id: str | None = None,
        *,
        url_only_rebind: bool = False,
        ensure_if_missing: bool = False,
    ) -> dict[str, Any] | None:
        if url_only_rebind and self._has_live_browser_transport():
            return self._url_only_rebind_connected_browser(ensure_if_missing=ensure_if_missing)
        if (
            self.state == PlaywrightAttachedBrainState.READY
            and self.browser is not None
            and self.context is not None
            and self.page is not None
            and self._has_live_browser_transport()
        ):
            return
        if self._has_live_browser_transport():
            return self._resume_page_discovery_on_connected_browser(connect_attempt_id)
        self.connect_attempt_id = connect_attempt_id or str(uuid4())
        self.attempt_started_at = _utc_now()
        self.attempt_finished_at = None
        self.attempt_duration_ms = None
        self._stage_started_at = {}
        self._stage_started_clock = {}
        self.trace = []
        self.last_error_details = None
        self.diagnostics.update({
            "cdpHttpPreflight": "NOT_RUN", "cdpHttpStatus": None,
            "jsonVersionShape": "UNKNOWN", "playwrightLoad": "NOT_RUN",
            "connectOverCdp": "NOT_RUN", "browserConnected": False,
            "lastSuccessfulStage": None,
            "lastSuccessStage": None, "lastFailureStage": None,
            "versions": self._version_diagnostics(),
            "channelSupport": chromium_channel_support(),
            "formalEffectiveConnectOptions": self._formal_connect_options(),
            "formalCdpPort": None,
            "formalEndpointFreshness": "NOT_READ",
            "formalTcpProbe": None,
            "formalConnectFailure": None,
        })
        self.state = PlaywrightAttachedBrainState.CONNECTING
        self._browser_disconnected = False
        self._trace("T0_UI_CLICK", "RECEIVED")
        try:
            self._trace("T1_IPC_REQUEST_SENT", "RECEIVED")
            self._trace("T2_IPC_HANDLER_ENTER", "RECEIVED")
            self._trace("T3_BRAIN_CONNECT_START", "START")
            self._assert_formal_configuration()
            try:
                self.playwright = self._load_playwright()
            except PlaywrightAttachedBrainError as exc:
                self.diagnostics["playwrightLoad"] = "FAIL"
                self._fail(PlaywrightAttachedBrainState.ERROR, exc.code, stage=exc.stage or "T4_PLAYWRIGHT_MODULE_READY", cause=exc.cause or exc)
                raise
            self.diagnostics["playwrightLoad"] = "PASS"
            self._trace("T4_PLAYWRIGHT_MODULE_READY", "PASS")
            if self.connection_mode == "EXACT_WS":
                self.diagnostics["cdpHttpPreflight"] = "NOT_APPLICABLE"
                self.diagnostics["jsonVersionShape"] = "DEVTOOLS_ACTIVE_PORT_EXACT_WS"
                self._trace("T5_CONNECT_OVER_CDP_START", "DEVTOOLS_ACTIVE_PORT_EXACT_WS")
            else:
                try:
                    preflight = self._preflight_cdp()
                    self.diagnostics["cdpHttpPreflight"] = "PASS"
                    self.diagnostics["cdpHttpStatus"] = preflight.get("status")
                    self.diagnostics["jsonVersionShape"] = preflight.get("shape", "VALID_WITH_WEBSOCKET_URL")
                    self._trace("T5_CONNECT_OVER_CDP_START", "PREFLIGHT_PASS")
                except PlaywrightAttachedBrainError as exc:
                    self.diagnostics["cdpHttpPreflight"] = "FAIL"
                    self._fail(PlaywrightAttachedBrainState.ERROR, exc.code, stage=exc.stage or "T5_CONNECT_OVER_CDP_START", cause=exc.cause or exc)
                    raise
            # Formal route: attach only. Do not call any browser-launch API
            # or create a Chrome process.
            try:
                self._trace("T5_CONNECT_OVER_CDP_START", "START")
                connect_over_cdp = self.playwright.chromium.connect_over_cdp
                endpoint = self.cdp_endpoint
                if self.connection_mode == "EXACT_WS":
                    read_started = time.monotonic()
                    try:
                        if self._injected_endpoint_resolver:
                            record = self.endpoint_resolver()
                            self.diagnostics["formalCdpPort"] = record.port
                            self.diagnostics["formalEndpointFreshness"] = "CURRENT"
                            tcp = {"result": "PASS"} if self._injected_playwright else tcp_connect_probe(record, timeout=2.0)
                        elif self._injected_playwright:
                            # Test-only Playwright injection must not make a
                            # real loopback connection.  The production path
                            # below always resolves the live file and probes
                            # the real endpoint.
                            record = DevToolsActivePortRecord(
                                path=Path("/test/DevToolsActivePort"),
                                port=1,
                                ws_path="/devtools/browser/TEST",
                            )
                            tcp = {"result": "PASS"}
                        else:
                            # DedicatedChromeEndpointResolver.resolve() is
                            # the formal equivalent of read_devtools_active_port
                            # and rereads the current file on every attach.
                            record = self.endpoint_resolver()
                            self.diagnostics["formalCdpPort"] = record.port
                            self.diagnostics["formalEndpointFreshness"] = "CURRENT"
                            tcp = tcp_connect_probe(record, timeout=2.0)
                    except Exception as exc:
                        self._record_formal_failure(
                            stage="READ_DEVTOOLS_ACTIVE_PORT",
                            error=exc,
                            started=read_started,
                        )
                        raise PlaywrightAttachedBrainError(
                            "Dedicated Chrome DevToolsActivePort is unavailable",
                            code="DEDICATED_CDP_ENDPOINT_UNAVAILABLE",
                            stage="READ_DEVTOOLS_ACTIVE_PORT",
                            cause=exc,
                        ) from exc
                    self.diagnostics["formalTcpProbe"] = {
                        "result": tcp.get("result"),
                        "errno": tcp.get("errnoName"),
                        "syscall": tcp.get("syscall", "connect"),
                        "durationMs": tcp.get("durationMs"),
                    }
                    if tcp.get("result") != "PASS":
                        tcp_error = RuntimeError(f"Dedicated Chrome CDP TCP probe {tcp.get('result', 'FAILED')}")
                        self._record_formal_failure(
                            stage="CONNECT_OVER_CDP",
                            error=tcp_error,
                            started=read_started,
                        )
                        raise PlaywrightAttachedBrainError(
                            "Dedicated Chrome CDP TCP endpoint is unavailable",
                            code=f"DEDICATED_CDP_TCP_{tcp.get('result', 'FAILED')}",
                            stage="CONNECT_OVER_CDP",
                        )
                    endpoint = record.endpoint
                    self._trace("BUILD_EXACT_WS", "PASS")
                supports_timeout = False
                supports_keyword_args = False
                parameter_names: set[str] = set()
                try:
                    parameters = inspect.signature(connect_over_cdp).parameters.values()
                    parameter_names = {parameter.name for parameter in parameters}
                    supports_timeout = any(
                        parameter.name == "timeout" or parameter.kind == inspect.Parameter.VAR_KEYWORD
                        for parameter in parameters
                    )
                    supports_keyword_args = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters)
                except (TypeError, ValueError):
                    supports_timeout = False
                    supports_keyword_args = False
                kwargs: dict[str, Any] = {}
                if supports_timeout:
                    kwargs["timeout"] = (
                        FORMAL_EXACT_CONNECT_TIMEOUT_MS
                        if self.connection_mode == "EXACT_WS"
                        else self.connect_timeout_ms
                    )
                if self.connection_mode == "EXACT_WS":
                    # Playwright's real sync API exposes these as named
                    # keyword parameters rather than **kwargs.  They are
                    # required for the exact browser endpoint path used by
                    # R26; only older injected shims may expose **kwargs.
                    if "is_local" in parameter_names or supports_keyword_args:
                        kwargs["is_local"] = True
                    if "no_defaults" in parameter_names or supports_keyword_args:
                        kwargs["no_defaults"] = True
                self.diagnostics["formalEffectiveConnectOptions"] = {
                    **self._formal_connect_options(),
                    "timeoutMs": kwargs.get("timeout", self.connect_timeout_ms),
                    "isLocal": kwargs.get("is_local", "NOT_PASSED"),
                    "noDefaults": kwargs.get("no_defaults", "NOT_PASSED"),
                }
                connect_started = time.monotonic()
                # Compatibility with older injected Playwright shims; the
                # real Playwright API supports the exact endpoint options.
                self.browser = connect_over_cdp(endpoint, **kwargs)
            except Exception as exc:
                self.diagnostics["connectOverCdp"] = "FAIL"
                message = str(exc)
                if isinstance(exc, TimeoutError) or "timeout" in f"{type(exc).__name__} {message}".lower():
                    code = "CHROME_RUNTIME_UNAVAILABLE"
                elif self.connection_mode == "EXACT_WS":
                    code = "PLAYWRIGHT_EXACT_WS_CONNECT_FAILED"
                else:
                    code = "PLAYWRIGHT_CONNECT_OVER_CDP_FAILED"
                failure_stage = getattr(exc, "stage", None) or "T5_CONNECT_OVER_CDP_START"
                wrapped = PlaywrightAttachedBrainError(
                    "Playwright channel/CDP connection failed",
                    code=code,
                    stage=failure_stage,
                    cause=exc,
                )
                self._record_formal_failure(
                    stage=self._formal_failure_stage(failure_stage),
                    error=exc,
                    started=locals().get("connect_started", time.monotonic()),
                    stack_id=wrapped.stack_id,
                )
                self._fail(PlaywrightAttachedBrainState.ERROR, code, stage=failure_stage, cause=exc)
                raise wrapped from exc
            try:
                connected = getattr(self.browser, "is_connected", None)
                if callable(connected) and not bool(connected()):
                    raise RuntimeError("Browser reported disconnected")
            except Exception as exc:
                self.diagnostics["connectOverCdp"] = "FAIL"
                self._fail(PlaywrightAttachedBrainState.ERROR, "PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T6_CONNECT_OVER_CDP_RESULT", cause=exc)
                raise PlaywrightAttachedBrainError("Browser is disconnected", code="PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T6_CONNECT_OVER_CDP_RESULT", cause=exc) from exc
            self.diagnostics["connectOverCdp"] = "PASS"
            self.diagnostics["browserConnected"] = True
            self.diagnostics["browserTransportReused"] = False
            self.brain_connection_id = f"chrome-{uuid4()}"
            self._trace("T6_CONNECT_OVER_CDP_RESULT", "PASS")
            self._mark_success("T6_CONNECT_OVER_CDP_SUCCESS")
            self._wire_browser()
            contexts = self._contexts()
            if not contexts:
                self._fail(PlaywrightAttachedBrainState.ERROR, "PLAYWRIGHT_CONTEXT_NOT_FOUND", stage="T7_CONTEXTS_RESULT")
                raise PlaywrightAttachedBrainError("No browser context found", code="PLAYWRIGHT_CONTEXT_NOT_FOUND", stage="T7_CONTEXTS_RESULT")
            self.diagnostics["contextCount"] = len(contexts)
            self._trace("T8_CONTEXTS_RESULT", "PASS")
            self._mark_success("T8_CONTEXTS_RESULT")
            self.state = PlaywrightAttachedBrainState.CONNECTED
            if url_only_rebind:
                self._trace("T12_URL_ONLY_REBIND", "START")
                result = self._bind_chatgpt_page_by_url(contexts, ensure_if_missing=ensure_if_missing)
                self._trace("T12_URL_ONLY_REBIND", "PASS")
                self._mark_success("T12_URL_ONLY_REBIND")
                self._finish_attempt()
                return result
            self.state = PlaywrightAttachedBrainState.DISCOVERING_CHATGPT
            self._trace("T11_CHATGPT_RESOLVER_START", "START")
            self.context, self.page = self._find_chatgpt_page()
            self.diagnostics["pageCount"] = sum(len(list(context.pages)) for context in contexts)
            self._trace("T10_PAGES_ENUM_RESULT", "PASS")
            self._mark_success("T10_PAGES_ENUM_RESULT")
            if self.page is None or self.context is None:
                self._fail(PlaywrightAttachedBrainState.ERROR, "CHATGPT_PAGE_NOT_FOUND", stage="T12_CHATGPT_RESOLVER_RESULT")
                raise PlaywrightAttachedBrainError("ChatGPT page not found (CHATGPT_TARGET_NOT_FOUND)", code="CHATGPT_PAGE_NOT_FOUND", stage="T12_CHATGPT_RESOLVER_RESULT")
            self._trace("T12_CHATGPT_RESOLVER_RESULT", "PASS")
            self._mark_success("T12_CHATGPT_PAGE_RESOLVE_RESULT")
            self._wire_page(self.page)
            self.state = PlaywrightAttachedBrainState.CHECKING_COMPOSER
            self._trace("T13_COMPOSER_CHECK_START", "START")
            selected_diagnostic = next(
                (item for item in self.page_candidates if item.get("selectedCandidate")), None
            )
            if selected_diagnostic is None:
                self._fail(PlaywrightAttachedBrainState.ERROR, "DOM_UNKNOWN", stage="T14_AUTH_STATE_RESULT")
                raise PlaywrightAttachedBrainError("ChatGPT page fingerprint unavailable", code="DOM_UNKNOWN", stage="T14_AUTH_STATE_RESULT")
            auth_state = str(selected_diagnostic.get("authState") or "DOM_UNKNOWN")
            failure_code = selected_diagnostic.get("failureCode")
            self.auth_state = auth_state
            if failure_code is None:
                self.state = PlaywrightAttachedBrainState.READY
                self.last_error = None
                self._last_failure = None
                self._mark_success("T15_SUCCESS")
                self._trace("T15_SUCCESS", "PASS")
                self._finish_attempt()
                return
            if failure_code == "AUTH_REQUIRED":
                self._fail(PlaywrightAttachedBrainState.ERROR, "AUTH_REQUIRED", stage="T14_AUTH_STATE_RESULT")
                raise PlaywrightAttachedBrainError("Authentication required", code="AUTH_REQUIRED", stage="T14_AUTH_STATE_RESULT")
            # A missing Composer is a distinct authenticated-page failure.  A
            # loading or unknown page is likewise never relabelled as login.
            stage = "T13_COMPOSER_CHECK" if failure_code in {"COMPOSER_NOT_FOUND", "PAGE_LOADING"} else "T14_AUTH_STATE_RESULT"
            self._fail(PlaywrightAttachedBrainState.ERROR, str(failure_code), stage=stage)
            raise PlaywrightAttachedBrainError(
                f"ChatGPT page is not ready ({failure_code})", code=str(failure_code), stage=stage
            )
        except PlaywrightAttachedBrainError:
            raise
        except Exception as exc:
            self._fail(PlaywrightAttachedBrainState.ERROR, "INTERNAL_UNCLASSIFIED_EXCEPTION", stage="T15_FAIL", cause=exc)
            raise PlaywrightAttachedBrainError("Unclassified connection failure", code="INTERNAL_UNCLASSIFIED_EXCEPTION", stage="T15_FAIL", cause=exc) from exc

    def _finish_attempt(self) -> None:
        if self.attempt_started_at is None or self.attempt_finished_at is not None:
            return
        self.attempt_finished_at = _utc_now()
        started = self._stage_started_clock.get("T0_UI_CLICK")
        if started is not None:
            self.attempt_duration_ms = max(0, round((time.monotonic() - started) * 1000))

    def _ensure_target(self) -> Any:
        self._record_owner_thread("target_access")
        if self.browser is None or self.context is None or self.page is None or self._browser_disconnected:
            self._fail(PlaywrightAttachedBrainState.BROWSER_LOST, "PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T7_CONTEXTS_START")
            raise PlaywrightAttachedBrainError("Browser is unavailable", code="PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T7_CONTEXTS_START")
        try:
            if bool(getattr(self.page, "is_closed", lambda: False)()) or self.page not in list(self.context.pages):
                self._fail(PlaywrightAttachedBrainState.PAGE_LOST, "CHATGPT_PAGE_NOT_FOUND", stage="T10_PAGES_RESULT")
                raise PlaywrightAttachedBrainError("ChatGPT page is closed", code="CHATGPT_PAGE_NOT_FOUND", stage="T10_PAGES_RESULT")
            if not is_allowed_chatgpt_url(str(self.page.url or "")):
                self._fail(PlaywrightAttachedBrainState.PAGE_LOST, "CHATGPT_PAGE_URL_INVALID", stage="T10_PAGES_RESULT")
                raise PlaywrightAttachedBrainError("ChatGPT page URL is invalid", code="CHATGPT_PAGE_URL_INVALID", stage="T10_PAGES_RESULT")
            return self.page
        except PlaywrightAttachedBrainError:
            raise
        except Exception as exc:
            self._fail(PlaywrightAttachedBrainState.BROWSER_LOST, "PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T7_CONTEXTS_START", cause=exc)
            raise PlaywrightAttachedBrainError("Browser is unavailable", code="PLAYWRIGHT_BROWSER_DISCONNECTED", stage="T7_CONTEXTS_START", cause=exc) from exc

    def get_page(self) -> Any:
        return self._ensure_target()

    def getPage(self) -> Any:
        return self.get_page()

    def bring_bound_page_to_front(self) -> dict[str, Any]:
        """Foreground only the Page already bound to this formal Host.

        This operation deliberately does not inspect, resolve, validate, or
        replace the Page.  The owner-thread check is required because the
        formal Browser uses Playwright's synchronous API.
        """
        operation_thread_id = self._record_owner_thread("bring_bound_page_to_front")
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        # Playwright's SyncBase restores its own dispatcher loop in asyncio's
        # thread-local slot after each sync call. That loop is required by the
        # sync facade and is not an application asyncio caller. Reject only a
        # different running loop; this also fails closed if no Playwright
        # runtime is attached to the Host.
        playwright_loop = getattr(self.playwright, "_loop", None)
        if running_loop is not None and running_loop is not playwright_loop:
            return {
                "ok": False,
                "code": "PLAYWRIGHT_SYNC_API_INSIDE_ASYNCIO_LOOP",
                "boundPageId": self.bound_page_id,
                "ownerThreadId": self.owner_thread_id,
                "operationThreadId": operation_thread_id,
                "pageReadCount": 0,
                "domReadCount": 0,
                "textReadCount": 0,
            }
        page = self.page
        page_id = self.bound_page_id
        if page is None or page_id is None:
            return {
                "ok": False,
                "code": "NO_BOUND_PAGE",
                "boundPageId": None,
                "ownerThreadId": self.owner_thread_id,
                "operationThreadId": operation_thread_id,
                "pageReadCount": 0,
                "domReadCount": 0,
                "textReadCount": 0,
            }
        try:
            page.bring_to_front()
        except Exception:
            # Do not surface Playwright exception text, which may carry page
            # metadata.  The caller only needs a stable failure code.
            return {
                "ok": False,
                "code": "BOUND_PAGE_FOREGROUND_FAILED",
                "boundPageId": page_id,
                "ownerThreadId": self.owner_thread_id,
                "operationThreadId": operation_thread_id,
                "pageReadCount": 0,
                "domReadCount": 0,
                "textReadCount": 0,
            }
        return {
            "ok": True,
            "code": "BOUND_PAGE_FOREGROUNDED",
            "boundPageId": page_id,
            "ownerThreadId": self.owner_thread_id,
            "operationThreadId": operation_thread_id,
            "pageReadCount": 0,
            "domReadCount": 0,
            "textReadCount": 0,
        }

    def ensure_chatgpt_page(self) -> dict[str, Any]:
        """Bind or create only the formal ChatGPT page in this attached Browser."""
        self._record_owner_thread("ensure_chatgpt_page")
        if not self._ensure_chatgpt_page_lock.acquire(blocking=False):
            raise PlaywrightAttachedBrainError(
                "ChatGPT page ensure is already in progress",
                code="ENSURE_CHATGPT_PAGE_IN_PROGRESS",
                stage="ENSURE_CHATGPT_PAGE",
            )
        try:
            if not self._has_live_browser_transport():
                raise PlaywrightAttachedBrainError(
                    "The formal Browser transport is not connected",
                    code="PLAYWRIGHT_BROWSER_DISCONNECTED",
                    stage="ENSURE_CHATGPT_PAGE",
                )
            contexts = self._contexts()
            if not contexts:
                raise PlaywrightAttachedBrainError(
                    "No formal Browser context is available",
                    code="PLAYWRIGHT_CONTEXT_NOT_FOUND",
                    stage="ENSURE_CHATGPT_PAGE",
                )

            previously_bound_page = self.page
            selected_context, selected_page = self._find_chatgpt_page()
            if selected_context is not None and selected_page is not None:
                action = "REUSED_EXISTING_PAGE"
            else:
                context = self.context if self.context in contexts else contexts[0]
                try:
                    page = context.new_page()
                    page.goto(CHATGPT_URL, wait_until="domcontentloaded")
                except Exception as exc:
                    raise PlaywrightAttachedBrainError(
                        "The formal Browser could not open ChatGPT",
                        code="CHATGPT_PAGE_ENSURE_FAILED",
                        stage="ENSURE_CHATGPT_PAGE",
                        cause=exc,
                    ) from exc
                if not is_allowed_chatgpt_url(str(getattr(page, "url", "") or "")):
                    raise PlaywrightAttachedBrainError(
                        "The opened page is outside the formal ChatGPT host allowlist",
                        code="CHATGPT_PAGE_TARGET_NOT_ALLOWED",
                        stage="ENSURE_CHATGPT_PAGE",
                    )
                selected_context, selected_page = self._find_chatgpt_page()
                if selected_context is None or selected_page is None:
                    raise PlaywrightAttachedBrainError(
                        "The opened ChatGPT page could not be resolved",
                        code="CHATGPT_PAGE_NOT_FOUND",
                        stage="ENSURE_CHATGPT_PAGE",
                    )
                action = "CREATED_NEW_PAGE"

            self.context = selected_context
            self.page = selected_page
            if selected_page is not previously_bound_page:
                self._wire_page(selected_page)
            try:
                selected_page.bring_to_front()
            except Exception as exc:
                raise PlaywrightAttachedBrainError(
                    "The formal ChatGPT page could not be brought to the foreground",
                    code="CHATGPT_PAGE_FOREGROUND_FAILED",
                    stage="ENSURE_CHATGPT_PAGE",
                    cause=exc,
                ) from exc
            self.state = PlaywrightAttachedBrainState.CONNECTED
            self.auth_state = "UNKNOWN"
            self.diagnostics["browserConnected"] = True
            all_pages = [page for context in contexts for page in list(context.pages)]
            return {
                "action": action,
                "browserConnected": True,
                "pageAlive": not bool(getattr(selected_page, "is_closed", lambda: False)()),
                "boundPageId": self.bound_page_id,
                "pageForegrounded": True,
                "contextCount": len(contexts),
                "pageCount": len(all_pages),
                "chatgptPageCount": sum(1 for page in all_pages if self.page_resolver.eligible(page)),
                "selectedTargetHostname": "chatgpt.com",
                "runtimeIdentity": self.runtime_identity_snapshot(),
            }
        finally:
            self._ensure_chatgpt_page_lock.release()

    def openBrain(self, connect_attempt_id: str | None = None) -> dict[str, Any]:
        self._record_owner_thread("open_brain")
        self.start(connect_attempt_id=connect_attempt_id)
        if self.page is not None:
            try:
                self.page.bring_to_front()
            except Exception:
                pass
        return self.status()

    def open(self) -> dict[str, Any]:
        return self.openBrain()

    def start(self, *, connect_attempt_id: str | None = None) -> None:
        self.connect(connect_attempt_id=connect_attempt_id)

    def connect_if_necessary(self, *, connect_attempt_id: str | None = None) -> dict[str, Any]:
        """Reuse the formal runtime or connect it, without creating a replacement."""
        self._record_owner_thread("atomic_connect")
        has_runtime = (
            self.state == PlaywrightAttachedBrainState.READY
            and self.auth_state == "AUTHENTICATED"
            and self.browser is not None
            and not self._browser_disconnected
            and self.context is not None
            and self.page is not None
            and self.bound_page_id is not None
        )
        if has_runtime:
            health = self.live_poc_health_check()
            if health.get("precheckResult") == "PASS" and health.get("boundPageId") == self.bound_page_id:
                return {"connected": True, "reused": True, "health": health}
            raise PlaywrightAttachedBrainError(
                "The READY formal runtime failed its liveness check",
                code="CHROME_RUNTIME_UNAVAILABLE",
                stage="ATOMIC_CONNECT_PRECONDITION",
            )
        if self.state != PlaywrightAttachedBrainState.DISCONNECTED:
            raise PlaywrightAttachedBrainError(
                "The formal runtime is not safely reconnectable",
                code="ATOMIC_CONNECT_PRECONDITION_FAILED",
                stage="ATOMIC_CONNECT_PRECONDITION",
            )
        self.connect(connect_attempt_id=connect_attempt_id)
        health = self.live_poc_health_check()
        if health.get("precheckResult") != "PASS":
            raise PlaywrightAttachedBrainError(
                "The newly connected formal runtime is not ready",
                code="CHROME_RUNTIME_UNAVAILABLE",
                stage="ATOMIC_CONNECT_HEALTH_CHECK",
            )
        return {"connected": True, "reused": False, "health": health}

    def checkHealth(self) -> bool:
        self._record_owner_thread("health_check")
        try:
            return self.auth_status() == "AUTHENTICATED" and self.state == PlaywrightAttachedBrainState.READY
        except Exception:
            return False

    def live_poc_health_check(self) -> dict[str, Any]:
        """Check the current bound target without reconnecting or re-resolving."""
        self._record_owner_thread("poc_precheck")
        result: dict[str, Any] = {
            "browserConnected": bool(self.browser is not None and not self._browser_disconnected),
            "pageAlive": False,
            "hostname": None,
            "authState": "UNKNOWN",
            "composerReady": False,
            "precheckResult": "FAIL",
            "failureCode": None,
            "registryInstanceId": self.registry_instance_id,
            "hostInstanceId": self.host_instance_id,
            "processPid": self.process_pid,
            "parentPid": self.parent_pid,
            "runtimeOwner": self.runtime_owner,
            "formalRuntimeType": "PLAYWRIGHT_ATTACHED_REAL_CHROME",
            "boundPageId": self.bound_page_id,
        }
        if not result["browserConnected"] or self.page is None or self.context is None:
            result["failureCode"] = "BROWSER_LOST" if not result["browserConnected"] else "PAGE_LOST"
            if not getattr(self, "_closing", False):
                self._runtime_lifecycle_event_once(
                    "RUNTIME_LOSS_EVENT",
                    "BROWSER_DISCONNECTED" if not result["browserConnected"] else "BOUND_PAGE_CLOSED",
                    caller="PlaywrightAttachedBrainHost.live_poc_health_check",
                )
            return result
        try:
            if bool(getattr(self.page, "is_closed", lambda: False)()) or self.page not in list(self.context.pages):
                result["failureCode"] = "PAGE_LOST"
                if not getattr(self, "_closing", False):
                    self._runtime_lifecycle_event_once(
                        "RUNTIME_LOSS_EVENT",
                        "BOUND_PAGE_CLOSED",
                        caller="PlaywrightAttachedBrainHost.live_poc_health_check",
                    )
                return result
            from urllib.parse import urlsplit

            parsed = urlsplit(str(self.page.url or ""))
            result["hostname"] = parsed.hostname
            if not is_allowed_chatgpt_url(str(self.page.url or "")):
                result["failureCode"] = "PAGE_LOST"
                if not getattr(self, "_closing", False):
                    self._runtime_lifecycle_event_once(
                        "RUNTIME_LOSS_EVENT",
                        "BOUND_PAGE_NAVIGATED_AWAY",
                        caller="PlaywrightAttachedBrainHost.live_poc_health_check",
                    )
                return result
            result["pageAlive"] = True
            fingerprint = ChatGPTPageFingerprint.from_mapping(self._page_fingerprint(self.page, self.selected_candidate_index or 0))
            scored = ChatGPTPageScorer.score(fingerprint)
            result["authState"] = scored["authState"]
            result["composerReady"] = bool(scored["ready"])
            result["failureCode"] = scored["failureCode"]
            if scored["ready"]:
                result["precheckResult"] = "PASS"
                result["failureCode"] = None
            return result
        except Exception:
            result["failureCode"] = "DOM_UNKNOWN"
            if not getattr(self, "_closing", False):
                self._runtime_lifecycle_event_once(
                    "RUNTIME_HEALTH_CHECK",
                    "HEALTH_CHECK_FALSE_POSITIVE",
                    caller="PlaywrightAttachedBrainHost.live_poc_health_check",
                )
            return result

    def _dev_probe_page_check(self, page: Any, page_id: str | None, stage: str) -> None:
        """Keep a development probe on the exact page selected at its start."""
        self._record_owner_thread(f"dev_probe_{stage}")
        if self.page is not page or self.bound_page_id != page_id:
            raise PlaywrightAttachedBrainError(
                "DEV_PROBE_BOUND_PAGE_CHANGED",
                code="DEV_PROBE_BOUND_PAGE_CHANGED",
                stage=f"DEV_PROBE_{stage.upper()}",
            )
        try:
            if bool(getattr(page, "is_closed", lambda: False)()) or page not in list(self.context.pages):
                raise PlaywrightAttachedBrainError(
                    "DEV_PROBE_PAGE_LOST",
                    code="DEV_PROBE_PAGE_LOST",
                    stage=f"DEV_PROBE_{stage.upper()}",
                )
            if not is_allowed_chatgpt_url(str(page.url or "")):
                raise PlaywrightAttachedBrainError(
                    "DEV_PROBE_PAGE_URL_INVALID",
                    code="DEV_PROBE_PAGE_URL_INVALID",
                    stage=f"DEV_PROBE_{stage.upper()}",
                )
        except PlaywrightAttachedBrainError:
            raise
        except Exception as exc:
            raise PlaywrightAttachedBrainError(
                "DEV_PROBE_PAGE_LOST",
                code="DEV_PROBE_PAGE_LOST",
                stage=f"DEV_PROBE_{stage.upper()}",
                cause=exc,
            ) from exc

    @staticmethod
    def _dev_probe_user_turn_count(page: Any) -> int:
        return int(page.locator('[data-message-author-role="user"]').count())

    @staticmethod
    def _dev_probe_error_message(error: BaseException, probe_text: str) -> str:
        return _safe_text(str(error).replace(probe_text, "[PROBE_REDACTED]"), 1000)

    @classmethod
    def _dev_probe_attempts(cls, attempts: list[dict[str, Any]], probe_text: str) -> list[dict[str, Any]]:
        safe: list[dict[str, Any]] = []
        for attempt in attempts:
            item = {
                "operation": str(attempt.get("operation") or "UNKNOWN"),
                "result": str(attempt.get("result") or "UNKNOWN"),
            }
            error = attempt.get("error")
            if isinstance(error, BaseException):
                stack = "".join(traceback.format_exception(error)).replace(probe_text, "[PROBE_REDACTED]")
                stack = _safe_text(stack, 12000)
                item.update({
                    "errorName": type(error).__name__,
                    "errorMessage": cls._dev_probe_error_message(error, probe_text),
                    "stackId": _stack_id(stack),
                })
            safe.append(item)
        return safe

    @classmethod
    def _dev_probe_fill_error(cls, error: ComposerWriteDiagnosticError, probe_text: str) -> dict[str, Any] | None:
        if error.fill_error is None:
            return None
        raw_stack = "".join(traceback.format_exception(error.fill_error)).replace(probe_text, "[PROBE_REDACTED]")
        raw_stack = _safe_text(raw_stack, 12000)
        return {
            "name": type(error.fill_error).__name__,
            "message": cls._dev_probe_error_message(error.fill_error, probe_text),
            "operation": error.failed_operation or "locator.fill",
            "stackId": _stack_id(raw_stack),
            "elementKind": error.target.element_kind,
            "targetId": error.target.target_id,
        }

    @classmethod
    def _dev_probe_raw_fill_error(cls, target: Any, error: BaseException | None, operation: str, probe_text: str) -> dict[str, Any] | None:
        if error is None:
            return None
        raw_stack = "".join(traceback.format_exception(error)).replace(probe_text, "[PROBE_REDACTED]")
        raw_stack = _safe_text(raw_stack, 12000)
        return {
            "name": type(error).__name__,
            "message": cls._dev_probe_error_message(error, probe_text),
            "operation": operation,
            "stackId": _stack_id(raw_stack),
            "elementKind": target.element_kind,
            "targetId": target.target_id,
        }

    def run_dev_composer_write_probe(self, probe_text: str, *, allow_send: bool = False) -> dict[str, Any]:
        """Run a localhost-only write/verify/clear probe without any send path."""
        with self._dev_probe_lock_context():
            owner_thread_id = self._record_owner_thread("dev_probe_start")
            result: dict[str, Any] = {
                "ok": False,
                "probeState": "NOT_RUN",
                "allowSend": bool(allow_send),
                "connectOverCdp": "NOT_USED",
                "runtimeOwner": self.runtime_owner,
                "ownerThreadId": owner_thread_id,
                "boundPageId": self.bound_page_id,
                "threadIds": {},
                "candidateDiagnostics": [],
                "domShape": None,
                "targetId": None,
                "strategy": None,
                "elementKind": None,
                "fillError": None,
                "writeAttempts": [],
                "clearAttempts": [],
                "write": "NOT_RUN",
                "verify": "NOT_RUN",
                "clear": "NOT_RUN",
                "clearVerify": "NOT_RUN",
                "userTurnCountBefore": None,
                "userTurnCountAfter": None,
                "accidentalSend": False,
                "firstFailureCode": None,
                "authStateAfter": self.auth_state,
                "brainStateAfter": self.state,
                "composerReadyAfter": False,
            }

            def fail(code: str, *, stage: str, error: BaseException | None = None) -> None:
                if result["firstFailureCode"] is None:
                    result["firstFailureCode"] = code
                result["failureCode"] = result.get("firstFailureCode")
                result["failureStage"] = stage
                if error is not None:
                    result["failureName"] = type(error).__name__
                    result["failureMessage"] = self._dev_probe_error_message(error, probe_text)

            if allow_send:
                fail("DEV_PROBE_SEND_FORBIDDEN", stage="DEV_PROBE_PRECONDITION")
                result["probeState"] = "FAILED"
                return result

            ready = (
                self.state == PlaywrightAttachedBrainState.READY
                and self.auth_state == "AUTHENTICATED"
                and self.browser is not None
                and not self._browser_disconnected
                and self.context is not None
                and self.page is not None
                and self.bound_page_id is not None
            )
            if not ready:
                fail("DEV_COMPOSER_PROBE_PRECONDITION_FAILED", stage="DEV_PROBE_PRECONDITION")
                result["probeState"] = "FAILED"
                return result

            page = self.page
            page_id = self.bound_page_id
            write_attempted = False
            clear_attempted = False
            target = None
            try:
                self._dev_probe_page_check(page, page_id, "resolve")
                result["threadIds"]["resolve"] = self._record_owner_thread("dev_probe_resolve")
                target = ChatGPTPlaywrightDomAdapter.resolve_composer(page)
                if target is None:
                    fail("COMPOSER_NOT_FOUND", stage="DEV_PROBE_RESOLVE")
                else:
                    result["targetId"] = target.target_id
                    result["strategy"] = target.strategy
                    result["elementKind"] = target.element_kind
                    result["candidateDiagnostics"] = [dict(item) for item in target.candidate_diagnostics]
                    result["threadIds"]["inspect"] = self._record_owner_thread("dev_probe_inspect")
                    metadata = dict(target.metadata)
                    result["domShape"] = {
                        "targetId": target.target_id,
                        "strategy": target.strategy,
                        "locatorCount": target.locator_count,
                        "tagName": metadata.get("tagName"),
                        "role": metadata.get("role"),
                        "contenteditable": metadata.get("contenteditable"),
                        "data-testid": metadata.get("dataTestId"),
                        "aria-label": metadata.get("ariaLabel"),
                        "placeholder": metadata.get("placeholder"),
                        "isVisible": target.visible,
                        "isEnabled": target.enabled,
                        "isEditable": target.editable,
                        "boundingBoxExists": bool(metadata.get("boundingBoxExists")),
                        "ownerThreadId": owner_thread_id,
                        "classTokens": list(metadata.get("classTokens") or []),
                    }
                    if target.locator_count != 1:
                        fail("COMPOSER_AMBIGUOUS", stage="DEV_PROBE_INSPECT")
                    elif not target.visible:
                        fail("COMPOSER_NOT_VISIBLE", stage="DEV_PROBE_INSPECT")
                    elif not target.enabled or not target.editable:
                        fail("COMPOSER_NOT_EDITABLE", stage="DEV_PROBE_INSPECT")
                    elif ChatGPTPlaywrightDomAdapter.composer_text(target):
                        fail("COMPOSER_BUSY", stage="DEV_PROBE_INSPECT")
                    else:
                        result["userTurnCountBefore"] = self._dev_probe_user_turn_count(page)
                        self._dev_probe_page_check(page, page_id, "write")
                        result["threadIds"]["write"] = self._record_owner_thread("dev_probe_write")
                        write_attempted = True
                        try:
                            write_result = ChatGPTPlaywrightDomAdapter.write_composer_diagnostic(target, probe_text)
                            result["write"] = "PASS"
                            result["writeAttempts"] = self._dev_probe_attempts(write_result.get("attempts", []), probe_text)
                            result["fillError"] = self._dev_probe_raw_fill_error(
                                target, write_result.get("fillError"), "locator.fill", probe_text,
                            )
                        except ComposerWriteDiagnosticError as exc:
                            fail(exc.code, stage="DEV_PROBE_WRITE", error=exc)
                            result["write"] = "FAIL"
                            result["writeAttempts"] = self._dev_probe_attempts(exc.attempts, probe_text)
                            result["fillError"] = self._dev_probe_fill_error(exc, probe_text)
                        if result["write"] == "PASS":
                            self._dev_probe_page_check(page, page_id, "verify")
                            result["threadIds"]["verify"] = self._record_owner_thread("dev_probe_verify")
                            result["verify"] = "PASS" if ChatGPTPlaywrightDomAdapter.composer_text(target) == probe_text.replace("\r\n", "\n").replace("\r", "\n") else "FAIL"
                            if result["verify"] != "PASS":
                                fail("COMPOSER_WRITE_VERIFY_FAILED", stage="DEV_PROBE_VERIFY")

                        # A write may have partially reached the page even when
                        # Playwright reported an error. Always make one best-
                        # effort clear attempt, but never press Enter or click
                        # a submit control.
                        self._dev_probe_page_check(page, page_id, "clear")
                        result["threadIds"]["clear"] = self._record_owner_thread("dev_probe_clear")
                        clear_attempted = True
                        try:
                            clear_result = ChatGPTPlaywrightDomAdapter.write_composer_diagnostic(target, "")
                            result["clear"] = "PASS"
                            result["clearAttempts"] = self._dev_probe_attempts(clear_result.get("attempts", []), probe_text)
                        except ComposerWriteDiagnosticError as exc:
                            fail(exc.code, stage="DEV_PROBE_CLEAR", error=exc)
                            result["clear"] = "FAIL"
                            result["clearAttempts"] = self._dev_probe_attempts(exc.attempts, probe_text)
                            if result["fillError"] is None:
                                result["fillError"] = self._dev_probe_fill_error(exc, probe_text)
                        if result["clear"] == "PASS":
                            self._dev_probe_page_check(page, page_id, "clear_verify")
                            result["threadIds"]["clear_verify"] = self._record_owner_thread("dev_probe_clear_verify")
                            result["clearVerify"] = "PASS" if ChatGPTPlaywrightDomAdapter.composer_text(target) == "" else "FAIL"
                            if result["clearVerify"] != "PASS":
                                fail("COMPOSER_WRITE_VERIFY_FAILED", stage="DEV_PROBE_CLEAR_VERIFY")
                        result["userTurnCountAfter"] = self._dev_probe_user_turn_count(page)
                        if result["userTurnCountAfter"] != result["userTurnCountBefore"]:
                            result["accidentalSend"] = True
                            fail("DEV_PROBE_ACCIDENTAL_SEND_DETECTED", stage="DEV_PROBE_USER_TURN_CHECK")
            except PlaywrightAttachedBrainError as exc:
                fail(exc.code, stage=exc.stage or "DEV_PROBE", error=exc)
                result["probeState"] = "FAILED"
            except Exception as exc:
                fail("DEV_PROBE_FAILED", stage="DEV_PROBE", error=exc)
                result["probeState"] = "FAILED"
            finally:
                # If an unexpected exception occurred after a write attempt,
                # make the same-page clear attempt before returning. This path
                # also remains send-free.
                if write_attempted and not clear_attempted and target is not None:
                    try:
                        self._dev_probe_page_check(page, page_id, "clear")
                        result["threadIds"]["clear"] = self._record_owner_thread("dev_probe_clear")
                        clear_attempted = True
                        clear_result = ChatGPTPlaywrightDomAdapter.write_composer_diagnostic(target, "")
                        result["clear"] = "PASS"
                        result["clearAttempts"] = self._dev_probe_attempts(clear_result.get("attempts", []), probe_text)
                    except Exception as exc:
                        fail("DEV_PROBE_CLEAR_FAILED", stage="DEV_PROBE_CLEAR", error=exc)
                        result["clear"] = "FAIL"
                try:
                    result["userTurnCountAfter"] = self._dev_probe_user_turn_count(page)
                except Exception as exc:
                    fail("DEV_PROBE_USER_TURN_COUNT_FAILED", stage="DEV_PROBE_USER_TURN_CHECK", error=exc)

            result["threadIds"].setdefault("resolve", owner_thread_id)
            result["threadIds"].setdefault("inspect", owner_thread_id)
            result["threadIds"].setdefault("write", owner_thread_id)
            result["threadIds"].setdefault("verify", owner_thread_id)
            result["threadIds"].setdefault("clear", owner_thread_id)
            result["threadIds"].setdefault("clear_verify", owner_thread_id)
            # Re-read live health on the exact owner-thread page after the
            # clear verification.  This is a read-only fingerprint check; it
            # never reconnects, re-resolves another page, or mutates Brain
            # state.  A probe failure must not be relabelled as AUTH_REQUIRED
            # or INTERNAL, and the first failure above remains authoritative.
            try:
                live_health = self.live_poc_health_check()
                result["healthAfter"] = {
                    "browserConnected": bool(live_health.get("browserConnected")),
                    "pageAlive": bool(live_health.get("pageAlive")),
                    "precheckResult": live_health.get("precheckResult", "FAIL"),
                    "failureCode": live_health.get("failureCode"),
                    "hostname": live_health.get("hostname"),
                    "boundPageId": live_health.get("boundPageId"),
                    "brainState": self.state,
                    "authState": live_health.get("authState", "UNKNOWN"),
                    "composerReady": bool(live_health.get("composerReady")),
                }
                if (
                    result["healthAfter"]["boundPageId"] != page_id
                    or not result["healthAfter"]["browserConnected"]
                    or not result["healthAfter"]["pageAlive"]
                    or result["healthAfter"]["precheckResult"] != "PASS"
                ):
                    fail("DEV_PROBE_POST_HEALTH_FAILED", stage="DEV_PROBE_POST_HEALTH")
            except Exception as exc:
                result["healthAfter"] = {
                    "browserConnected": bool(self.browser is not None and not self._browser_disconnected),
                    "pageAlive": bool(self.page is page and self.bound_page_id == page_id),
                    "brainState": self.state,
                    "authState": self.auth_state,
                    "composerReady": False,
                    "precheckResult": "FAIL",
                    "failureCode": "DEV_PROBE_POST_HEALTH_FAILED",
                    "boundPageId": self.bound_page_id,
                }
                fail("DEV_PROBE_POST_HEALTH_FAILED", stage="DEV_PROBE_POST_HEALTH", error=exc)
            result["authStateAfter"] = result["healthAfter"]["authState"]
            result["brainStateAfter"] = result["healthAfter"]["brainState"]
            result["composerReadyAfter"] = result["healthAfter"]["composerReady"]
            if result["firstFailureCode"] is None and result["write"] == "PASS" and result["verify"] == "PASS" and result["clear"] == "PASS" and result["clearVerify"] == "PASS" and not result["accidentalSend"]:
                result["probeState"] = "PASS"
                result["ok"] = True
            else:
                result["probeState"] = "FAILED"
            # The returned structure contains only metadata and sanitized
            # errors. In particular, probe_text itself is never persisted.
            _append_jsonl(COMPOSER_PROBE_LOG, {key: value for key, value in result.items() if key != "ok"})
            return result

    def run_atomic_connect_and_composer_probe(
        self,
        probe_text: str,
        *,
        allow_send: bool = False,
        connect_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """Connect/reuse and run the no-send probe as one owner-thread operation.

        The lock spans the connection decision and the Composer probe.  A
        READY runtime is reused exactly; only a fully DISCONNECTED Host may
        use its existing formal connect path.  No new Registry, Host, browser,
        context, page, bridge session, or alternate transport is created here.
        """
        self._record_owner_thread("atomic_probe_start")
        with self._dev_probe_lock:
            self._atomic_probe_lock_held = True
            lifecycle_before = self.lifecycle_snapshot()
            result: dict[str, Any] = {
                "ok": False,
                "probeState": "NOT_RUN",
                "allowSend": bool(allow_send),
                "atomicConnect": False,
                "connectIdentity": None,
                "probeIdentity": None,
                "sameRegistry": False,
                "sameHost": False,
                "sameBoundPage": False,
                "sameOwnerThread": False,
                "connectRegistryId": None,
                "connectHostId": None,
                "connectBoundPageId": None,
                "connectOwnerThreadId": None,
                "probeRegistryId": None,
                "probeHostId": None,
                "probeBoundPageId": None,
                "probeOwnerThreadId": None,
                "lifecycleBefore": lifecycle_before["counts"],
                "lifecycleAfter": None,
                "runtimeLossEvidence": None,
            }
            try:
                if allow_send:
                    probe = self.run_dev_composer_write_probe(probe_text, allow_send=True)
                    result.update(probe)
                    result["atomicConnect"] = False
                    result["probeState"] = "FAILED"
                    return result

                self.connect_if_necessary(connect_attempt_id=connect_attempt_id)
                connect_identity = self.runtime_identity_snapshot()
                result["connectIdentity"] = connect_identity
                result.update({
                    "connectRegistryId": connect_identity["registryInstanceId"],
                    "connectHostId": connect_identity["hostInstanceId"],
                    "connectBoundPageId": connect_identity["boundPageId"],
                    "connectOwnerThreadId": connect_identity["ownerThreadId"],
                    "atomicConnect": True,
                })
                probe = self.run_dev_composer_write_probe(probe_text, allow_send=False)
                result.update(probe)
                probe_identity = self.runtime_identity_snapshot()
                result["probeIdentity"] = probe_identity
                result.update({
                    "probeRegistryId": probe_identity["registryInstanceId"],
                    "probeHostId": probe_identity["hostInstanceId"],
                    "probeBoundPageId": probe_identity["boundPageId"],
                    "probeOwnerThreadId": probe_identity["ownerThreadId"],
                    "sameRegistry": connect_identity["registryInstanceId"] == probe_identity["registryInstanceId"],
                    "sameHost": connect_identity["hostInstanceId"] == probe_identity["hostInstanceId"],
                    "sameBoundPage": connect_identity["boundPageId"] == probe_identity["boundPageId"],
                    "sameOwnerThread": connect_identity["ownerThreadId"] == probe_identity["ownerThreadId"],
                })
                if not all(result[name] for name in ("sameRegistry", "sameHost", "sameBoundPage", "sameOwnerThread")):
                    result["firstFailureCode"] = "ATOMIC_PROBE_RUNTIME_IDENTITY_CHANGED"
                    result["failureCode"] = "ATOMIC_PROBE_RUNTIME_IDENTITY_CHANGED"
                    result["probeState"] = "FAILED"
                    result["ok"] = False
                return result
            except PlaywrightAttachedBrainError as exc:
                result.update({
                    "probeState": "FAILED",
                    "failureCode": exc.code,
                    "failureStage": exc.stage or "ATOMIC_PROBE",
                    "failureName": exc.original_name,
                    "failureMessage": _safe_text(exc.cause or exc),
                    "failureStackId": exc.stack_id,
                })
                return result
            except Exception as exc:
                wrapped = PlaywrightAttachedBrainError(
                    "Atomic Composer probe failed",
                    code="CHROME_RUNTIME_UNAVAILABLE",
                    stage="ATOMIC_PROBE",
                    cause=exc,
                )
                result.update({
                    "probeState": "FAILED",
                    "failureCode": wrapped.code,
                    "failureStage": wrapped.stage,
                    "failureName": wrapped.original_name,
                    "failureMessage": _safe_text(exc),
                    "failureStackId": wrapped.stack_id,
                })
                return result
            finally:
                lifecycle_after = self.lifecycle_snapshot()
                result["lifecycleAfter"] = lifecycle_after["counts"]
                result["runtimeLossEvidence"] = lifecycle_after["lastEvent"] if lifecycle_after["lastEvent"] and lifecycle_after["lastEvent"].get("event") == "RUNTIME_LOSS_EVENT" else None
                self._atomic_probe_lock_held = False

    def recover(self) -> bool:
        self.close(reason_code="RECONNECT_REPLACEMENT", caller="PlaywrightAttachedBrainHost.recover")
        try:
            self.start()
        except Exception:
            return False
        return self.checkHealth()

    def auth_status(self) -> str:
        self._record_owner_thread("auth_check")
        page = self._ensure_target()
        try:
            fingerprint = self._page_fingerprint(page, self.selected_candidate_index or 0)
            scored = ChatGPTPageScorer.score(
                ChatGPTPageFingerprint.from_mapping(fingerprint)
            )
            status = str(scored["authState"])
            self.auth_state = status
            if scored["failureCode"] is None:
                self.state = PlaywrightAttachedBrainState.READY
                self.last_error = None
            elif scored["failureCode"] == "AUTH_REQUIRED":
                self._fail(PlaywrightAttachedBrainState.ERROR, "AUTH_REQUIRED", stage="T14_AUTH_STATE_RESULT")
            else:
                self._fail(PlaywrightAttachedBrainState.ERROR, str(scored["failureCode"]), stage="T13_COMPOSER_CHECK")
            return status
        except Exception as exc:
            self._fail(PlaywrightAttachedBrainState.PAGE_LOST, "CHATGPT_PAGE_UNAVAILABLE")
            raise PlaywrightAttachedBrainError("CHATGPT_PAGE_UNAVAILABLE") from exc

    def open_conversation(self, conversation_id: str | None = None) -> str:
        page = self._ensure_target()
        if conversation_id is not None and not re.fullmatch(r"[A-Za-z0-9-]+", conversation_id):
            raise PlaywrightAttachedBrainError("INVALID_CONVERSATION_BINDING")
        url = CHATGPT_URL if conversation_id is None else f"{CHATGPT_URL}c/{conversation_id}"
        page.goto(url, wait_until="domcontentloaded")
        if not is_allowed_chatgpt_url(str(page.url or "")):
            self._fail(PlaywrightAttachedBrainState.PAGE_LOST, "NAVIGATION_AWAY_FROM_CHATGPT")
            raise PlaywrightAttachedBrainError("NAVIGATION_AWAY_FROM_CHATGPT")
        return str(page.url)

    @staticmethod
    def _composer_text(composer: Any) -> str:
        try:
            value = composer.input_value()
        except Exception:
            try:
                value = composer.inner_text()
            except Exception:
                value = ""
        return str(value or "")

    def prepare_request(self, request: Any) -> dict[str, Any]:
        """Establish a turn boundary on the already selected ChatGPT page."""
        self._record_owner_thread("composer_check")
        page = self._ensure_target()
        if self.auth_status() != "AUTHENTICATED":
            raise PlaywrightAttachedBrainError("AUTH_REQUIRED", code="AUTH_REQUIRED", stage="T14_AUTH_STATE_RESULT")
        self._record_owner_thread("composer_resolve")
        target = ChatGPTPlaywrightDomAdapter.resolve_composer(page)
        if target is None:
            raise PlaywrightAttachedBrainError("COMPOSER_NOT_FOUND", code="COMPOSER_NOT_FOUND", stage="T13_COMPOSER_CHECK")
        if target.locator_count != 1:
            raise PlaywrightAttachedBrainError("COMPOSER_AMBIGUOUS", code="COMPOSER_AMBIGUOUS", stage="T13_COMPOSER_CHECK")
        if not target.visible:
            raise PlaywrightAttachedBrainError("COMPOSER_NOT_VISIBLE", code="COMPOSER_NOT_VISIBLE", stage="T13_COMPOSER_CHECK")
        if not target.editable:
            raise PlaywrightAttachedBrainError("COMPOSER_NOT_EDITABLE", code="COMPOSER_NOT_EDITABLE", stage="T13_COMPOSER_CHECK")
        if ChatGPTPlaywrightDomAdapter.composer_text(target):
            raise PlaywrightAttachedBrainError("COMPOSER_BUSY", code="COMPOSER_BUSY", stage="T13_COMPOSER_CHECK")
        self._poc_bound_page = page
        self._poc_bound_page_id = self.bound_page_id
        self._poc_composer_target = target
        self._poc_request = {
            "brainRequestId": str(getattr(request, "brain_request_id", "")),
            "taskId": str(getattr(request, "task_id", "")),
            "prompt": None,
            "baselineAssistantCount": page.locator('[data-message-author-role="assistant"]').count(),
            "baselineUserCount": page.locator('[data-message-author-role="user"]').count(),
            "sent": False,
        }
        self._baseline_assistant_count = int(self._poc_request["baselineAssistantCount"])
        self._poc_metrics = {key: False for key in self._poc_metrics}
        self._poc_metrics.update({
            "composerStrategy": target.strategy,
            "composerElementKind": target.element_kind,
        })
        return {
            "ok": True,
            "requestTurnBoundary": True,
            "baselineAssistantCount": self._poc_request["baselineAssistantCount"],
            "baselineUserCount": self._poc_request["baselineUserCount"],
        }

    @staticmethod
    def _usable_send_button(page: Any) -> Any | None:
        selectors = (
            '[data-testid="send-button"]',
            '[data-testid="send-message-button"]',
            '[data-testid="composer-submit-button"]',
        )
        for selector in selectors:
            try:
                locator = page.locator(selector)
                for index in range(min(int(locator.count()), 20)):
                    button = locator.nth(index)
                    if button.is_visible() and not button.is_disabled():
                        return button
            except Exception:
                continue
        try:
            locator = page.get_by_role("button", name=re.compile(r"^(send|send prompt|发送)$", re.I))
            for index in range(min(int(locator.count()), 20)):
                button = locator.nth(index)
                if button.is_visible() and not button.is_disabled():
                    return button
        except Exception:
            pass
        return None

    def send_prompt(self, prompt: str) -> None:
        self._record_owner_thread("composer_write")
        page = self._ensure_poc_target()
        target = self._poc_composer_target
        if target is None:
            raise PlaywrightAttachedBrainError("COMPOSER_NOT_FOUND", code="COMPOSER_NOT_FOUND", stage="T13_COMPOSER_CHECK")
        if self._poc_request is None:
            self._poc_request = {
                "brainRequestId": "UNKNOWN",
                "taskId": "UNKNOWN",
                "prompt": None,
                "baselineAssistantCount": page.locator('[data-message-author-role="assistant"]').count(),
                "baselineUserCount": page.locator('[data-message-author-role="user"]').count(),
                "sent": False,
            }
            self._baseline_assistant_count = int(self._poc_request["baselineAssistantCount"])
        try:
            operation = ChatGPTPlaywrightDomAdapter.write_composer(target, prompt)
        except Exception as exc:
            code = str(exc) if str(exc) in {
                "COMPOSER_NOT_VISIBLE", "COMPOSER_NOT_EDITABLE", "COMPOSER_AMBIGUOUS",
                "COMPOSER_WRITE_VERIFY_FAILED",
            } else "COMPOSER_WRITE_FAILED"
            error = PlaywrightAttachedBrainError(code, code=code, stage="T13_COMPOSER_WRITE", cause=exc)
            self._record_poc_failure(error)
            raise error from exc
        self._poc_request["prompt"] = prompt
        self._poc_metrics["composerWrite"] = True
        self._poc_metrics["composerWriteOperation"] = operation
        button = self._usable_send_button(page)
        try:
            if button is not None:
                button.click()
            else:
                target.locator.press("Enter")
        except Exception as exc:
            raise PlaywrightAttachedBrainError("SEND_FAILED", code="SEND_FAILED", stage="T14_SEND_CONFIRM", cause=exc) from exc
        self._poc_request["sent"] = True
        self._poc_metrics["chatgptSend"] = True

    def _record_poc_failure(self, error: PlaywrightAttachedBrainError) -> None:
        """Persist POC diagnostics without demoting a healthy Brain runtime."""
        self.last_error_details = error.envelope(self.connect_attempt_id or "UNKNOWN")
        self.diagnostics["lastFailureStage"] = error.stage
        self._trace(error.stage or "T13_COMPOSER_WRITE", "FAIL", code=error.code, error=error.cause or error)
        self._write_stack(error)

    def _ensure_poc_target(self) -> Any:
        """Use the page and Composer selected by prepare_request, never rebind."""
        self._record_owner_thread("poc_bound_target")
        if self._poc_bound_page is None or self.page is not self._poc_bound_page or self.bound_page_id != self._poc_bound_page_id:
            raise PlaywrightAttachedBrainError(
                "POC_BOUND_PAGE_CHANGED",
                code="POC_BOUND_PAGE_CHANGED",
                stage="T13_COMPOSER_WRITE",
            )
        page = self._poc_bound_page
        try:
            if bool(getattr(page, "is_closed", lambda: False)()):
                raise PlaywrightAttachedBrainError("CHATGPT_PAGE_NOT_FOUND", code="CHATGPT_PAGE_NOT_FOUND", stage="T10_PAGES_RESULT")
            if not is_allowed_chatgpt_url(str(page.url or "")):
                raise PlaywrightAttachedBrainError("CHATGPT_PAGE_URL_INVALID", code="CHATGPT_PAGE_URL_INVALID", stage="T10_PAGES_RESULT")
        except PlaywrightAttachedBrainError:
            raise
        except Exception as exc:
            raise PlaywrightAttachedBrainError("CHATGPT_PAGE_NOT_FOUND", code="CHATGPT_PAGE_NOT_FOUND", stage="T10_PAGES_RESULT", cause=exc) from exc
        return page

    def wait_for_completion(self, timeout_seconds: float) -> None:
        self._record_owner_thread("response_wait")
        page = self._ensure_target()
        deadline = time.monotonic() + timeout_seconds
        last_response = ""
        stable_since: float | None = None
        while time.monotonic() < deadline:
            if self.auth_status() != "AUTHENTICATED":
                raise PlaywrightAttachedBrainError("PLAYWRIGHT_BRAIN_AUTH_CHANGED", code="AUTH_LOST", stage="T15_RESPONSE_WAIT")
            try:
                stop = page.get_by_role("button", name=re.compile(r"stop generating|停止生成", re.I))
                is_generating = stop.count() > 0 and any(stop.nth(index).is_visible() for index in range(min(stop.count(), 5)))
            except Exception:
                is_generating = False
            assistant_nodes = page.locator('[data-message-author-role="assistant"]')
            assistant_count = assistant_nodes.count()
            response_node = assistant_nodes.last if assistant_count > self._poc_baseline_assistant_count() else None
            raw_response = ""
            if response_node is not None:
                try:
                    raw_response = str(response_node.inner_text() or "").strip()
                except Exception:
                    raw_response = ""
            try:
                composer = ChatGPTPlaywrightDomAdapter.find_composer(page)
                user_count = page.locator('[data-message-author-role="user"]').count()
                send_confirmed = bool(
                    (composer is not None and not self._composer_text(composer))
                    or user_count > self._poc_baseline_user_count()
                    or is_generating
                    or response_node is not None
                )
            except Exception:
                send_confirmed = False
            self._poc_metrics["chatgptSendConfirmed"] = send_confirmed
            self._poc_metrics["responseBoundary"] = response_node is not None and bool(raw_response)
            if raw_response and not is_generating and send_confirmed:
                if raw_response != last_response:
                    last_response = raw_response
                    stable_since = time.monotonic()
                elif stable_since is not None and time.monotonic() - stable_since >= 1.0:
                    self._poc_metrics["responseCompletion"] = True
                    self._poc_metrics["chatgptResponseCapture"] = True
                    return
            page.wait_for_timeout(250)
        raise PlaywrightAttachedBrainError("CHATGPT_RESPONSE_TIMEOUT", code="RESPONSE_TIMEOUT", stage="T15_RESPONSE_WAIT")

    def _poc_baseline_assistant_count(self) -> int:
        return int((self._poc_request or {}).get("baselineAssistantCount", self._baseline_assistant_count))

    def _poc_baseline_user_count(self) -> int:
        return int((self._poc_request or {}).get("baselineUserCount", 0))

    def read_response(self) -> str:
        self._record_owner_thread("response_capture")
        page = self._ensure_target()
        articles = page.locator('[data-message-author-role="assistant"]')
        if articles.count() <= self._poc_baseline_assistant_count():
            raise PlaywrightAttachedBrainError("RESPONSE_NOT_FOUND", code="RESPONSE_NOT_AVAILABLE", stage="T16_RESPONSE_CAPTURE")
        raw = str(articles.last.inner_text() or "").strip()
        if not raw:
            raise PlaywrightAttachedBrainError("RESPONSE_NOT_FOUND", code="RESPONSE_NOT_AVAILABLE", stage="T16_RESPONSE_CAPTURE")
        self._poc_metrics["chatgptResponseCapture"] = True
        self._poc_metrics["responseBoundary"] = True
        return raw

    def poc_metrics(self) -> dict[str, Any]:
        return dict(self._poc_metrics)

    def close(self, *, reason_code: str = "EXPLICIT_HOST_STOP", caller: str | None = None) -> None:
        # Disconnect Playwright's client only. Never close the user-owned
        # browser or any of its contexts/pages.
        self._record_owner_thread("close")
        self._closing = True
        self._runtime_lifecycle_event(
            "FORMAL_HOST_RESET",
            reason_code,
            caller=caller or "PlaywrightAttachedBrainHost.close",
        )
        playwright = getattr(self, "playwright", None)
        self.page = None
        self.context = None
        self.browser = None
        self.playwright = None
        self._bound_page_object = None
        self.bound_page_id = None
        self._diagnostic_bound_page_object = None
        self._diagnostic_bound_main_frame_object = None
        self._diagnostic_bound_context_object = None
        self.selected_candidate_index = None
        self.page_candidates = []
        self._poc_bound_page = None
        self._poc_bound_page_id = None
        self._poc_composer_target = None
        self.auth_state = "UNKNOWN"
        self.state = PlaywrightAttachedBrainState.DISCONNECTED
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                pass
        self._closing = False

    def stop(self, *, reason_code: str = "EXPLICIT_HOST_STOP", caller: str | None = None) -> None:
        self.close(reason_code=reason_code, caller=caller or "PlaywrightAttachedBrainHost.stop")

    def status(self) -> dict[str, Any]:
        self._record_owner_thread("health_status")
        contexts: list[Any] = []
        pages: list[Any] = []
        if self.browser is not None and not self._browser_disconnected:
            try:
                contexts = list(self.browser.contexts)
                pages = [page for context in contexts for page in list(context.pages)]
            except Exception:
                contexts, pages = [], []
        selected = self.page if self.page is not None else None
        if selected is None and pages:
            selected, diagnostics = self.page_resolver.resolve_with_diagnostics(pages, self._page_fingerprint)
            candidate_diagnostics = diagnostics
        else:
            candidate_diagnostics = list(self.page_candidates)
        selected_url = str(getattr(selected, "url", "") or "") if selected is not None else ""
        composer_found = False
        selected_diagnostic = next((item for item in candidate_diagnostics if item.get("selectedCandidate")), None)
        if selected_diagnostic is not None:
            composer_found = bool(selected_diagnostic.get("editableComposerCount", 0) > 0)
        from urllib.parse import urlsplit
        candidates = []
        for item in candidate_diagnostics:
            candidate = dict(item)
            index = int(candidate.get("candidateIndex", candidate.get("index", 0)))
            candidate["index"] = index
            candidate["safePageId"] = f"page-{index}"
            # pageTitle is already reduced to a generic value by
            # _safe_page_title; pathname contains no query or fragment.
            candidates.append(candidate)
        return {
            "state": self.state,
            "authState": self.auth_state,
            "browserRuntime": PLAYWRIGHT_ATTACHED_BROWSER_RUNTIME,
            "formalBrowserConnection": PLAYWRIGHT_FORMAL_CONNECTION if self.connection_mode == "EXACT_WS" else "PLAYWRIGHT_CONNECT_OVER_CDP_URL_LEGACY",
            "channel": None if self.connection_mode == "EXACT_WS" else self.channel,
            "cdpEndpoint": "DevToolsActivePort->EXACT_WS" if self.connection_mode == "EXACT_WS" else self.cdp_endpoint,
            "cdpUrlFormalRole": "DEDICATED_DEVTOOLS_ACTIVE_PORT_EXACT_WS" if self.connection_mode == "EXACT_WS" else "LEGACY_EXPLICIT_ONLY",
            "formalAttachMode": self.attach_mode,
            "formalEndpointSource": self.endpoint_source,
            "dedicatedUserDataDir": str(self.dedicated_user_data_dir) if self.connection_mode == "EXACT_WS" else None,
            "browserAlive": self.browser is not None and not self._browser_disconnected,
            "cdpConnected": self.browser is not None and not self._browser_disconnected,
            "contextFound": bool(contexts),
            "contextCount": len(contexts),
            "chatgptPageFound": bool(selected is not None and is_allowed_chatgpt_url(selected_url)),
            "chatgptPageUrlValid": is_allowed_chatgpt_url(selected_url),
            "composerFound": composer_found,
            "pageTargetCount": len(pages),
            "chatgptTargetCount": sum(1 for page in pages if self.page_resolver.eligible(page)),
            "selectedTargetHostname": "chatgpt.com" if is_allowed_chatgpt_url(selected_url) else None,
            "selectedTargetPathname": selected_url.split("?", 1)[0].split("#", 1)[0] if selected_url else None,
            "selectedCandidateIndex": self.selected_candidate_index,
            "chatgptPageSelectionPolicy": SELECTION_POLICY,
            "applicationTabPolicy": "CHATGPT_ONLY",
            "otherTabAccess": "FORBIDDEN",
            "cookieReadByApp": "NONE",
            "tokenReadByApp": "NONE",
            "otherTabReadByApp": "NONE",
            "lastError": self.last_error,
            "connectAttemptId": self.connect_attempt_id,
            "attemptStartedAt": self.attempt_started_at,
            "attemptFinishedAt": self.attempt_finished_at,
            "durationMs": self.attempt_duration_ms,
            "buildId": os.environ.get("APP_BUILD_ID", "UNKNOWN"),
            "gitCommit": os.environ.get("GIT_COMMIT", "UNKNOWN"),
            "sourceTimestamp": os.environ.get("SOURCE_TIMESTAMP", "UNKNOWN"),
            "cdpHttpPreflight": self.diagnostics.get("cdpHttpPreflight", "UNKNOWN"),
            "cdpHttpStatus": self.diagnostics.get("cdpHttpStatus"),
            "jsonVersionShape": self.diagnostics.get("jsonVersionShape", "UNKNOWN"),
            "playwrightLoad": self.diagnostics.get("playwrightLoad", "UNKNOWN"),
            "connectOverCdp": self.diagnostics.get("connectOverCdp", "UNKNOWN"),
            "connectTimeoutMs": (self.diagnostics.get("formalEffectiveConnectOptions") or {}).get("timeoutMs", self.connect_timeout_ms),
            "formalEffectiveConnectOptions": self.diagnostics.get("formalEffectiveConnectOptions"),
            "formalCdpPort": self.diagnostics.get("formalCdpPort"),
            "formalEndpointFreshness": self.diagnostics.get("formalEndpointFreshness", "UNKNOWN"),
            "formalTcpProbe": self.diagnostics.get("formalTcpProbe"),
            "formalConnectFailure": self.diagnostics.get("formalConnectFailure"),
            "browserConnected": self.diagnostics.get("browserConnected", False),
            "brainConnectionId": self.brain_connection_id,
            "registryInstanceId": self.registry_instance_id,
            "hostInstanceId": self.host_instance_id,
            "processPid": self.process_pid,
            "parentPid": self.parent_pid,
            "runtimeOwner": self.runtime_owner,
            "ownerThreadId": self.owner_thread_id,
            "threadMatrix": dict(self._thread_operation_matrix),
            "threadAffinityError": self.thread_affinity_error,
            "formalRuntimeType": "PLAYWRIGHT_ATTACHED_REAL_CHROME",
            "boundPageId": self.bound_page_id,
            "lastSuccessfulStage": self.diagnostics.get("lastSuccessfulStage"),
            "lastSuccessStage": self.diagnostics.get("lastSuccessStage"),
            "lastFailureStage": self.diagnostics.get("lastFailureStage"),
            "lastErrorCode": (self.last_error_details or {}).get("code") or self.last_error,
            "safeErrorSummary": (self.last_error_details or {}).get("message") or self.last_error,
            "lastErrorDetails": self.last_error_details,
            "runtimeLifecycle": self.lifecycle_snapshot(),
            "trace": list(self.trace),
            "pageCandidates": candidates,
            "versions": self.diagnostics.get("versions", {}),
            "channelSupport": self.diagnostics.get("channelSupport", {}),
            "channelResolution": "PASS" if self.diagnostics.get("connectOverCdp") == "PASS" else ("FAIL" if self.diagnostics.get("connectOverCdp") == "FAIL" else "NOT_RUN"),
        }


__all__ = [
    "PLAYWRIGHT_CDP_ENDPOINT",
    "PLAYWRIGHT_CHANNEL",
    "PLAYWRIGHT_LEGACY_CDP_URL",
    "PLAYWRIGHT_ATTACHED_BROWSER_RUNTIME",
    "PLAYWRIGHT_FORMAL_CONNECTION",
    "FORMAL_ATTACH_MODE",
    "FORMAL_ENDPOINT_SOURCE",
    "FORMAL_EXACT_CONNECT_TIMEOUT_MS",
    "chromium_channel_support",
    "PlaywrightAttachedBrainError",
    "PlaywrightAttachedBrainState",
    "ChatGPTPageFingerprint",
    "ChatGPTAuthenticatedFingerprint",
    "ChatGPTPageScorer",
    "PlaywrightChatGPTPageResolver",
    "PlaywrightAttachedBrainTransport",
    "PlaywrightAttachedBrainHost",
    "is_allowed_chatgpt_url",
]
