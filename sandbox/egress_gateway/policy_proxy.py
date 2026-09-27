"""Small audited HTTP CONNECT gateway for the Phase 1.4 fixture.

It is intentionally deny-by-default and exact-host/port based. It is a POC
enforcement sidecar, not a claim of production-grade proxy completeness.
"""

from __future__ import annotations

import json
import os
import select
import socket
import sys
import threading
import time
from pathlib import Path


POLICY_PATH = Path(os.environ.get("EGRESS_POLICY", "/etc/egress/policy.json"))
AGENT_ID = os.environ.get("AI_MEETING_AGENT_ID", "unknown")


def load_rules() -> set[tuple[str, int]]:
    try:
        document = json.loads(POLICY_PATH.read_text())
        return {(str(rule["host"]), int(port)) for rule in document.get("rules", []) for port in rule.get("ports", [443])}
    except Exception as exc:
        print(json.dumps({"timestamp": time.time(), "agentId": AGENT_ID, "destination": "policy", "port": 0, "decision": "DENY", "reason": f"policy_parse_error:{type(exc).__name__}"}), flush=True)
        return set()


def audit(destination: str, port: int, decision: str, reason: str) -> None:
    print(json.dumps({"timestamp": time.time(), "agentId": AGENT_ID, "destination": destination, "port": port, "decision": decision, "reason": reason}), flush=True)


def deny(client: socket.socket, destination: str, port: int, reason: str) -> None:
    audit(destination, port, "DENY", reason)
    try:
        client.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
    finally:
        client.close()


def tunnel(client: socket.socket, upstream: socket.socket) -> None:
    sockets = [client, upstream]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 30)
            if not readable:
                continue
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (upstream if source is client else client).sendall(data)
    finally:
        client.close()
        upstream.close()


def handle(client: socket.socket, rules: set[tuple[str, int]]) -> None:
    try:
        reader = client.makefile("rb")
        first = reader.readline(8192).decode("latin1").strip()
        headers: dict[str, str] = {}
        while True:
            line = reader.readline(8192)
            if not line or line in (b"\r\n", b"\n"):
                break
            key, _, value = line.decode("latin1").partition(":")
            headers[key.lower()] = value.strip()
        parts = first.split()
        if len(parts) < 2:
            return deny(client, "invalid-request", 0, "malformed_proxy_request")
        if parts[0].upper() == "CONNECT":
            host, _, port_text = parts[1].partition(":")
            port = int(port_text or "443")
        else:
            host = headers.get("host", "").split(":", 1)[0]
            port = 443 if parts[0].upper() == "CONNECT" else 80
        if (host, port) not in rules:
            return deny(client, host or "missing-host", port, "deny-by-default")
        audit(host, port, "ALLOW", "exact-allowlist-match")
        upstream = socket.create_connection((host, port), timeout=15)
        if parts[0].upper() == "CONNECT":
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            return tunnel(client, upstream)
        request = (first + "\r\n").encode("latin1")
        for key, value in headers.items():
            request += f"{key}: {value}\r\n".encode("latin1")
        request += b"\r\n"
        upstream.sendall(request)
        return tunnel(client, upstream)
    except Exception as exc:
        audit("connection", 0, "DENY", f"gateway_error:{type(exc).__name__}")
        client.close()


def main() -> int:
    rules = load_rules()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", 3128))
        server.listen(64)
        print(json.dumps({"event": "gateway_started", "agentId": AGENT_ID, "ruleCount": len(rules)}), flush=True)
        while True:
            client, _ = server.accept()
            threading.Thread(target=handle, args=(client, rules), daemon=True).start()
    return 0


if __name__ == "__main__":
    sys.exit(main())
