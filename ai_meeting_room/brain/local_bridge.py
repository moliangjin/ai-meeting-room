"""Loopback Brain Bridge and explicit extension pairing state."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping


class LocalBrainBridgeError(RuntimeError):
    pass


@dataclass
class _PairingTicket:
    code_hash: str
    meeting_id: str
    expires_at: float
    used: bool = False


@dataclass
class _BridgeSession:
    token_hash: str
    connection_id: str
    meeting_id: str
    last_seen_at: float
    tab_id: int | None = None
    conversation_id: str | None = None
    auth_state: str = "UNKNOWN"
    dom_recognized: bool = False
    pending: dict[str, Any] | None = None
    delivered_request_id: str | None = None
    response: dict[str, Any] | None = None
    error: str | None = None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class LocalBrainBridge:
    """In-memory localhost state; credentials are never written to disk."""

    def __init__(self, *, pairing_ttl_seconds: float = 300.0, heartbeat_timeout_seconds: float = 30.0) -> None:
        self.pairing_ttl_seconds = pairing_ttl_seconds
        self.heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._tickets: dict[str, _PairingTicket] = {}
        self._sessions: dict[str, _BridgeSession] = {}
        self._seen_request_ids: set[str] = set()
        self._lock = threading.RLock()

    def create_pairing(self, meeting_id: str) -> dict[str, Any]:
        if not meeting_id:
            raise LocalBrainBridgeError("meetingId is required")
        code = f"{secrets.randbelow(10**8):08d}"
        ticket = _PairingTicket(_hash(code), meeting_id, time.time() + self.pairing_ttl_seconds)
        with self._lock:
            self._tickets[ticket.code_hash] = ticket
        return {"pairingCode": code, "expiresAt": ticket.expires_at}

    def pair(self, pairing_code: str, connection_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"\d{8}", pairing_code) or not connection_id:
            raise LocalBrainBridgeError("invalid pairing request")
        with self._lock:
            ticket = self._tickets.get(_hash(pairing_code))
            if ticket is None or ticket.used or ticket.expires_at < time.time():
                raise LocalBrainBridgeError("pairing code expired or invalid")
            ticket.used = True
            token = secrets.token_urlsafe(32)
            self._sessions[_hash(token)] = _BridgeSession(_hash(token), connection_id, ticket.meeting_id, time.time())
            return {"bridgeToken": token, "meetingId": ticket.meeting_id}

    def _session(self, token: str) -> _BridgeSession:
        if not token:
            raise LocalBrainBridgeError("missing bridge token")
        with self._lock:
            session = self._sessions.get(_hash(token))
            if session is None:
                raise LocalBrainBridgeError("invalid bridge session")
            if time.time() - session.last_seen_at > self.heartbeat_timeout_seconds:
                raise LocalBrainBridgeError("bridge heartbeat timeout")
            return session

    def bind(self, token: str, *, meeting_id: str, tab_id: int, conversation_id: str) -> dict[str, Any]:
        if not isinstance(tab_id, int) or tab_id < 0 or not re.fullmatch(r"[A-Za-z0-9-]+", conversation_id):
            raise LocalBrainBridgeError("invalid conversation binding")
        session = self._session(token)
        if meeting_id != session.meeting_id:
            raise LocalBrainBridgeError("meeting binding mismatch")
        with self._lock:
            session.tab_id = tab_id
            session.conversation_id = conversation_id
            session.last_seen_at = time.time()
        return self.status(token)

    def heartbeat(self, token: str, metadata: Mapping[str, Any]) -> dict[str, Any]:
        session = self._session(token)
        with self._lock:
            session.last_seen_at = time.time()
            if isinstance(metadata.get("authState"), str):
                session.auth_state = metadata["authState"]
            if isinstance(metadata.get("domRecognized"), bool):
                session.dom_recognized = metadata["domRecognized"]
            if session.tab_id is not None and isinstance(metadata.get("tabId"), int) and metadata["tabId"] != session.tab_id:
                session.dom_recognized = False
            if session.conversation_id and isinstance(metadata.get("conversationBindingId"), str) and metadata["conversationBindingId"] != session.conversation_id:
                session.dom_recognized = False
        return self.status(token)

    def status(self, token: str) -> dict[str, Any]:
        session = self._session(token)
        return self._status_for_session(session)

    @staticmethod
    def _status_for_session(session: _BridgeSession) -> dict[str, Any]:
        return {
            "paired": True,
            "bound": session.tab_id is not None and session.conversation_id is not None,
            "meetingId": session.meeting_id,
            "tabId": session.tab_id,
            "conversationBindingId": session.conversation_id,
            "authState": session.auth_state,
            "domRecognized": session.dom_recognized,
            "responseReady": session.response is not None,
            "error": session.error,
            "lastSeenAt": session.last_seen_at,
        }

    def status_for_meeting(self, meeting_id: str) -> dict[str, Any]:
        """Return non-secret status for the Product Shell's bound session.

        This is deliberately an in-process boundary.  It never returns the
        extension token; the extension remains the only caller of the
        token-authenticated HTTP endpoints.
        """
        with self._lock:
            sessions = [session for session in self._sessions.values() if session.meeting_id == meeting_id]
            if not sessions:
                raise LocalBrainBridgeError("extension is not paired")
            session = max(sessions, key=lambda item: item.last_seen_at)
            if time.time() - session.last_seen_at > self.heartbeat_timeout_seconds:
                raise LocalBrainBridgeError("bridge heartbeat timeout")
            return self._status_for_session(session)

    def _queue_request(self, session: _BridgeSession, request: Mapping[str, Any]) -> None:
        required = ("brainRequestId", "meetingId", "taskId", "prompt")
        if any(not isinstance(request.get(key), str) or not request[key] for key in required):
            raise LocalBrainBridgeError("invalid BrainRequest")
        if request["meetingId"] != session.meeting_id or session.tab_id is None or session.conversation_id is None:
            raise LocalBrainBridgeError("Brain conversation is not bound")
        request_id = request["brainRequestId"]
        with self._lock:
            if request_id in self._seen_request_ids:
                raise LocalBrainBridgeError("duplicate BrainRequest")
            if session.pending or session.response:
                raise LocalBrainBridgeError("another BrainRequest is active")
            self._seen_request_ids.add(request_id)
            session.pending = dict(request)
            session.error = None
            session.last_seen_at = time.time()

    def queue_request(self, token: str, request: Mapping[str, Any]) -> None:
        session = self._session(token)
        self._queue_request(session, request)

    def queue_request_for_meeting(self, meeting_id: str, request: Mapping[str, Any]) -> dict[str, Any]:
        """Queue to the already paired/bound extension without exposing its token."""
        status = self.status_for_meeting(meeting_id)
        if not status["bound"]:
            raise LocalBrainBridgeError("extension is not bound")
        if status["error"] or status["authState"] != "AUTHENTICATED" or not status["domRecognized"]:
            raise LocalBrainBridgeError("extension Brain is not ready")
        with self._lock:
            session = max(
                (item for item in self._sessions.values() if item.meeting_id == meeting_id),
                key=lambda item: item.last_seen_at,
            )
            self._queue_request(session, request)
            return {
                "meetingId": meeting_id,
                "tabId": session.tab_id,
                "conversationBindingId": session.conversation_id,
            }

    def poll_request(self, token: str) -> dict[str, Any]:
        session = self._session(token)
        with self._lock:
            if session.pending is None:
                return {"type": "NO_EVENT"}
            request = dict(session.pending)
            session.pending = None
            session.delivered_request_id = request["brainRequestId"]
            return {"type": "BRAIN_REQUEST", **request, "conversationBindingId": session.conversation_id}

    def submit_response(self, token: str, *, brain_request_id: str, raw_response: str) -> None:
        session = self._session(token)
        if not isinstance(raw_response, str) or not raw_response or brain_request_id != session.delivered_request_id:
            raise LocalBrainBridgeError("response request mismatch")
        with self._lock:
            session.response = {"type": "BRAIN_RESPONSE", "brainRequestId": brain_request_id, "rawResponse": raw_response}
            session.last_seen_at = time.time()

    def poll_result(self, token: str) -> dict[str, Any]:
        session = self._session(token)
        return self._poll_result_for_session(session)

    def _poll_result_for_session(self, session: _BridgeSession) -> dict[str, Any]:
        with self._lock:
            if session.response is None:
                if session.error and session.delivered_request_id:
                    return {"type": "ERROR", "brainRequestId": session.delivered_request_id, "reason": session.error}
                return {"type": "NO_EVENT"}
            result = dict(session.response)
            # Keep the result available for an idempotent reconnect read; the
            # BrainRequestId guard in ChatGPTWebBrainBridge prevents resubmit.
            return result

    def poll_result_for_meeting(self, meeting_id: str, brain_request_id: str) -> dict[str, Any]:
        status = self.status_for_meeting(meeting_id)
        with self._lock:
            session = max(
                (item for item in self._sessions.values() if item.meeting_id == meeting_id),
                key=lambda item: item.last_seen_at,
            )
            result = self._poll_result_for_session(session)
            if result.get("type") in {"BRAIN_RESPONSE", "ERROR"} and result.get("brainRequestId") != brain_request_id:
                return {"type": "NO_EVENT"}
            return result

    def report_error(self, token: str, *, brain_request_id: str, reason: str) -> None:
        session = self._session(token)
        if not reason or brain_request_id != session.delivered_request_id:
            raise LocalBrainBridgeError("error request mismatch")
        with self._lock:
            session.error = reason
            session.last_seen_at = time.time()

    def unbind(self, token: str) -> None:
        session = self._session(token)
        with self._lock:
            session.tab_id = None
            session.conversation_id = None
            session.dom_recognized = False
            session.last_seen_at = time.time()


class LocalBrainBridgeServer:
    """Minimal JSON loopback server bound strictly to 127.0.0.1."""

    def __init__(self, bridge: LocalBrainBridge | None = None, *, host: str = "127.0.0.1", port: int = 9890) -> None:
        if host != "127.0.0.1":
            raise ValueError("Local Brain Bridge must bind to 127.0.0.1")
        self.bridge = bridge or LocalBrainBridge()
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        bridge = self.bridge

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def _reply(self, status: int, payload: Mapping[str, Any]) -> None:
                data = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
                origin = self.headers.get("Origin", "")
                if origin and not origin.startswith("chrome-extension://"):
                    self.send_response(403); self.end_headers(); return
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", origin or "chrome-extension://aimr-brain")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-AIMR-Bridge-Token")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers(); self.wfile.write(data)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0"))
                value = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                return value if isinstance(value, dict) else {}

            def _token(self) -> str:
                return self.headers.get("X-AIMR-Bridge-Token", "")

            def do_OPTIONS(self) -> None:
                self._reply(204, {})

            def do_GET(self) -> None:
                try:
                    token = self._token()
                    if self.path == "/health": self._reply(200, {"status": "ok", "bind": "127.0.0.1"}); return
                    if self.path == "/bridge/status": self._reply(200, bridge.status(token)); return
                    if self.path == "/bridge/poll": self._reply(200, bridge.poll_request(token)); return
                    if self.path == "/bridge/result": self._reply(200, bridge.poll_result(token)); return
                    self._reply(404, {"error": "not found"})
                except Exception as exc:
                    self._reply(400, {"error": str(exc)})

            def do_POST(self) -> None:
                try:
                    body = self._body()
                    token = self._token()
                    if self.path == "/api/brain/pairing": self._reply(200, bridge.create_pairing(str(body.get("meetingId", "")))); return
                    if self.path == "/bridge/hello": self._reply(200, bridge.pair(str(body.get("pairingCode", "")), str(body.get("connectionId", "")))); return
                    if self.path == "/bridge/bind": self._reply(200, bridge.bind(token, meeting_id=str(body.get("meetingId", "")), tab_id=body.get("tabId"), conversation_id=str(body.get("conversationBindingId", "")))); return
                    if self.path == "/bridge/heartbeat": self._reply(200, bridge.heartbeat(token, body)); return
                    if self.path == "/bridge/request": bridge.queue_request(token, body); self._reply(202, {"accepted": True}); return
                    if self.path == "/bridge/response": bridge.submit_response(token, brain_request_id=str(body.get("brainRequestId", "")), raw_response=body.get("rawResponse")); self._reply(202, {"accepted": True}); return
                    if self.path == "/bridge/error": bridge.report_error(token, brain_request_id=str(body.get("brainRequestId", "")), reason=str(body.get("reason", ""))); self._reply(202, {"accepted": True}); return
                    if self.path == "/bridge/unbind": bridge.unbind(token); self._reply(200, {"unbound": True}); return
                    self._reply(404, {"error": "not found"})
                except Exception as exc:
                    self._reply(400, {"error": str(exc)})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="local-brain-bridge")
        self._thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown(); self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None
