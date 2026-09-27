"""Safe diagnostics for the user-owned dedicated Chrome CDP endpoint.

This module is deliberately separate from the formal Brain adapter during the
R26 investigation.  It reads only the dedicated profile's DevToolsActivePort
file, probes the loopback TCP endpoint, and can optionally attach through the
exact WebSocket endpoint from inside the Product Shell process.  It never
reads browser storage or page/chat content and never launches or closes Chrome.
"""

from __future__ import annotations

import errno as errno_module
import os
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


DEDICATED_CHROME_USER_DATA_DIR = (
    Path.home() / "Library" / "Application Support" / "AI Meeting Room" / "chrome-brain-profile"
)
DEVTOOLS_ACTIVE_PORT_NAME = "DevToolsActivePort"


class DedicatedChromeEndpointResolver:
    """Resolve the current endpoint for the one formal dedicated profile."""

    def __init__(self, user_data_dir: Path | str = DEDICATED_CHROME_USER_DATA_DIR) -> None:
        self.user_data_dir = Path(user_data_dir)
        self.active_port_path = self.user_data_dir / DEVTOOLS_ACTIVE_PORT_NAME

    def resolve(self) -> "DevToolsActivePortRecord":
        return read_devtools_active_port(self.active_port_path)


@dataclass(frozen=True)
class DevToolsActivePortRecord:
    path: Path
    port: int
    ws_path: str

    @property
    def endpoint(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.ws_path}"


def _errno_name(value: int | None) -> str:
    if value is None:
        return "UNKNOWN"
    return errno_module.errorcode.get(int(value), f"ERRNO_{int(value)}")


def _safe_error_message(error: BaseException) -> str:
    # The diagnostic needs the OS classification, not a potentially verbose
    # library message.  In particular, never return an endpoint or URL value.
    if isinstance(error, TimeoutError):
        return "TCP connect timed out"
    return f"{type(error).__name__}: {_errno_name(getattr(error, 'errno', None))}"


def read_devtools_active_port(path: Path | None = None) -> DevToolsActivePortRecord:
    """Read only the exact dedicated profile DevToolsActivePort file."""
    target = Path(path) if path is not None else DEDICATED_CHROME_USER_DATA_DIR / DEVTOOLS_ACTIVE_PORT_NAME
    expected = DEDICATED_CHROME_USER_DATA_DIR / DEVTOOLS_ACTIVE_PORT_NAME
    if target != expected or target.name != DEVTOOLS_ACTIVE_PORT_NAME:
        raise ValueError("unexpected DevToolsActivePort path")
    raw = target.read_text(encoding="utf-8")
    lines = [line.strip() for line in raw.splitlines()]
    if len(lines) < 2 or not lines[0].isdigit():
        raise ValueError("DevToolsActivePort has invalid shape")
    port = int(lines[0])
    ws_path = lines[1]
    if not 1 <= port <= 65535 or not ws_path.startswith("/devtools/browser/") or "?" in ws_path or "#" in ws_path:
        raise ValueError("DevToolsActivePort has invalid endpoint shape")
    return DevToolsActivePortRecord(path=target, port=port, ws_path=ws_path)


def tcp_connect_probe(record: DevToolsActivePortRecord, *, timeout: float = 2.0) -> dict[str, Any]:
    """Perform only a bounded TCP connect to the current loopback port."""
    started = time.monotonic()
    try:
        with socket.create_connection(("127.0.0.1", record.port), timeout=timeout):
            return {
                "result": "PASS",
                "errno": None,
                "errnoName": None,
                "syscall": "connect",
                "address": "127.0.0.1",
                "port": record.port,
                "durationMs": int((time.monotonic() - started) * 1000),
            }
    except TimeoutError as exc:
        return {
            "result": "TIMEOUT",
            "errno": getattr(exc, "errno", None),
            "errnoName": _errno_name(getattr(exc, "errno", None)),
            "syscall": "connect",
            "address": "127.0.0.1",
            "port": record.port,
            "durationMs": int((time.monotonic() - started) * 1000),
            "errorMessage": _safe_error_message(exc),
        }
    except OSError as exc:
        name = _errno_name(getattr(exc, "errno", None))
        result = name if name in {"EPERM", "EACCES", "ECONNREFUSED", "ETIMEDOUT", "ENETUNREACH", "EHOSTUNREACH"} else "FAILED"
        return {
            "result": result,
            "errno": getattr(exc, "errno", None),
            "errnoName": name,
            "syscall": "connect",
            "address": "127.0.0.1",
            "port": record.port,
            "durationMs": int((time.monotonic() - started) * 1000),
            "errorMessage": _safe_error_message(exc),
        }


def _ws_error_details(error: BaseException) -> dict[str, Any]:
    raw = str(error)
    upper = raw.upper()
    name = type(error).__name__
    errno_name = next((candidate for candidate in ("EPERM", "EACCES", "ECONNREFUSED", "ETIMEDOUT", "ENETUNREACH", "EHOSTUNREACH") if candidate in upper), "UNKNOWN")
    return {
        "type": name,
        "message": f"{name}: {errno_name}" if errno_name != "UNKNOWN" else name,
        "errno": errno_name,
        "syscall": "connect",
    }


