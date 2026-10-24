"""Autopilot web UI on 127.0.0.1: live incidents, the agent's work, suggested fixes and Approve / Reject.

Every state-changing request needs the per-process token embedded in the page and a same-origin Origin header.
The Grafana webhook only asks the engine to re-read alert states from Grafana; its body is never trusted.
"""
from __future__ import annotations

import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlsplit

from .engine import Decision, Engine

HOST = "127.0.0.1"
_PAGE = Path(__file__).with_name("ui.html")
WEBHOOK_PATH = "/api/alerts/grafana"
MAX_BODY = 256 * 1024
MAX_NOTE = 500


class AutopilotServer:
    def __init__(self, engine: Engine, port: int = 8790) -> None:
        self.engine = engine
        self.token = secrets.token_urlsafe(32)
        self.httpd = ThreadingHTTPServer((HOST, port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.app = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.url = f"http://localhost:{self.port}/"
        self.hosts = {f"{HOST}:{self.port}", f"localhost:{self.port}"}
        self.origins = {f"http://{h}" for h in self.hosts}
        self.webhook_hosts = self.hosts | {f"host.docker.internal:{self.port}"}

    def start(self) -> None:
        threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()

    def shutdown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    server_version = "IncidentZeroAutopilot"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass

    @property
    def app(self) -> AutopilotServer:
        return self.server.app  # type: ignore[attr-defined]

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj: Any) -> None:
        self._send(code, json.dumps(obj, default=str).encode("utf-8"), "application/json")

    def _body(self) -> Optional[Dict[str, Any]]:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self._json(413, {"error": "body too large"})
            return None
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            self._json(400, {"error": "invalid JSON"})
            return None
        if not isinstance(data, dict):
            self._json(400, {"error": "expected a JSON object"})
            return None
        return data

    def do_GET(self) -> None:
        if self.headers.get("Host") not in self.app.hosts:
            return self._json(403, {"error": "bad host"})
        path = urlsplit(self.path).path
        if path == "/":
            page = _PAGE.read_text(encoding="utf-8").replace("{{token}}", self.app.token)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/api/state":
            return self._json(200, self.app.engine.state())
        if path.startswith("/api/incidents/"):
            found = self.app.engine.incident(unquote(path[len("/api/incidents/"):]))
            return self._json(200, found) if found else self._json(404, {"error": "unknown incident"})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == WEBHOOK_PATH:
            if self.headers.get("Host") not in self.app.webhook_hosts:
                return self._json(403, {"error": "bad host"})
            self._body()
            threading.Thread(target=self.app.engine.poll, daemon=True).start()
            return self._json(200, {"ok": True})
        if self.headers.get("Host") not in self.app.hosts or self.headers.get("Origin") not in self.app.origins:
            return self._json(403, {"error": "bad host or origin"})
        data = self._body()
        if data is None:
            return None
        token = data.get("token")
        if not isinstance(token, str) or not secrets.compare_digest(token, self.app.token):
            return self._json(403, {"error": "bad token"})
        engine = self.app.engine
        if path == "/api/poll":
            threading.Thread(target=engine.poll, daemon=True).start()
            return self._json(200, {"ok": True})
        if path == "/api/killswitch":
            sentinel = engine.settings.resolve_path(engine.settings.kill_switch_file)
            if data.get("engaged") is True:
                sentinel.touch()
            elif sentinel.exists():
                sentinel.unlink()
            return self._json(200, {"kill_switch": engine.state()["kill_switch"]})
        if path.startswith("/api/incidents/"):
            rest = unquote(path[len("/api/incidents/"):])
            key, _, op = rest.rpartition("/")
            if op == "decide":
                approver = str(data.get("approver") or "").strip()[:80]
                if data.get("approve") is True and not approver:
                    return self._json(400, {"error": "enter your name to approve"})
                err = engine.decide(key, Decision(approved=data.get("approve") is True,
                                                  candidate_id=data.get("candidate_id"),
                                                  approver=approver or "operator",
                                                  note=str(data.get("note") or "")[:MAX_NOTE]))
                return self._json(409, {"error": err}) if err else self._json(200, {"ok": True})
            if op == "retry":
                ok = engine.retry(key)
                return self._json(200, {"ok": True}) if ok else self._json(409, {
                    "error": "cannot investigate again while another incident is open"})
        self._json(404, {"error": "not found"})
