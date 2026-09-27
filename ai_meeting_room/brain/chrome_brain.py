"""Dedicated Google Chrome runtime for the formal GPT Web Brain.

The runtime launches only an application-owned Chrome profile and exposes a
small DOM-level BrowserController. It never reads browser databases, cookies,
storage state, headers, tokens, passwords, or verification data.
"""

from __future__ import annotations

import http.client
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .chatgpt_web import BrowserController


CHATGPT_URL = "https://chatgpt.com/"
CHROME_BRAIN_PROFILE = Path.home() / "Library" / "Application Support" / "AI Meeting Room" / "chrome-brain-profile"
CHROME_DEBUG_HOST = "127.0.0.1"
CHROME_DEBUG_PORT = 0
CHATGPT_ALLOWED_HOST = "chatgpt.com"


class ChromeBrainError(RuntimeError):
    pass


class ChromeBrainStartupStage:
    NOT_STARTED = "NOT_STARTED"
    BOOT_INIT = "BOOT_INIT"
    CHECKING_CHROME = "CHECKING_CHROME"
    STARTING_CHROME = "STARTING_CHROME"
    ATTACHING_CHROME = "ATTACHING_CHROME"
    DISCOVERING_CDP = "DISCOVERING_CDP"
    CONNECTING_CDP = "CONNECTING_CDP"
    DISCOVERING_TARGETS = "DISCOVERING_TARGETS"
    BINDING_CHATGPT = "BINDING_CHATGPT"
    CHECKING_DOM = "CHECKING_DOM"
    READY = "READY"
    FAILED = "FAILED"


@dataclass(frozen=True)
class ChatGPTDomFingerprint:
    """Non-content DOM signals used to classify a ChatGPT page."""

    ready_state: str
    app_shell: bool
    composer: bool
    login_form: bool
    challenge: bool

    @property
    def authenticated(self) -> bool:
        return self.app_shell and self.composer and not self.login_form and not self.challenge


@dataclass(frozen=True)
class ChatGPTTargetMetadata:
    """Safe target metadata; never contains URL query values or page content."""

    target_id: str | None
    target_type: str
    hostname: str
    pathname: str
    title: str
    ready_state: str
    active: bool
    eligible_chatgpt: bool
    fingerprint: ChatGPTDomFingerprint


def _url_metadata(url: str) -> tuple[str, str]:
    parsed = urlparse(url or "")
    return parsed.hostname or "", parsed.path or "/"


def is_chatgpt_page(url: str) -> bool:
    """Accept only the app-owned ChatGPT host, never OAuth/internal targets."""
    parsed = urlparse(url or "")
    return parsed.scheme == "https" and parsed.hostname == CHATGPT_ALLOWED_HOST and not parsed.path.startswith("/auth/") and "/login" not in parsed.path


class ChatGPTChromeDomAdapter:
    """Version-tolerant, DOM-only adapter for the currently rendered ChatGPT UI."""

    COMPOSER_SELECTORS = (
        '[data-testid="prompt-textarea"]',
        '#prompt-textarea',
        '[role="textbox"][contenteditable="true"]',
        '[contenteditable="true"][role="textbox"]',
        'textarea[placeholder]',
        '[contenteditable="true"]',
    )
    APP_SHELL_SELECTORS = ("main", '[role="main"]', "nav", "#__next")
    LOGIN_SELECTORS = ('input[type="email"]', 'form[action*="login"]', 'a[href*="/auth/login"]')

    @staticmethod
    def _count(locator: Any) -> int:
        try:
            return int(locator.count())
        except Exception:
            return 0

    @classmethod
    def _visible_editable(cls, locator: Any) -> Any | None:
        for index in range(cls._count(locator)):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                editable = candidate.is_editable() if hasattr(candidate, "is_editable") else True
                if editable:
                    return candidate
            except Exception:
                continue
        return None

    @classmethod
    def find_composer(cls, page: Any) -> Any | None:
        for selector in cls.COMPOSER_SELECTORS:
            candidate = cls._visible_editable(page.locator(selector))
            if candidate is not None:
                return candidate
        return None

    @classmethod
    def _has_any(cls, page: Any, selectors: tuple[str, ...]) -> bool:
        return any(cls._count(page.locator(selector)) > 0 for selector in selectors)

    @classmethod
    def _has_login_text(cls, page: Any) -> bool:
        try:
            return page.get_by_text(re.compile(r"sign in|log in|登录|注册", re.I)).count() > 0
        except Exception:
            return False

    @classmethod
    def fingerprint(cls, page: Any) -> ChatGPTDomFingerprint:
        ready_state = "unknown"
        try:
            ready_state = str(page.evaluate("document.readyState"))
        except Exception:
            pass
        challenge = False
        try:
            challenge = bool(page.url and ("challenges.cloudflare.com" in page.url or page.get_by_text("Verify you are human", exact=False).count() > 0))
        except Exception:
            pass
        composer = cls.find_composer(page) is not None
        app_shell = cls._has_any(page, cls.APP_SHELL_SELECTORS)
        login_form = cls._has_any(page, cls.LOGIN_SELECTORS) or cls._has_login_text(page)
        return ChatGPTDomFingerprint(ready_state, app_shell, composer, login_form, challenge)

    @classmethod
    def status(cls, page: Any) -> str:
        fingerprint = cls.fingerprint(page)
        if fingerprint.challenge:
            return "CHALLENGE_REQUIRED"
        if fingerprint.login_form:
            return "AUTH_REQUIRED"
        if fingerprint.ready_state != "complete" or (fingerprint.app_shell and not fingerprint.composer):
            return "LOADING"
        if fingerprint.authenticated:
            return "AUTHENTICATED"
        return "DOM_UNKNOWN"