def _chatgpt_page_diagnostics(browser: Any, *, fingerprint: Callable[[Any, int], dict[str, Any]], resolver: Any) -> dict[str, Any]:
    contexts = list(getattr(browser, "contexts", []) or [])
    pages = [page for context in contexts for page in list(getattr(context, "pages", []) or [])]
    for page in pages:
        try:
            page.set_default_timeout(1500)
        except Exception:
            pass
    selected, candidates = resolver.resolve_with_diagnostics(pages, fingerprint)
    chatgpt_count = sum(1 for page in pages if resolver.eligible(page))
    selected_item = next((item for item in candidates if item.get("selectedCandidate")), None)
    auth_state = "UNKNOWN"
    composer = "UNKNOWN"
    if selected_item is not None:
        if selected_item.get("loginUiDetected"):
            auth_state = "AUTH_REQUIRED"
        elif selected_item.get("authenticatedShellDetected"):
            auth_state = "AUTHENTICATED"
        else:
            auth_state = "UNKNOWN"
        composer = "READY" if int(selected_item.get("editableComposerCount", 0) or 0) > 0 else "NOT_READY"
    return {
        "contextCount": len(contexts),
        "pageCount": len(pages),
        "chatgptPageCount": chatgpt_count,
        "chatgptPageResolution": {
            "selectedPageId": f"page-{selected_item.get('candidateIndex')}" if selected_item else None,
            "candidateCount": len(candidates),
            "candidates": [
                {
                    key: value
                    for key, value in item.items()
                    if key in {
                        "candidateIndex", "pathname", "pageTitle", "isClosed", "visibilityState",
                        "documentReadyState", "loginUiDetected", "authenticatedShellDetected",
                        "composerCandidateCount", "visibleComposerCount", "editableComposerCount",
                        "selectedCandidate", "category", "score",
                    }
                }
                for item in candidates
            ],
        },
        "authState": auth_state,
        "composerDetection": composer,
    }


def run_product_shell_exact_cdp_diagnostic() -> dict[str, Any]:
    """Run the R26 diagnostic in the caller's Product Shell process."""
    result: dict[str, Any] = {
        "ok": False,
        "productShellPid": os.getpid(),
        "productShellExecutable": sys.executable,
        "productShellTcpConnect": "NOT_RUN",
        "productShellTcpErrno": None,
        "productShellTcpSyscall": "connect",
        "productShellWsAttach": "NOT_RUN",
        "productShellWsErrorType": None,
        "productShellWsErrorMessage": None,
        "productShellWsErrno": None,
        "productShellBrowserConnected": False,
        "productShellContextCount": None,
        "productShellPageCount": None,
        "productShellChatgptPageCount": None,
        "productShellChatgptPageResolution": None,
        "productShellAuthState": "UNKNOWN",
        "productShellComposerDetection": "UNKNOWN",
        "executionContext": "PRODUCT_SHELL",
        "sensitiveProfileDataRead": False,
    }
    try:
        record = read_devtools_active_port()
        tcp = tcp_connect_probe(record, timeout=2.0)
        result["productShellTcpConnect"] = tcp["result"]
        result["productShellTcpErrno"] = tcp.get("errnoName")
        if tcp["result"] != "PASS":
            result["rootCauseClass"] = "EXECUTION_CONTEXT_PERMISSION" if tcp["result"] in {"EPERM", "EACCES"} else "CDP_TCP_UNREACHABLE"
            return result

        from playwright.sync_api import sync_playwright
        from .playwright_attached_brain import PlaywrightAttachedBrainHost, PlaywrightChatGPTPageResolver

        with sync_playwright() as runtime:
            browser = runtime.chromium.connect_over_cdp(record.endpoint, timeout=10000, is_local=True, no_defaults=True)
            try:
                result["productShellWsAttach"] = "PASS"
                result["productShellBrowserConnected"] = True
                result.update(_chatgpt_page_diagnostics(
                    browser,
                    fingerprint=PlaywrightAttachedBrainHost._page_fingerprint,
                    resolver=PlaywrightChatGPTPageResolver(),
                ))
                result["ok"] = True
            finally:
                # Do not call browser.close(): Chrome is user-owned.  The
                # Playwright client is stopped by the context manager.
                pass
    except Exception as exc:
        result["productShellWsAttach"] = "FAIL" if result["productShellTcpConnect"] == "PASS" else result["productShellWsAttach"]
        details = _ws_error_details(exc)
        result["productShellWsErrorType"] = details["type"]
        result["productShellWsErrorMessage"] = details["message"]
        result["productShellWsErrno"] = details["errno"]
        result["productShellWsSyscall"] = details["syscall"]
        result["rootCauseClass"] = "EXECUTION_CONTEXT_PERMISSION" if details["errno"] in {"EPERM", "EACCES"} else "PRODUCT_SHELL_EXACT_WS_ATTACH_FAILED"
    return result


__all__ = [
    "DEDICATED_CHROME_USER_DATA_DIR",
    "DEVTOOLS_ACTIVE_PORT_NAME",
    "DedicatedChromeEndpointResolver",
    "DevToolsActivePortRecord",
    "read_devtools_active_port",
    "tcp_connect_probe",
    "run_product_shell_exact_cdp_diagnostic",
]
