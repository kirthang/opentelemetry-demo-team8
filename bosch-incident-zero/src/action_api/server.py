"""Local Action API on 127.0.0.1: the only path the agent uses to change the demo.

It performs allowlisted actions on demo services only, requires the approver and approval time with every
request (the agent's backend sends them only after the approval gate issued an ApprovedAction), and appends
every call, accepted or refused, to audit_logs/action_api.jsonl.

    python -m src.action_api [--port 8798]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ALLOWED_HOST = "127.0.0.1"
# Shop services the agent may restart. Monitoring, flagd and the proxy are never touched.
RESTARTABLE = frozenset({"ad", "cart", "checkout", "currency", "email", "fraud-detection", "frontend", "payment",
                         "product-catalog", "quote", "recommendation", "shipping", "accounting", "image-provider",
                         "valkey-cart", "kafka", "astronomy-db"})
CATALOG = {
    "RESTART_SERVICE": {"targets": sorted(RESTARTABLE), "rollback": None},
    "SET_FEATURE_FLAG": {"parameters": {"flag": "flag name", "state": "disabled | enabled"},
                         "rollback": "SET_FEATURE_FLAG with the previous state"},
    "THROTTLE_LOAD_GENERATOR": {"parameters": {"users": "target user count (default 1)"},
                                "rollback": "RESTORE_LOAD_GENERATOR"},
    "RESTORE_LOAD_GENERATOR": {"rollback": "THROTTLE_LOAD_GENERATOR"},
    "SCALE_SERVICE": {"supported": False, "reason": "needs Kubernetes; this deployment is Docker Compose"},
}
MAX_BODY = 16 * 1024


class ActionAPI:
    def __init__(self, repo_root: Path, audit_path: Path, loadgen_url: str = "http://localhost:8080/loadgen",
                 port: int = 8798) -> None:
        self.repo_root, self.audit_path, self.loadgen_url = repo_root, audit_path, loadgen_url.rstrip("/")
        self.flag_file = repo_root / "src" / "flagd" / "demo.flagd.json"
        self.state_file = audit_path.parent / "action_api_state.json"
        self._lock = threading.Lock()
        self.httpd = ThreadingHTTPServer((ALLOWED_HOST, port), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.api = self  # type: ignore[attr-defined]
        self.port = self.httpd.server_address[1]
        self.url = f"http://{ALLOWED_HOST}:{self.port}"

    def serve_forever(self) -> None:
        self.httpd.serve_forever(poll_interval=0.2)

    def audit(self, record: Dict[str, Any]) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.audit_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}, default=str) + "\n")

    def handle(self, req: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        approval = req.get("approval") or {}
        action_type = str(req.get("action_type") or "")
        target = str(req.get("target_service") or "")
        params = req.get("parameters") or {}
        if not approval.get("approver") or not approval.get("decided_at"):
            return 403, {"status": "REFUSED", "message": "no human approval attached"}
        if action_type not in CATALOG or CATALOG[action_type].get("supported") is False:
            reason = CATALOG.get(action_type, {}).get("reason", "not in the action catalog")
            return 409, {"status": "REFUSED", "message": f"{action_type}: {reason}"}
        if action_type == "RESTART_SERVICE":
            return self._restart(target)
        if action_type == "SET_FEATURE_FLAG":
            return self._set_flag(str(params.get("flag") or ""), str(params.get("state") or ""))
        if action_type == "THROTTLE_LOAD_GENERATOR":
            return self._swarm(int(params.get("users") or 1), remember=True)
        return self._swarm(self._remembered_users(), remember=False)

    def _restart(self, target: str) -> Tuple[int, Dict[str, Any]]:
        if target not in RESTARTABLE:
            return 409, {"status": "REFUSED", "message": f"'{target}' is not a restartable shop service"}
        run = subprocess.run(["docker", "restart", "-t", "10", target], capture_output=True, text=True, timeout=90)
        if run.returncode != 0:
            return 502, {"status": "FAILED", "message": f"docker restart {target} failed: {run.stderr.strip()[:300]}"}
        state = ""
        for _ in range(60):
            state = subprocess.run(["docker", "inspect", "-f",
                                    "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", target],
                                   capture_output=True, text=True, timeout=10).stdout.strip()
            if state in ("running", "running healthy"):
                break
            time.sleep(1)
        return 200, {"status": "EXECUTED", "message": f"restarted '{target}' (now {state or 'unknown'})"}

    def _set_flag(self, flag: str, state: str) -> Tuple[int, Dict[str, Any]]:
        try:
            flags = json.loads(self.flag_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return 502, {"status": "FAILED", "message": f"cannot read the flag configuration: {exc}"}
        entry = flags.get("flags", {}).get(flag)
        if entry is None:
            return 409, {"status": "REFUSED", "message": f"unknown feature flag '{flag}'"}
        variants = list(entry.get("variants", {}))
        wanted = "off" if state == "disabled" else ("on" if "on" in variants else None)
        if wanted not in variants:
            return 409, {"status": "REFUSED", "message": f"flag '{flag}' has no '{state}' variant"}
        previous = entry.get("defaultVariant")
        entry["defaultVariant"] = wanted
        with self.flag_file.open("r+", encoding="utf-8") as f:
            f.seek(0)
            f.write(json.dumps(flags, indent=2) + "\n")
            f.truncate()
        return 200, {"status": "EXECUTED", "message": f"set flag '{flag}' to '{wanted}' (was '{previous}')",
                     "previous": previous}

    def _remembered_users(self) -> int:
        try:
            return int(json.loads(self.state_file.read_text())["users"])
        except (OSError, ValueError, KeyError, TypeError):
            return 5

    def _swarm(self, users: int, remember: bool) -> Tuple[int, Dict[str, Any]]:
        try:
            if remember:
                stats = json.load(urllib.request.urlopen(f"{self.loadgen_url}/stats/requests", timeout=10))
                self.state_file.parent.mkdir(parents=True, exist_ok=True)
                self.state_file.write_text(json.dumps({"users": stats.get("user_count") or 5}))
            body = urllib.parse.urlencode({"user_count": users, "spawn_rate": max(1, users)}).encode()
            urllib.request.urlopen(urllib.request.Request(f"{self.loadgen_url}/swarm", data=body), timeout=10)
        except Exception as exc:
            return 502, {"status": "FAILED", "message": f"load generator did not accept the change: {exc}"}
        return 200, {"status": "EXECUTED", "message": f"load generator set to {users} user(s)"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "IncidentZeroActionAPI"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        pass

    @property
    def api(self) -> ActionAPI:
        return self.server.api  # type: ignore[attr-defined]

    def _json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/v1/catalog":
            return self._json(200, {"actions": CATALOG})
        if path == "/health":
            return self._json(200, {"ok": True})
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if urllib.parse.urlsplit(self.path).path != "/v1/actions":
            return self._json(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(length)) if 0 < length <= MAX_BODY else None
        except ValueError:
            req = None
        if not isinstance(req, dict):
            return self._json(400, {"status": "REFUSED", "message": "body must be a JSON object"})
        try:
            status, result = self.api.handle(req)
        except Exception as exc:
            status, result = 500, {"status": "FAILED", "message": f"{type(exc).__name__}: {exc}"}
        self.api.audit({"request": {k: req.get(k) for k in ("action_id", "operation", "action_type",
                                                             "target_service", "parameters", "approval")},
                        "http_status": status, **result})
        self._json(status, result)


def start(repo_root: Path, audit_path: Path, port: int = 8798) -> Optional[ActionAPI]:
    """Start the API in a background thread; None when the port is taken (assume it is already running)."""
    try:
        api = ActionAPI(repo_root, audit_path, port=port)
    except OSError:
        return None
    threading.Thread(target=api.serve_forever, daemon=True).start()
    return api


def main() -> None:
    from ..config import Settings

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8798)
    args = parser.parse_args()
    root = Settings().root_dir
    api = ActionAPI(root.parent, root / "audit_logs" / "action_api.jsonl", port=args.port)
    print(f"[ACTION API] listening on {api.url} (Ctrl+C to stop)", flush=True)
    try:
        api.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