class ChatGPTTargetResolver:
    """Re-scan live page targets and choose an active, authenticated ChatGPT tab."""

    def __init__(self, dom: ChatGPTChromeDomAdapter | None = None) -> None:
        self.dom = dom or ChatGPTChromeDomAdapter()
        self.last_targets: list[ChatGPTTargetMetadata] = []
        self.selected_target: ChatGPTTargetMetadata | None = None

    @staticmethod
    def _target_id(page: Any) -> str | None:
        # Playwright does not expose a portable CDP target id. Use it when a
        # provider exposes one, otherwise report UNKNOWN rather than guessing.
        for name in ("target_id", "_target_id"):
            value = getattr(page, name, None)
            if value and isinstance(value, str):
                return value
        return None

    @staticmethod
    def _active(page: Any) -> bool:
        try:
            return bool(page.evaluate("document.visibilityState === 'visible'"))
        except Exception:
            return False

    def inspect(self, pages: list[Any]) -> list[ChatGPTTargetMetadata]:
        targets: list[ChatGPTTargetMetadata] = []
        for page in pages:
            url = str(getattr(page, "url", "") or "")
            hostname, pathname = _url_metadata(url)
            fingerprint = self.dom.fingerprint(page)
            try:
                title = str(page.title())
            except Exception:
                title = ""
            targets.append(ChatGPTTargetMetadata(
                target_id=self._target_id(page),
                target_type="page",
                hostname=hostname,
                pathname=pathname,
                title=title,
                ready_state=fingerprint.ready_state,
                active=self._active(page),
                eligible_chatgpt=is_chatgpt_page(url),
                fingerprint=fingerprint,
            ))
        self.last_targets = targets
        return targets

    def resolve(self, pages: list[Any]) -> Any | None:
        candidates: list[tuple[tuple[int, int, int, int], Any]] = []
        self.inspect(pages)
        # Re-read pages in the same order used by inspect so no non-ChatGPT
        # target can become the selected page.
        for index, page in enumerate(pages):
            if not is_chatgpt_page(str(getattr(page, "url", "") or "")):
                continue
            fingerprint = self.dom.fingerprint(page)
            active = self._active(page)
            # A visible authenticated/composer-ready page wins over a visible
            # loading or login page. The final index is only a deterministic
            # tie-breaker, never the primary selection rule.
            candidates.append(((int(fingerprint.authenticated), int(fingerprint.composer), int(active), index), page))
        if not candidates:
            self.selected_target = None
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        selected = candidates[0][1]
        self.selected_target = next((target for page, target in zip(pages, self.last_targets) if page is selected), None)
        return selected

    def diagnostics(self) -> list[dict[str, Any]]:
        """Return safe page-target diagnostics without page content."""
        return [
            {
                "targetId": target.target_id,
                "targetType": target.target_type,
                "hostname": target.hostname,
                "pathname": target.pathname,
                "title": target.title,
                "readyState": target.ready_state,
                "active": target.active,
                "eligibleChatGPT": target.eligible_chatgpt,
                "hasComposer": target.fingerprint.composer,
                "hasAppShell": target.fingerprint.app_shell,
                "hasLoginForm": target.fingerprint.login_form,
                "hasChallenge": target.fingerprint.challenge,
            }
            for target in self.last_targets
        ]

    def selected_diagnostics(self) -> dict[str, Any]:
        target = self.selected_target
        if target is None:
            return {
                "selectedTargetHostname": None,
                "selectedTargetPathname": None,
                "composerDetected": False,
                "loginFormDetected": False,
                "challengeDetected": False,
            }
        return {
            "selectedTargetHostname": target.hostname,
            "selectedTargetPathname": target.pathname,
            "composerDetected": target.fingerprint.composer,
            "loginFormDetected": target.fingerprint.login_form,
            "challengeDetected": target.fingerprint.challenge,
        }


@dataclass(frozen=True)
class DedicatedChromeProcess:
    pid: int
    executable: str
    profile_path: str
    command_line: str

    @property
    def has_remote_debugging(self) -> bool:
        return "--remote-debugging-port" in self.command_line or "--remote-debugging-address" in self.command_line


