"""Dedicated, user-owned browser profile lifecycle for the Web Brain.

This module owns profile paths and persistent-context lifecycle. It never
reads browser databases, exports storage state, or handles credentials.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any


class BrowserProfileError(RuntimeError):
    pass


class BrowserProfileManager:
    """Manage one application-owned persistent browser profile."""

    def __init__(self, profile_id: str = "chatgpt_brain", *, data_dir: str | Path | None = None) -> None:
        if not profile_id or Path(profile_id).name != profile_id:
            raise ValueError("profile_id must be a single safe directory name")
        base = Path(data_dir).expanduser() if data_dir else Path.home() / "Library" / "Application Support" / "AI Meeting Room" / "browser_profiles"
        self.profile_id = profile_id
        self.base_dir = base
        self.profile_path = base / profile_id
        self._playwright: Any = None
        self._context: Any = None

    def create(self) -> Path:
        if self.profile_path.exists() and not self.profile_path.is_dir():
            raise BrowserProfileError("dedicated profile path is not a directory")
        self.profile_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.profile_path, 0o700)
        except OSError as exc:
            raise BrowserProfileError("cannot secure dedicated profile permissions") from exc
        return self.profile_path

    def exists(self) -> bool:
        return self.profile_path.is_dir()

    def launch(self, *, headless: bool = False, browser_type: str = "chromium") -> Any:
        if headless:
            raise BrowserProfileError("Phase 3A requires a headed browser")
        self.create()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserProfileError("Playwright is not installed") from exc
        self._playwright = sync_playwright().start()
        browser = getattr(self._playwright, browser_type, None)
        if browser is None:
            self.close()
            raise BrowserProfileError(f"unsupported browser type: {browser_type}")
        try:
            self._context = browser.launch_persistent_context(str(self.profile_path), headless=False)
        except Exception:
            self.close()
            raise
        return self._context

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        finally:
            self._context = None
            if self._playwright is not None:
                self._playwright.stop()
            self._playwright = None

    def health_check(self) -> bool:
        try:
            mode = stat.S_IMODE(self.profile_path.stat().st_mode)
            return self.profile_path.is_dir() and mode & 0o077 == 0
        except OSError:
            return False

    def auth_check(self, page: Any) -> str:
        """Return only a coarse page-state result; never inspect auth data."""
        try:
            if page.url.startswith("https://chatgpt.com/auth") or "/login" in page.url:
                return "AUTH_REQUIRED"
            textboxes = page.get_by_role("textbox")
            return "AUTHENTICATED" if textboxes.count() > 0 else "UNKNOWN"
        except Exception:
            return "UNKNOWN"
