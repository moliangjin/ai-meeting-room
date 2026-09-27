"""Safe, deterministic selection of an authenticated ChatGPT page.

Only bounded page metadata and boolean DOM fingerprints belong here.  This
module deliberately has no message, cookie, storage, or credential access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


SELECTION_POLICY = "READY_FIRST; AUTHENTICATED_LOADING_SECOND; AUTHENTICATED_NO_COMPOSER_THIRD; LOGIN_UI_FOURTH; DOM_UNKNOWN_LAST; TIE_INDEX"


@dataclass(frozen=True)
class ChatGPTPageFingerprint:
    """Safe per-page metadata used by the resolver and diagnostics UI."""

    candidate_index: int
    pathname: str
    page_title: str
    is_closed: bool
    visibility_state: str
    document_ready_state: str
    login_ui_detected: bool
    authenticated_shell_detected: bool
    composer_candidate_count: int
    visible_composer_count: int
    editable_composer_count: int

    @classmethod
    def from_mapping(cls, value: dict[str, Any], *, index: int | None = None) -> "ChatGPTPageFingerprint":
        return cls(
            candidate_index=int(value.get("candidateIndex", value.get("index", index or 0))),
            pathname=str(value.get("pathname", "/")),
            page_title=str(value.get("pageTitle", "UNKNOWN")),
            is_closed=bool(value.get("isClosed", False)),
            visibility_state=str(value.get("visibilityState", "UNKNOWN")),
            document_ready_state=str(value.get("documentReadyState", "UNKNOWN")),
            login_ui_detected=bool(value.get("loginUiDetected", False)),
            authenticated_shell_detected=bool(value.get("authenticatedShellDetected", False)),
            composer_candidate_count=max(0, int(value.get("composerCandidateCount", 0))),
            visible_composer_count=max(0, int(value.get("visibleComposerCount", 0))),
            editable_composer_count=max(0, int(value.get("editableComposerCount", 0))),
        )

    def as_diagnostic(self, *, selected: bool = False) -> dict[str, Any]:
        return {
            "candidateIndex": self.candidate_index,
            "pathname": self.pathname,
            "pageTitle": self.page_title,
            "isClosed": self.is_closed,
            "visibilityState": self.visibility_state,
            "documentReadyState": self.document_ready_state,
            "loginUiDetected": self.login_ui_detected,
            "authenticatedShellDetected": self.authenticated_shell_detected,
            "composerCandidateCount": self.composer_candidate_count,
            "visibleComposerCount": self.visible_composer_count,
            "editableComposerCount": self.editable_composer_count,
            "selectedCandidate": selected,
        }


# Public name used by the architecture notes; the same safe fingerprint
# carries both authentication-shell and Composer signals.
ChatGPTAuthenticatedFingerprint = ChatGPTPageFingerprint


class ChatGPTPageScorer:
    """Score pages without treating missing Composer as missing authentication."""

    @staticmethod
    def classify(fingerprint: ChatGPTPageFingerprint) -> tuple[str, str, int]:
        if fingerprint.is_closed:
            return "DOM_UNKNOWN", "DOM_UNKNOWN", 100
        ready = (
            fingerprint.authenticated_shell_detected
            and fingerprint.visible_composer_count > 0
            and fingerprint.editable_composer_count > 0
        )
        if ready:
            return "COMPOSER_READY", "AUTHENTICATED", 400
        if fingerprint.authenticated_shell_detected and fingerprint.document_ready_state != "complete":
            return "AUTHENTICATED_LOADING", "LOADING", 300
        if fingerprint.authenticated_shell_detected:
            return "AUTHENTICATED_NO_COMPOSER", "AUTHENTICATED", 250
        if fingerprint.login_ui_detected:
            return "LOGIN_UI", "AUTH_REQUIRED", 200
        return "DOM_UNKNOWN", "DOM_UNKNOWN", 100

    @classmethod
    def score(cls, fingerprint: ChatGPTPageFingerprint) -> dict[str, Any]:
        category, auth_state, score = cls.classify(fingerprint)
        if category == "COMPOSER_READY":
            failure_code = None
        elif category == "AUTHENTICATED_LOADING":
            failure_code = "PAGE_LOADING"
        elif category == "AUTHENTICATED_NO_COMPOSER":
            failure_code = "COMPOSER_NOT_FOUND"
        elif category == "LOGIN_UI":
            failure_code = "AUTH_REQUIRED"
        else:
            failure_code = "DOM_UNKNOWN"
        return {
            "category": category,
            "authState": auth_state,
            "score": score,
            "failureCode": failure_code,
            "ready": category == "COMPOSER_READY",
        }


class ChatGPTPageResolver:
    """Inspect every eligible page, then select one by a stable rule."""

    @staticmethod
    def eligible(page: Any) -> bool:
        try:
            from urllib.parse import urlsplit

            parsed = urlsplit(str(getattr(page, "url", "") or ""))
            if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
                return False
            path = parsed.path or "/"
            return path == "/" or path.startswith("/c/") or path.startswith("/g/")
        except Exception:
            return False

    def resolve_with_diagnostics(
        self,
        pages: Iterable[Any],
        fingerprint_page: Any,
    ) -> tuple[Any | None, list[dict[str, Any]]]:
        ranked: list[tuple[tuple[int, int, int, int], Any, dict[str, Any]]] = []
        diagnostics: list[dict[str, Any]] = []
        for index, page in enumerate(pages):
            if not self.eligible(page):
                continue
            fingerprint = ChatGPTPageFingerprint.from_mapping(fingerprint_page(page, index), index=index)
            scored = ChatGPTPageScorer.score(fingerprint)
            item = fingerprint.as_diagnostic()
            item.update(scored)
            diagnostics.append(item)
            # Ready/authenticated pages win.  Visibility and editable counts
            # are stable tie-breakers; the original page order is final.
            rank = (scored["score"], int(fingerprint.visibility_state == "visible"), fingerprint.editable_composer_count, -index)
            ranked.append((rank, page, item))
        if not ranked:
            return None, diagnostics
        ranked.sort(key=lambda value: value[0], reverse=True)
        selected = ranked[0][1]
        selected_index = int(ranked[0][2]["candidateIndex"])
        for item in diagnostics:
            item["selectedCandidate"] = item["candidateIndex"] == selected_index
        return selected, diagnostics

    def resolve(self, pages: list[Any]) -> Any | None:
        # Compatibility helper for callers that only need a deterministic URL
        # resolver.  The attached runtime always supplies a real fingerprint.
        def unknown_fingerprint(_page: Any, index: int) -> dict[str, Any]:
            return {"candidateIndex": index}

        selected, _ = self.resolve_with_diagnostics(pages, unknown_fingerprint)
        return selected