class DedicatedChromeProcessResolver:
    """Find only Chrome processes carrying this app's exact profile argument."""

    def __init__(self, profile: ChromeBrainProfileManager | None = None, *, executable: str | None = None) -> None:
        self.profile = profile or ChromeBrainProfileManager()
        self.executable = executable

    def _lines(self) -> list[str]:
        if not shutil.which("pgrep"):
            return []
        try:
            result = subprocess.run(["pgrep", "-af", "Google Chrome"], check=False, capture_output=True, text=True)
        except OSError:
            return []
        return result.stdout.splitlines()

    def find_all(self) -> list[DedicatedChromeProcess]:
        profile_arg = f"--user-data-dir={self.profile.profile_path}"
        expected = self.executable or find_google_chrome()
        found: list[DedicatedChromeProcess] = []
        for line in self._lines():
            if profile_arg not in line:
                continue
            try:
                pid_text, command_line = line.split(maxsplit=1)
                pid = int(pid_text)
            except (ValueError, IndexError):
                continue
            if expected and expected not in command_line and "Google Chrome" not in command_line:
                continue
            found.append(DedicatedChromeProcess(pid, expected or "Google Chrome", str(self.profile.profile_path), command_line))
        return found

    def find(self) -> DedicatedChromeProcess | None:
        processes = self.find_all()
        if not processes:
            return None
        # Chrome's browser process does not carry a --type child marker.
        return next((process for process in processes if "--type=" not in process.command_line), processes[0])

    def pids(self) -> set[int]:
        return {process.pid for process in self.find_all()}

    def profile_process_diagnostics(self) -> list[dict[str, Any]]:
        return [
            {
                "pid": process.pid,
                "executable": process.executable,
                "userDataDirMatched": process.profile_path == str(self.profile.profile_path),
                "remoteDebuggingArgumentPresent": process.has_remote_debugging,
            }
            for process in self.find_all()
        ]

    def default_chrome_present(self) -> bool:
        profile_arg = f"--user-data-dir={self.profile.profile_path}"
        return any(profile_arg not in line for line in self._lines())

    def profile_lock_detected(self) -> bool:
        return any((self.profile.profile_path / name).exists() for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"))

    def profile_lock_diagnostics(self) -> dict[str, bool]:
        return {
            name: (self.profile.profile_path / name).exists()
            for name in ("SingletonLock", "SingletonSocket", "SingletonCookie")
        }

    def gracefully_stop(self, timeout_seconds: float = 5.0) -> bool:
        """Stop only exact-profile Chrome processes; never force-kill them."""
        pids = self.pids()
        if not pids:
            return True
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                continue
            except OSError:
                return False
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not self.pids():
                return True
            time.sleep(0.1)
        return not self.pids()

    def diagnostics(self) -> dict[str, Any]:
        browser = self.find()
        return {
            "ownedProcessDetected": browser is not None,
            "ownedBrowserPid": browser.pid if browser else None,
            "existingDedicatedChromeDetected": browser is not None,
            "existingProfileChrome": self.profile_process_diagnostics(),
            "defaultChromePresent": self.default_chrome_present(),
            "profileLockDetected": self.profile_lock_detected(),
        }


class DedicatedProfileSingletonInspector:
    """Inspect and, only when provably stale, remove Chrome runtime locks."""

    RUNTIME_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")

    def __init__(self, profile: ChromeBrainProfileManager, resolver: DedicatedChromeProcessResolver) -> None:
        self.profile = profile
        self.resolver = resolver

    def inspect(self, *, cdp_reachable: Callable[[int], bool] | None = None) -> dict[str, Any]:
        lock_state = {
            name: (self.profile.profile_path / name).exists()
            for name in self.RUNTIME_FILES
        }
        owned = self.resolver.find_all()
        active_port_path = self.profile.profile_path / "DevToolsActivePort"
        active_port_exists = active_port_path.is_file() and not active_port_path.is_symlink()
        active_port: int | None = None
        if active_port_exists:
            try:
                lines = active_port_path.read_text(encoding="utf-8").splitlines()
                candidate = int(lines[0].strip()) if lines else 0
                if 1 <= candidate <= 65535:
                    active_port = candidate
            except (OSError, UnicodeError, ValueError):
                active_port = None
        reachable = bool(active_port and cdp_reachable and cdp_reachable(active_port))
        return {
            "singletonLock": lock_state["SingletonLock"],
            "singletonSocket": lock_state["SingletonSocket"],
            "singletonCookie": lock_state["SingletonCookie"],
            "ownedProfileChromeCount": len(owned),
            "ownedProfileChromePids": [process.pid for process in owned],
            "devToolsActivePortExists": active_port_exists,
            "devToolsActivePortPort": active_port,
            "cdpReachable": reachable,
        }

    @classmethod
    def stale_from(cls, state: dict[str, Any]) -> bool:
        return (
            state.get("ownedProfileChromeCount") == 0
            and not state.get("cdpReachable", False)
            and not state.get("devToolsActivePortPort")
            and any(state.get(name, False) for name in ("singletonLock", "singletonSocket", "singletonCookie"))
        )

    def cleanup_if_stale(self, *, cdp_reachable: Callable[[int], bool] | None = None) -> dict[str, Any]:
        before = self.inspect(cdp_reachable=cdp_reachable)
        stale = (
            before.get("ownedProfileChromeCount") == 0
            and not before.get("cdpReachable", False)
            and not before.get("devToolsActivePortPort")
            and any(before.get(key, False) for key in ("singletonLock", "singletonSocket", "singletonCookie"))
        )
        removed: dict[str, bool] = {}
        if stale:
            for name in self.RUNTIME_FILES:
                path = self.profile.profile_path / name
                try:
                    info = path.lstat()
                    if path.is_symlink() or (not stat.S_ISREG(info.st_mode) and not stat.S_ISSOCK(info.st_mode)):
                        removed[name] = False
                        continue
                    path.unlink()
                    removed[name] = True
                except FileNotFoundError:
                    removed[name] = False
                except OSError:
                    removed[name] = False
        after = self.inspect(cdp_reachable=cdp_reachable)
        return {
            "stale": stale,
            "before": before,
            "removed": removed,
            "after": after,
        }

class ChromeBrainProfileManager:
    """Create and validate the one private Chrome profile owned by the app."""

    def __init__(self, profile_path: str | Path | None = None) -> None:
        self.profile_path = Path(profile_path).expanduser() if profile_path else CHROME_BRAIN_PROFILE

    def create(self) -> Path:
        if self.profile_path.name == "Default" or self.profile_path.name == "Profile 1":
            raise ChromeBrainError("DEFAULT_CHROME_PROFILE_FORBIDDEN")
        if self.profile_path.exists() and not self.profile_path.is_dir():
            raise ChromeBrainError("CHROME_BRAIN_PROFILE_NOT_DIRECTORY")
        self.profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.profile_path, 0o700)
        except OSError as exc:
            raise ChromeBrainError("CHROME_BRAIN_PROFILE_PERMISSION_FAILED") from exc
        return self.profile_path

    def health_check(self) -> bool:
        try:
            return self.profile_path.is_dir() and stat.S_IMODE(self.profile_path.stat().st_mode) & 0o077 == 0
        except OSError:
            return False


def find_google_chrome() -> str | None:
    candidates = (
        os.environ.get("AIMR_CHROME_EXECUTABLE"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        shutil.which("google-chrome"),
        shutil.which("chrome"),
    )
    return next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)


class _SafeChromeOutputTail:
    """Drain Chrome output without retaining credentials or page content."""

    _SENSITIVE_QUERY = re.compile(r"(?i)([?&](?:token|access_token|refresh_token|code|key|secret|authorization|cookie|session)[^=]*=)[^&\s]+")
    _SENSITIVE_HEADER = re.compile(r"(?i)((?:authorization|cookie|set-cookie|x-api-key)\s*[:=]\s*).*$")

    def __init__(self, stream: Any, *, name: str, max_lines: int = 100, max_chars: int = 20000) -> None:
        self.stream = stream
        self.name = name
        self.lines: deque[str] = deque(maxlen=max_lines)
        self.max_chars = max_chars
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, name=f"chrome-{name}-tail", daemon=True)
        self._thread.start()

    @classmethod
    def sanitize(cls, value: str) -> str:
        value = cls._SENSITIVE_QUERY.sub(r"\1<redacted>", value)
        value = cls._SENSITIVE_HEADER.sub(r"\1<redacted>", value)
        value = re.sub(r"(?i)https?://[^\s?#]+(?:[?#][^\s]*)?", lambda match: match.group(0).split("?", 1)[0].split("#", 1)[0], value)
        return value.replace("\x00", "")[:400]

    def _drain(self) -> None:
        try:
            for raw in self.stream:
                line = self.sanitize(str(raw).rstrip("\r\n"))
                with self._lock:
                    self.lines.append(line)
        except (OSError, ValueError):
            return

    def snapshot(self) -> list[str]:
        with self._lock:
            lines = list(self.lines)
        return lines[-100:]

    def join(self, timeout: float = 0.5) -> None:
        self._thread.join(timeout=timeout)


