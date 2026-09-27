"""Semantic Playwright controller for the dedicated ChatGPT Web profile."""

from __future__ import annotations

import re
import time
from typing import Any

from .browser_profile import BrowserProfileManager
from .chatgpt_web import BrowserController


class DedicatedChatGPTBrowserController(BrowserController):
    """BrowserController backed by an app-owned headed persistent context.

    Selectors are limited to roles/names and the bound conversation URL. This
    class does not inspect cookies, storage state, headers, or browser DBs.
    """

    def __init__(self, profile: BrowserProfileManager, *, base_url: str = "https://chatgpt.com/") -> None:
        self.profile = profile
        self.base_url = base_url
        self.context: Any = None
        self.page: Any = None
        self.conversation_id: str | None = None

    def connect(self) -> None:
        self.context = self.profile.launch(headless=False)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.goto(self.base_url, wait_until="domcontentloaded")

    def auth_status(self) -> str:
        return self.profile.auth_check(self.page) if self.page is not None else "UNKNOWN"

    def open_conversation(self, conversation_id: str | None = None) -> str:
        if self.page is None:
            raise RuntimeError("browser is not connected")
        if conversation_id:
            if not re.fullmatch(r"[A-Za-z0-9-]+", conversation_id):
                raise ValueError("invalid conversation binding")
            self.conversation_id = conversation_id
            self.page.goto(f"https://chatgpt.com/c/{conversation_id}", wait_until="domcontentloaded")
            return conversation_id
        self.page.goto(self.base_url, wait_until="domcontentloaded")
        self.conversation_id = None
        return self.page.url

    def send_prompt(self, prompt: str) -> None:
        if self.page is None:
            raise RuntimeError("browser is not connected")
        textbox = self.page.get_by_role("textbox").last
        textbox.fill(prompt)
        textbox.press("Enter")

    def wait_for_completion(self, timeout_seconds: float) -> None:
        if self.page is None:
            raise RuntimeError("browser is not connected")
        deadline = time.monotonic() + timeout_seconds
        stop_names = re.compile(r"stop generating|停止生成", re.I)
        while time.monotonic() < deadline:
            if self.page.get_by_role("button", name=stop_names).count() == 0:
                return
            time.sleep(0.25)
        raise TimeoutError("ChatGPT Web response timeout")

    def read_response(self) -> str:
        if self.page is None:
            raise RuntimeError("browser is not connected")
        articles = self.page.get_by_role("article")
        if articles.count() == 0:
            raise RuntimeError("ChatGPT Web response not found")
        return articles.last.inner_text()

    def close(self) -> None:
        self.profile.close()
        self.context = None
        self.page = None