class ChromeBrainRuntime:
    """Own a headed Chrome process and loopback-only DevTools endpoint."""

    def __init__(self, profile: ChromeBrainProfileManager | None = None, *, executable: str | None = None, port: int = CHROME_DEBUG_PORT) -> None:
        if port < 0 or port > 65535:
            raise ValueError("invalid Chrome debugging port")
        self.profile = profile or ChromeBrainProfileManager()
        self.executable = executable
        self.process_resolver = DedicatedChromeProcessResolver(self.profile, executable=executable)
        self.singleton_inspector = DedicatedProfileSingletonInspector(self.profile, self.process_resolver)
        self.port: int | None = port or None
        self.process: subprocess.Popen[bytes] | None = None
        self._adopted = False
        self.startup_stage = ChromeBrainStartupStage.NOT_STARTED
        self.chrome_process_detected = False
        self.chrome_pid: int | None = None
        self.chrome_executable: str | None = executable
        self.cdp_endpoint_discovered = False
        self.cdp_connected = False
        self.last_failure_code: str | None = None
        self.last_failure_stage: str | None = None
        self.devtools_websocket_path: str | None = None
        self.runtime_id = str(uuid.uuid4())
        self.started_at: float | None = None
        self.launch_pid: int | None = None
        self.launch_process_exited = False
        self.launch_exit_code: int | None = None
        self.owned_browser_pid: int | None = None
        self.browser_handoff_detected = False
        self.profile_lock_detected = False
        self.devtools_active_port_exists = False
        self.devtools_active_port_port: int | None = None
        self.devtools_active_port_mtime: float | None = None
        self.stale_devtools_active_port = False
        self.cdp_endpoint_reachable = False
        self.profile_lock_state: dict[str, Any] = {}
        self.singleton_before: dict[str, Any] = {}
        self.singleton_cleanup: dict[str, Any] = {}
        self.singleton_after: dict[str, Any] = {}
        self.stale_profile_singleton = False
        self.existing_profile_chrome: list[dict[str, Any]] = []
        self.default_chrome_present = False
        self.chrome_stdout_tail: list[str] = []
        self.chrome_stderr_tail: list[str] = []
        self.chrome_exit_code: int | None = None
        self.chrome_exit_signal: str | None = None
        self.chrome_runtime_ms: int | None = None
        self.launch_args_safe: list[str] = []
        self.launch_cwd: str | None = None
        self.launcher_type: str | None = None
        self.chrome_start_timeline: list[dict[str, Any]] = []
        self._timeline_started_at: float | None = None
        self._timeline_states: dict[str, Any] = {}
        self._stdout_capture: _SafeChromeOutputTail | None = None
        self._stderr_capture: _SafeChromeOutputTail | None = None
        self._exit_diagnostic_recorded = False

    def set_stage(self, stage: str, *, failure_code: str | None = None) -> None:
        previous_stage = self.startup_stage
        self.startup_stage = stage
        if stage == ChromeBrainStartupStage.FAILED:
            self.last_failure_code = failure_code or self.last_failure_code or "UNKNOWN"
            self.last_failure_stage = self.last_failure_stage or previous_stage
        elif failure_code:
            self.last_failure_code = failure_code
            self.last_failure_stage = stage

    @property
    def endpoint(self) -> str:
        if not self.port:
            raise ChromeBrainError("CDP_ENDPOINT_NOT_FOUND")
        return f"http://{CHROME_DEBUG_HOST}:{self.port}"

    def _debug_endpoint_busy(self, port: int | None = None) -> bool:
        port = port or self.port
        if not port:
            return False
        try:
            connection = http.client.HTTPConnection(CHROME_DEBUG_HOST, port, timeout=0.5)
            connection.request("GET", "/json/version")
            response = connection.getresponse()
            response.read()
            connection.close()
            return response.status == 200
        except OSError:
            return False

    def _owned_profile_process_exists(self) -> bool:
        return self.process_resolver.find() is not None

    def _record_timeline(self, event: str, **details: Any) -> None:
        if self._timeline_started_at is None:
            self._timeline_started_at = time.monotonic()
        item: dict[str, Any] = {
            "event": event,
            "elapsedMs": int((time.monotonic() - self._timeline_started_at) * 1000),
        }
        item.update(details)
        self.chrome_start_timeline.append(item)

    def _append_launch_log(self, message: str) -> None:
        log_path = Path(__file__).resolve().parents[2] / "logs" / "chrome-brain-launch.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(_SafeChromeOutputTail.sanitize(message)[:1000] + "\n")
        except OSError:
            return

    def _capture_process_exit(self) -> None:
        if self.process is None or self.process.poll() is None:
            return
        if self.launch_process_exited and self._exit_diagnostic_recorded:
            return
        self.launch_process_exited = True
        self.launch_exit_code = self.process.returncode
        self.chrome_exit_code = self.process.returncode if self.process.returncode is not None and self.process.returncode >= 0 else None
        self.chrome_exit_signal = None
        if self.process.returncode is not None and self.process.returncode < 0:
            try:
                self.chrome_exit_signal = signal.Signals(-self.process.returncode).name
            except ValueError:
                self.chrome_exit_signal = f"SIGNAL_{-self.process.returncode}"
        if self.started_at is not None:
            self.chrome_runtime_ms = max(0, int((time.time() - self.started_at) * 1000))
        if self._stdout_capture is not None:
            self._stdout_capture.join()
            self.chrome_stdout_tail = self._stdout_capture.snapshot()
        if self._stderr_capture is not None:
            self._stderr_capture.join()
            self.chrome_stderr_tail = self._stderr_capture.snapshot()
        self._record_timeline(
            "launch process exit",
            launchPid=self.launch_pid,
            exitCode=self.chrome_exit_code,
            signal=self.chrome_exit_signal,
            runtimeMs=self.chrome_runtime_ms,
        )
        self._append_launch_log(
            f"CHROME_EXIT_DIAGNOSTIC exitCode={self.chrome_exit_code} signal={self.chrome_exit_signal} "
            f"runtimeMs={self.chrome_runtime_ms} stderrTail={self.chrome_stderr_tail[-100:]} stdoutTail={self.chrome_stdout_tail[-100:]}"
        )
        self._exit_diagnostic_recorded = True

    def _inspect_profile_state(self) -> None:
        self.profile_lock_state = self.process_resolver.profile_lock_diagnostics()
        self.profile_lock_detected = any(self.profile_lock_state.values())
        self.existing_profile_chrome = self.process_resolver.profile_process_diagnostics()
        self.default_chrome_present = self.process_resolver.default_chrome_present()
        self._record_timeline(
            "preflight process scan",
            ownedProfileChrome=len(self.existing_profile_chrome),
            profileLock=self.profile_lock_detected,
            defaultChromePresent=self.default_chrome_present,
        )

    def _inspect_and_cleanup_stale_singleton(self) -> None:
        self.singleton_before = self.singleton_inspector.inspect(cdp_reachable=self._debug_endpoint_busy)
        self.stale_profile_singleton = self.singleton_inspector.stale_from(self.singleton_before)
        if self.stale_profile_singleton:
            self._record_timeline("stale Profile Singleton confirmed", singleton=self.singleton_before)
            self.singleton_cleanup = self.singleton_inspector.cleanup_if_stale(cdp_reachable=self._debug_endpoint_busy)
            self._record_timeline(
                "stale Profile Singleton cleanup",
                removed=self.singleton_cleanup.get("removed", {}),
            )
        else:
            self.singleton_cleanup = {
                "stale": False,
                "before": self.singleton_before,
                "removed": {},
                "after": self.singleton_before,
            }
        self.singleton_after = self.singleton_cleanup.get("after", self.singleton_before)

    def _inspect_devtools_file(self) -> None:
        path = self.profile.profile_path / "DevToolsActivePort"
        try:
            self.devtools_active_port_exists = path.is_file() and not path.is_symlink()
            self.devtools_active_port_mtime = path.stat().st_mtime if self.devtools_active_port_exists else None
        except OSError:
            self.devtools_active_port_exists = False
            self.devtools_active_port_mtime = None
        state = (self.devtools_active_port_exists, self.devtools_active_port_mtime)
        if self._timeline_states.get("activePort") != state:
            self._timeline_states["activePort"] = state
            self._record_timeline(
                "DevToolsActivePort scan",
                exists=self.devtools_active_port_exists,
                mtime=self.devtools_active_port_mtime,
            )

    def _start_output_capture(self) -> None:
        if self.process is None:
            return
        if self.process.stdout is not None:
            self._stdout_capture = _SafeChromeOutputTail(self.process.stdout, name="stdout")
        if self.process.stderr is not None:
            self._stderr_capture = _SafeChromeOutputTail(self.process.stderr, name="stderr")

    def _read_devtools_active_port(self) -> tuple[int, str] | None:
        """Read only the current port and browser websocket path metadata."""
        path = self.profile.profile_path / "DevToolsActivePort"
        self._inspect_devtools_file()
        try:
            if not self.devtools_active_port_exists:
                return None
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return None
        if len(lines) < 2:
            return None
        try:
            port = int(lines[0].strip())
        except ValueError:
            return None
        websocket_path = lines[1].strip()
        if not 1 <= port <= 65535 or not websocket_path.startswith("/devtools/browser/"):
            return None
        return port, websocket_path

    def _discover_cdp_endpoint(self) -> bool:
        owned = self.process_resolver.find()
        if owned is None:
            return False
        metadata = self._read_devtools_active_port()
        if metadata is None:
            return False
        port, websocket_path = metadata
        if not self._debug_endpoint_busy(port):
            return False
        self.port = port
        self.devtools_websocket_path = websocket_path
        self.cdp_endpoint_discovered = True
        self.owned_browser_pid = owned.pid
        self.devtools_active_port_port = port
        self.cdp_endpoint_reachable = True
        self._record_timeline("owned browser and CDP detected", ownedBrowserPid=owned.pid, port=port)
        return True

    def _owned_profile_pid(self) -> int | None:
        browser = self.process_resolver.find()
        return browser.pid if browser else None

    def has_existing_instance(self) -> bool:
        return self._owned_profile_process_exists()

    def _clear_stale_devtools_file(self) -> None:
        """Retain stale endpoint evidence during forensics; never delete it here."""
        self._inspect_devtools_file()
        if self.devtools_active_port_exists and not self._owned_profile_process_exists():
            self.stale_devtools_active_port = True
            self._record_timeline("stale DevToolsActivePort observed", mtime=self.devtools_active_port_mtime)

    def start(self) -> None:
        self.set_stage(ChromeBrainStartupStage.CHECKING_CHROME)
        self.cdp_connected = False
        # Never reuse a previous dynamic port. DevToolsActivePort is the only
        # authority for the current Chrome instance.
        self.port = None
        self.devtools_websocket_path = None
        self.cdp_endpoint_discovered = False
        self.devtools_active_port_exists = False
        self.devtools_active_port_port = None
        self.devtools_active_port_mtime = None
        self.stale_devtools_active_port = False
        self.cdp_endpoint_reachable = False
        self.chrome_process_detected = False
        self.chrome_pid = None
        self.profile_lock_state = {}
        self.singleton_before = {}
        self.singleton_cleanup = {}
        self.singleton_after = {}
        self.stale_profile_singleton = False
        self.existing_profile_chrome = []
        self.default_chrome_present = False
        self.chrome_stdout_tail = []
        self.chrome_stderr_tail = []
        self.chrome_exit_code = None
        self.chrome_exit_signal = None
        self.chrome_runtime_ms = None
        self.launch_args_safe = []
        self.launch_cwd = None
        self.launcher_type = None
        self.chrome_start_timeline = []
        self._timeline_started_at = time.monotonic()
        self._timeline_states = {}
        self._exit_diagnostic_recorded = False
        self._inspect_profile_state()
        self._clear_stale_devtools_file()
        self._inspect_and_cleanup_stale_singleton()
        if self.process is not None and self.process.poll() is None:
            self.chrome_process_detected = True
            self.chrome_pid = self.process.pid
            self._record_timeline("existing launch child still running", launchPid=self.process.pid)
            return
        executable = self.executable or find_google_chrome()
        self.chrome_executable = executable
        existing = self.process_resolver.find()
        if existing is not None:
            self.set_stage(ChromeBrainStartupStage.ATTACHING_CHROME)
            self._adopted = True
            self.chrome_process_detected = True
            self.owned_browser_pid = existing.pid
            self.chrome_pid = existing.pid
            self.started_at = time.time()
            self._record_timeline(
                "existing Dedicated Chrome detected",
                ownedBrowserPid=existing.pid,
                remoteDebuggingArgumentPresent=existing.has_remote_debugging,
            )
            if not existing.has_remote_debugging:
                self.set_stage(ChromeBrainStartupStage.FAILED, failure_code="EXISTING_PROFILE_INSTANCE_WITHOUT_CDP")
                raise ChromeBrainError("EXISTING_PROFILE_INSTANCE_WITHOUT_CDP")
            if self._discover_cdp_endpoint():
                return
            self.set_stage(ChromeBrainStartupStage.CHECKING_CHROME, failure_code="EXISTING_PROFILE_CDP_NOT_READY")
            return
        if not executable:
            self.set_stage(ChromeBrainStartupStage.FAILED, failure_code="CHROME_EXECUTABLE_NOT_FOUND")
            raise ChromeBrainError("GOOGLE_CHROME_NOT_FOUND")
        self.set_stage(ChromeBrainStartupStage.STARTING_CHROME)
        self._clear_stale_devtools_file()
        profile_path = self.profile.create()
        chrome_args = [
            f"--user-data-dir={profile_path}",
            f"--remote-debugging-address={CHROME_DEBUG_HOST}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "--enable-logging=stderr",
            CHATGPT_URL,
        ]
        if sys.platform == "darwin" and Path("/usr/bin/open").is_file():
            # `open -n` creates a new Chrome application instance instead of
            # handing the request to the already-running ordinary Chrome app.
            command = ["/usr/bin/open", "-n", "-a", "Google Chrome", "--args", *chrome_args]
            self.launcher_type = "MACOS_OPEN_NEW_INSTANCE"
        else:
            command = [executable, *chrome_args]
            self.launcher_type = "DIRECT_EXECUTABLE_FALLBACK"
        self._record_timeline(
            "Chrome spawn",
            executable=executable,
            launcherType=self.launcher_type,
            launchArgs=command,
        )
        self.launch_args_safe = command
        self.launch_cwd = str(Path.cwd())
        self._append_launch_log(
            f"CHROME_LAUNCH_ARGS_SAFE launcherType={self.launcher_type} executable={executable} "
            f"cwd={Path.cwd()} args={command}"
        )
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._adopted = False
        self.chrome_process_detected = False
        self.chrome_pid = None
        self.started_at = time.time()
        self.launch_pid = self.process.pid
        self.launch_process_exited = False
        self.launch_exit_code = None
        self.owned_browser_pid = None
        self.profile_lock_detected = self.process_resolver.profile_lock_detected()
        self._record_timeline("Chrome child spawned", launchPid=self.launch_pid)
        self._start_output_capture()

    def start_or_attach(self) -> None:
        """Idempotently start our profile or attach to its live process."""
        self.start()

    # Keep the lifecycle name used by the architecture notes available to
    # integration code while retaining Python's snake_case API.
    def startOrAttach(self) -> None:
        self.start_or_attach()

    def wait_until_ready(self, timeout_seconds: float = 10.0) -> bool:
        self.set_stage(ChromeBrainStartupStage.DISCOVERING_CDP)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                self._capture_process_exit()
                owned = self.process_resolver.find()
                if owned is not None:
                    self.browser_handoff_detected = True
                    self._adopted = True
                    self.owned_browser_pid = owned.pid
                    self.chrome_process_detected = True
                    self.chrome_pid = owned.pid
                    self._record_timeline("browser handoff detected", ownedBrowserPid=owned.pid)
                elif not self._adopted:
                    if not self._timeline_states.get("noOwnedBrowser"):
                        self._timeline_states["noOwnedBrowser"] = True
                        self._record_timeline("launcher exited; waiting for Dedicated Chrome")
            if self._adopted and not self._owned_profile_process_exists():
                self._record_timeline("owned browser process exit")
                return False
            if self._discover_cdp_endpoint():
                return True
            time.sleep(0.1)
        self._record_timeline("DevToolsActivePort wait timeout", timeoutMs=int(timeout_seconds * 1000))
        return False

    def is_alive(self) -> bool:
        if self.process is not None and self.process.poll() is not None:
            self._capture_process_exit()
            owned = self.process_resolver.find()
            if owned is not None:
                self.browser_handoff_detected = True
                self._adopted = True
                self.owned_browser_pid = owned.pid
                self.chrome_process_detected = True
                self.chrome_pid = owned.pid
        process_alive = (self._adopted and self._owned_profile_process_exists()) or (self.process is not None and self.process.poll() is None)
        return process_alive and self._discover_cdp_endpoint()

    def cdp_failure_code(self) -> str:
        """Classify launch failure only after process/ActivePort/handoff scans."""
        if self.launcher_type == "MACOS_OPEN_NEW_INSTANCE":
            if self.stale_profile_singleton and any(self.singleton_cleanup.get("removed", {}).values()) and self.process_resolver.find() is None:
                return "DEDICATED_CHROME_START_FAILED_AFTER_SINGLETON_CLEANUP"
            if self.launch_process_exited and self.chrome_exit_code not in (None, 0):
                return "CHROME_APPLICATION_LAUNCH_FAILED"
            if self.process_resolver.find() is None:
                return "DEDICATED_CHROME_NOT_STARTED"
            if not self.devtools_active_port_exists:
                return "DEVTOOLS_START_TIMEOUT"
            return "CDP_CONNECTION_FAILED"
        return "CDP_ENDPOINT_NOT_FOUND"

    def diagnostics(self) -> dict[str, Any]:
        current_profile_chrome = self.process_resolver.profile_process_diagnostics()
        current_default_chrome = self.process_resolver.default_chrome_present()
        current_locks = self.process_resolver.profile_lock_diagnostics()
        return {
            "startupStage": self.startup_stage,
            "chromeProcessDetected": self.chrome_process_detected,
            "chromePid": self.chrome_pid,
            "chromeExecutable": self.chrome_executable,
            "dedicatedProfilePath": str(self.profile.profile_path),
            "cdpEndpointDiscovered": self.cdp_endpoint_discovered,
            "cdpConnected": self.cdp_connected,
            "lastFailureCode": self.last_failure_code,
            "lastFailureStage": self.last_failure_stage,
            "runtimeId": self.runtime_id,
            "startedAt": self.started_at,
            "launchPid": self.launch_pid,
            "launchProcessExited": self.launch_process_exited,
            "launchExitCode": self.launch_exit_code,
            "chromeExitCode": self.chrome_exit_code,
            "chromeExitSignal": self.chrome_exit_signal,
            "chromeRuntimeMs": self.chrome_runtime_ms,
            "chromeLaunchArgsSafe": self.launch_args_safe,
            "chromeLaunchCwd": self.launch_cwd,
            "launcherType": self.launcher_type,
            "chromeStdoutTail": self.chrome_stdout_tail[-100:],
            "chromeStderrTail": self.chrome_stderr_tail[-100:],
            "chromeStartTimeline": self.chrome_start_timeline,
            "ownedChromeProcessDetected": self.process_resolver.find() is not None,
            "ownedBrowserPid": self.owned_browser_pid or self._owned_profile_pid(),
            "existingDedicatedChromeDetected": self.process_resolver.find() is not None,
            "existingProfileChrome": current_profile_chrome or self.existing_profile_chrome,
            "defaultChromePresent": current_default_chrome,
            "devToolsActivePortExists": self.devtools_active_port_exists,
            "devToolsActivePortPort": self.devtools_active_port_port,
            "devToolsActivePortMtime": self.devtools_active_port_mtime,
            "staleDevToolsActivePort": self.stale_devtools_active_port,
            "cdpEndpointReachable": self.cdp_endpoint_reachable,
            "browserHandoffDetected": self.browser_handoff_detected,
            "profileLockDetected": any(current_locks.values()),
            "profileLockState": current_locks or self.profile_lock_state,
            "staleProfileSingleton": self.stale_profile_singleton,
            "singletonBefore": self.singleton_before,
            "singletonCleanup": self.singleton_cleanup,
            "singletonAfter": self.singleton_after,
        }

    def bring_to_front(self) -> None:
        # This only activates the application window; it does not interact with
        # the page or any credential-bearing control.
        if shutil.which("osascript"):
            subprocess.run(["osascript", "-e", 'tell application "Google Chrome" to activate'], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        self.process_resolver.gracefully_stop()
        process, self.process = self.process, None
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
        self._adopted = False
        self.chrome_process_detected = False
        self.owned_browser_pid = None


class ChromeWebBrainTransport(BrowserController):
    """DOM-only transport connected to the app-owned Chrome over CDP."""

    def __init__(self, runtime: ChromeBrainRuntime | None = None, *, playwright_factory: Callable[[], Any] | None = None) -> None:
        self.runtime = runtime or ChromeBrainRuntime()
        self._playwright_factory = playwright_factory
        self.playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self.conversation_id: str | None = None
        self._baseline_assistant_count = 0
        self.target_resolver = ChatGPTTargetResolver()
        self._context_page_handler: Callable[[Any], None] | None = None

    def connect(self) -> None:
        self.runtime.start_or_attach()
        self.runtime.set_stage(ChromeBrainStartupStage.DISCOVERING_CDP)
        if not self.runtime.wait_until_ready():
            failure_code = self.runtime.cdp_failure_code()
            self.runtime.set_stage(ChromeBrainStartupStage.FAILED, failure_code=failure_code)
            raise ChromeBrainError(failure_code)
        if self.browser is not None:
            return
        if self._playwright_factory is not None:
            self.playwright = self._playwright_factory()
        else:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as exc:
                self.runtime.set_stage(ChromeBrainStartupStage.FAILED, failure_code="PLAYWRIGHT_NOT_INSTALLED")
                raise ChromeBrainError("PLAYWRIGHT_NOT_INSTALLED") from exc
            self.playwright = sync_playwright().start()
        try:
            self.runtime.set_stage(ChromeBrainStartupStage.CONNECTING_CDP)
            self.browser = self.playwright.chromium.connect_over_cdp(self.runtime.endpoint)
            self.runtime.cdp_connected = True
            contexts = self.browser.contexts
            self.context = contexts[0] if contexts else None
            if self.context is None:
                self.runtime.set_stage(ChromeBrainStartupStage.FAILED, failure_code="CHROME_CDP_CONTEXT_UNAVAILABLE")
                raise ChromeBrainError("CHROME_CDP_CONTEXT_UNAVAILABLE")
            self.runtime.set_stage(ChromeBrainStartupStage.DISCOVERING_TARGETS)
            self._install_target_watchers()
            self.page = self.target_resolver.resolve(list(self.context.pages))
            if self.page is None:
                self.runtime.set_stage(ChromeBrainStartupStage.BINDING_CHATGPT)
                self.page = self.context.new_page()
                self.page.goto(CHATGPT_URL, wait_until="domcontentloaded")
                self.page = self._resolve_target()
            else:
                self.runtime.set_stage(ChromeBrainStartupStage.BINDING_CHATGPT)
        except Exception as exc:
            if self.runtime.startup_stage != ChromeBrainStartupStage.FAILED:
                self.runtime.set_stage(ChromeBrainStartupStage.FAILED, failure_code=type(exc).__name__)
            self.close()
            raise

    def _install_target_watchers(self) -> None:
        """Invalidate target references as CDP pages are created/navigated/closed."""
        if self.context is None:
            return

        def register(page: Any) -> None:
            for event in ("framenavigated", "close"):
                try:
                    page.on(event, lambda *_args: self._invalidate_target())
                except Exception:
                    pass

        self._context_page_handler = register
        for page in list(self.context.pages):
            register(page)
        try:
            # Playwright's page event is the CDP equivalent of targetcreated.
            self.context.on("page", register)
        except Exception:
            pass

    def _invalidate_target(self) -> None:
        self.page = None

    def _resolve_target(self) -> Any:
        if self.context is None:
            raise ChromeBrainError("CHROME_CDP_CONTEXT_UNAVAILABLE")
        page = self.target_resolver.resolve(list(self.context.pages))
        if page is None:
            raise ChromeBrainError("CHATGPT_TARGET_NOT_FOUND")
        self.page = page
        return page

    def _require_page(self) -> Any:
        if not self.runtime.is_alive():
            raise ChromeBrainError("CHROME_PROCESS_LOST")
        return self._resolve_target()

    def auth_status(self) -> str:
        try:
            page = self._require_page()
            self.runtime.set_stage(ChromeBrainStartupStage.CHECKING_DOM)
            return self.target_resolver.dom.status(page)
        except ChromeBrainError as exc:
            if str(exc) == "CHATGPT_TARGET_NOT_FOUND":
                self.runtime.set_stage(ChromeBrainStartupStage.FAILED, failure_code="CHATGPT_TARGET_NOT_FOUND")
            raise
        except Exception:
            return "DOM_UNKNOWN"

    def open_conversation(self, conversation_id: str | None = None) -> str:
        page = self._require_page()
        if conversation_id:
            if not re.fullmatch(r"[A-Za-z0-9-]+", conversation_id):
                raise ChromeBrainError("INVALID_CONVERSATION_BINDING")
            self.conversation_id = conversation_id
            page.goto(f"https://chatgpt.com/c/{conversation_id}", wait_until="domcontentloaded")
        else:
            page.goto(CHATGPT_URL, wait_until="domcontentloaded")
            self.conversation_id = None
        self._resolve_target()
        return page.url

    def send_prompt(self, prompt: str) -> None:
        page = self._require_page()
        if self.auth_status() != "AUTHENTICATED":
            raise ChromeBrainError("AUTH_REQUIRED")
        composer = self.target_resolver.dom.find_composer(page)
        if composer is None:
            raise ChromeBrainError("COMPOSER_NOT_FOUND")
        self._baseline_assistant_count = page.locator('[data-message-author-role="assistant"]').count()
        composer.fill(prompt)
        composer.press("Enter")

    def wait_for_completion(self, timeout_seconds: float) -> None:
        page = self._require_page()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.auth_status() != "AUTHENTICATED":
                raise ChromeBrainError("CHROME_BRAIN_AUTH_CHANGED")
            stop = page.get_by_role("button", name=re.compile(r"stop generating|停止生成", re.I))
            if stop.count() == 0:
                assistant_count = page.locator('[data-message-author-role="assistant"]').count()
                if assistant_count > self._baseline_assistant_count:
                    return
            time.sleep(0.25)
        raise TimeoutError("ChatGPT Web response timeout")

    def read_response(self) -> str:
        page = self._require_page()
        articles = page.locator('[data-message-author-role="assistant"]')
        if articles.count() <= self._baseline_assistant_count:
            raise ChromeBrainError("RESPONSE_NOT_FOUND")
        return articles.last.inner_text()

    def close(self) -> None:
        try:
            if self.browser is not None:
                self.browser.close()
        finally:
            self.browser = None
            self.context = None
            self.page = None
            self._context_page_handler = None
            if self.playwright is not None and self._playwright_factory is None:
                self.playwright.stop()
            self.playwright = None


class ChromeWebBrainHost(BrowserController):
    """Lifecycle/state facade used by Product Shell and the Brain adapter."""

    def __init__(self, transport: ChromeWebBrainTransport | None = None, *, on_failure: Callable[[str, str], None] | None = None) -> None:
        self.transport = transport or ChromeWebBrainTransport()
        self.on_failure = on_failure
        self.state = "DISCONNECTED"
        self.auth_state = "UNKNOWN"
        self.dom_recognized = False
        self.conversation_binding_id: str | None = None
        self.last_error: str | None = None
        self._last_failure: tuple[str, str] | None = None
        self.target_diagnostics: list[dict[str, Any]] = []

    def _fail(self, state: str, reason: str) -> None:
        self.state = state
        self.last_error = reason
        signature = (state, reason)
        if signature != self._last_failure and self.on_failure is not None:
            self._last_failure = signature
            self.on_failure(state, reason)

    def _apply_health(self, auth_state: str) -> str:
        self.auth_state = auth_state
        self.dom_recognized = auth_state == "AUTHENTICATED"
        self.conversation_binding_id = self.transport.conversation_id
        if auth_state == "AUTHENTICATED":
            self.transport.runtime.set_stage(ChromeBrainStartupStage.READY)
            self.state = "READY"
            self._last_failure = None
        elif auth_state == "AUTH_REQUIRED":
            self._fail("AUTH_REQUIRED", auth_state)
            self.transport.runtime.bring_to_front()
        elif auth_state == "CHALLENGE_REQUIRED":
            self._fail("CHALLENGE_REQUIRED", auth_state)
            self.transport.runtime.bring_to_front()
        elif auth_state == "DOM_UNKNOWN":
            self._fail("DOM_UNKNOWN", auth_state)
        else:
            self._fail("UNKNOWN", auth_state)
        return auth_state

    def refresh(self) -> dict[str, Any]:
        if not self.transport.runtime.is_alive():
            self.auth_state = "UNKNOWN"
            self.transport.runtime.set_stage(ChromeBrainStartupStage.FAILED, failure_code="CHROME_PROCESS_LOST")
            self._fail("ERROR", "CHROME_PROCESS_LOST")
            return self.status()
        try:
            if self.transport.browser is None:
                self.transport.connect()
            self._apply_health(self.transport.auth_status())
        except ChromeBrainError as exc:
            self._fail("ERROR", str(exc))
        self.target_diagnostics = self.transport.target_resolver.diagnostics()
        return self.status()

    def start(self) -> None:
        self.state = "STARTING"
        self.transport.runtime.set_stage(ChromeBrainStartupStage.BOOT_INIT)
        try:
            self.transport.connect()
            self._apply_health(self.transport.auth_status())
            self.target_diagnostics = self.transport.target_resolver.diagnostics()
        except Exception as exc:
            if self.transport.runtime.startup_stage != ChromeBrainStartupStage.FAILED:
                self.transport.runtime.set_stage(ChromeBrainStartupStage.FAILED, failure_code=type(exc).__name__)
            self._fail("ERROR", getattr(exc, "args", [type(exc).__name__])[0] or type(exc).__name__)

    def connect(self) -> None:
        self.transport.connect()

    def close(self) -> None:
        self.stop()

    def open(self) -> dict[str, Any]:
        self.start()
        if self.state == "ERROR":
            raise ChromeBrainError(self.last_error or "CHROME_BRAIN_START_FAILED")
        return self.status()

    def stop(self) -> None:
        self.transport.close()
        self.transport.runtime.stop()
        self.state = "DISCONNECTED"
        self.auth_state = "UNKNOWN"

    def health_check(self) -> bool:
        self.refresh()
        return self.state == "READY" and self.auth_state == "AUTHENTICATED"

    def auth_status(self) -> str:
        self.refresh()
        return self.auth_state

    def open_conversation(self, conversation_id: str | None = None) -> str:
        result = self.transport.open_conversation(conversation_id)
        self.conversation_binding_id = self.transport.conversation_id
        return result

    def send_prompt(self, prompt: str) -> None:
        self.transport.send_prompt(prompt)

    def wait_for_completion(self, timeout_seconds: float) -> None:
        self.transport.wait_for_completion(timeout_seconds)

    def read_response(self) -> str:
        return self.transport.read_response()

    def status(self) -> dict[str, Any]:
        runtime_diagnostics = self.transport.runtime.diagnostics()
        selected = self.transport.target_resolver.selected_diagnostics()
        targets = self.target_diagnostics
        return {
            "state": self.state,
            "authState": self.auth_state,
            "domRecognized": self.dom_recognized,
            "conversationBindingId": self.conversation_binding_id,
            "browserRuntime": "Google Chrome",
            "profilePersistent": True,
            "browserAlive": self.transport.runtime.is_alive(),
            "lastError": self.last_error,
            "targets": self.target_diagnostics,
            "pageTargetCount": len(targets),
            "chatgptTargetCount": sum(1 for target in targets if target.get("eligibleChatGPT")),
            **runtime_diagnostics,
            **selected,
        }


__all__ = [
  "CHATGPT_URL", "CHROME_BRAIN_PROFILE", "CHROME_DEBUG_HOST", "CHROME_DEBUG_PORT",
    "ChromeBrainStartupStage",
    "ChatGPTDomFingerprint", "ChatGPTTargetMetadata", "ChatGPTChromeDomAdapter", "ChatGPTTargetResolver",
    "ChromeBrainError", "ChromeBrainProfileManager", "DedicatedChromeProcess", "DedicatedChromeProcessResolver", "ChromeBrainRuntime",
    "ChromeWebBrainTransport", "ChromeWebBrainHost", "find_google_chrome",
]
