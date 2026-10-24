"""Fix candidates for a diagnosis, and the local backend that applies an approved fix to the Docker demo.

Every candidate is an allowlisted RemediationAction. LocalDockerBackend inherits the ApprovedAction checks
of ActionBackend, so it runs nothing a human has not approved through ApprovalGate.
"""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import requests

from ..agent.tools import TOPOLOGY
from ..agent.transcript import Transcript
from ..config import Settings
from ..models.action import ActionType, RemediationAction, is_allowlisted
from ..remediation.backends import ActionBackend, ActionResult, AuditFn
from ..remediation.planner import LOAD_GENERATOR, NON_TARGETS, _build, plan_with_reason

THROTTLE_FACTOR = 4
# Feature flags are the organisers' fault-injection mechanism; toggling one is opt-in.
FLAG_ACTIONS_ENV = "AUTOPILOT_FLAG_ACTIONS"


def flag_actions_enabled() -> bool:
    return os.environ.get(FLAG_ACTIONS_ENV, "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Candidate:
    action: RemediationAction
    title: str
    rationale: str
    risk: str
    recommended: bool = False

    def to_dict(self) -> Dict[str, Any]:
        a, rb = self.action, self.action.rollback
        return {"id": a.action_id, "title": self.title, "rationale": self.rationale, "risk": self.risk,
                "recommended": self.recommended, "action_type": a.action_type.value,
                "target": a.target_service, "parameters": {k: v for k, v in a.parameters.items() if k != "reason"},
                "irreversible": a.irreversible,
                "rollback": None if rb is None else {"action_type": rb.action_type.value,
                                                     "target": rb.target_service, "description": rb.description}}


def _titled(action: RemediationAction) -> str:
    t, target = action.action_type, action.target_service
    if t == ActionType.RESTART_SERVICE:
        return f"Restart {target}"
    if t == ActionType.THROTTLE_LOAD_GENERATOR:
        return f"Throttle {LOAD_GENERATOR} to 1/{THROTTLE_FACTOR} of its users"
    if t == ActionType.SET_FEATURE_FLAG:
        return f"Disable feature flag '{action.parameters.get('flag')}'"
    if t == ActionType.SCALE_SERVICE:
        return f"Scale {target} by {action.parameters.get('replicas_delta', 1):+d} replica"
    return f"{t.value} on {target}"


def _risk(action: RemediationAction) -> str:
    if action.irreversible:
        return "No rollback: the container restarts and in-flight requests on it fail."
    return "Reversible: the rollback runs automatically if verification shows no recovery."


def suggest(transcript: Transcript, alert_service: str, platform: str = "docker") -> List[Candidate]:
    """The planner's action first (recommended), then other allowlisted fixes for the same incident."""
    diag = transcript.diagnosis or {}
    root = diag.get("root_cause") or {}
    service = str(root.get("service") or "").strip() or alert_service
    category = str(root.get("category") or "unknown")
    confidence = (transcript.gate or {}).get("final_confidence") or diag.get("confidence") or "low"
    out: List[Candidate] = []
    seen = set()

    def add(action: Optional[RemediationAction], rationale: str, recommended: bool = False) -> None:
        if action is None or not is_allowlisted(action.action_type):
            return
        if action.action_type == ActionType.SET_FEATURE_FLAG and not flag_actions_enabled():
            return
        if action.action_type == ActionType.SCALE_SERVICE and platform != "kubernetes":
            return
        sig = (action.action_type, action.target_service, json.dumps(action.parameters.get("flag")))
        if sig in seen:
            return
        seen.add(sig)
        action.action_id = f"ACT-{transcript.incident_id}-{len(out) + 1}"
        if action.rollback is not None:
            action.rollback.action_id = f"{action.action_id}-RB"
            action.rollback.parameters["rollback_of"] = action.action_id
        out.append(Candidate(action, _titled(action), rationale, _risk(action), recommended))

    plan = plan_with_reason(transcript, platform)
    if plan.action is not None:
        add(plan.action, plan.reason, recommended=True)

    restartable = [s for s in dict.fromkeys([service, alert_service]) if s in TOPOLOGY and s not in NON_TARGETS
                   and s != LOAD_GENERATOR]
    for i, svc in enumerate(restartable):
        why = (f"{category} diagnosed on '{svc}' ({confidence} confidence): a restart clears stuck connections, "
               f"leaked memory or a wedged process." if svc == service and diag else
               f"'{svc}' is the service the alert fired on; a restart is the generic first response.")
        add(_build(transcript, ActionType.RESTART_SERVICE, svc, why), why, recommended=not out and i == 0)

    if category in ("traffic_spike", "cpu_saturation", "latency_degradation", "memory_growth") or not diag:
        why = (f"Reduce synthetic load from {LOAD_GENERATOR} so '{service}' has headroom while the root cause "
               f"is fixed; it is restored automatically if this does not help.")
        add(_build(transcript, ActionType.THROTTLE_LOAD_GENERATOR, LOAD_GENERATOR, why), why)
    return out


Runner = Callable[[List[str]], str]


def _docker(args: List[str]) -> str:
    done = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise RuntimeError((done.stderr or done.stdout).strip()[:300] or f"docker exited {done.returncode}")
    return done.stdout.strip()


class LocalDockerBackend(ActionBackend):
    """Applies approved fixes to the local Docker Compose demo: docker restart, the Locust API, flagd-ui."""

    name = "local_docker"
    dry_run = False

    def __init__(self, settings: Settings, audit: Optional[AuditFn] = None, docker: Runner = _docker,
                 http: Optional[requests.Session] = None) -> None:
        super().__init__(audit)
        self.base = settings.frontend_proxy_url.rstrip("/")
        self.docker = docker
        self.http = http or requests.Session()
        self.saved_users: Dict[str, int] = {}

    def _result(self, action: RemediationAction, role: str, message: str, request: Dict[str, Any]) -> ActionResult:
        return ActionResult(status="EXECUTED", action_id=action.action_id, action_type=action.action_type.value,
                            target_service=action.target_service, role=role, dry_run=False, backend=self.name,
                            message=message, request=request)

    def _perform(self, action: RemediationAction, role: str) -> ActionResult:
        t = action.action_type
        if t == ActionType.RESTART_SERVICE:
            return self._restart(action, role)
        if t in (ActionType.THROTTLE_LOAD_GENERATOR, ActionType.RESTORE_LOAD_GENERATOR):
            return self._load(action, role)
        if t == ActionType.SET_FEATURE_FLAG:
            return self._flag(action, role)
        raise RuntimeError(f"{t.value} is not supported by the local Docker backend")

    def _restart(self, action: RemediationAction, role: str) -> ActionResult:
        target = action.target_service
        if target not in TOPOLOGY or target in NON_TARGETS:
            raise RuntimeError(f"'{target}' is not a restartable demo service")
        self.docker(["restart", target])
        state = self.docker(["inspect", "-f", "{{.State.Status}}", target])
        return self._result(action, role, f"Restarted container '{target}' (state: {state}).",
                            {"command": f"docker restart {target}"})

    def _users(self) -> int:
        r = self.http.get(f"{self.base}/loadgen/stats/requests", timeout=10)
        r.raise_for_status()
        return int(r.json().get("user_count") or 0)

    def _swarm(self, users: int) -> None:
        r = self.http.post(f"{self.base}/loadgen/swarm", data={"user_count": users, "spawn_rate": max(1, users)},
                           timeout=10)
        r.raise_for_status()

    def _load(self, action: RemediationAction, role: str) -> ActionResult:
        current = self._users()
        if action.action_type == ActionType.THROTTLE_LOAD_GENERATOR:
            target_users = max(1, current // THROTTLE_FACTOR)
            self.saved_users[action.action_id] = current
        else:
            original = self.saved_users.get(str(action.parameters.get("rollback_of")))
            target_users = original if original else current * THROTTLE_FACTOR
        self._swarm(target_users)
        verb = "Throttled" if action.action_type == ActionType.THROTTLE_LOAD_GENERATOR else "Restored"
        return self._result(action, role, f"{verb} {LOAD_GENERATOR}: {current} -> {target_users} users.",
                            {"endpoint": "/loadgen/swarm", "user_count": target_users, "previous": current})

    def _flag(self, action: RemediationAction, role: str) -> ActionResult:
        if not flag_actions_enabled():
            raise RuntimeError(f"feature-flag actions are disabled; set {FLAG_ACTIONS_ENV}=true to allow them")
        name = str(action.parameters.get("flag") or "")
        r = self.http.get(f"{self.base}/feature/api/read", timeout=10)
        r.raise_for_status()
        flags = r.json().get("flags") or {}
        if name not in flags:
            raise RuntimeError(f"feature flag '{name}' does not exist")
        off = next((v for v, val in (flags[name].get("variants") or {}).items() if v == "off" or val is False), None)
        if off is None:
            raise RuntimeError(f"feature flag '{name}' has no 'off' variant")
        flags[name]["defaultVariant"] = off
        w = self.http.post(f"{self.base}/feature/api/write",
                           json={"data": {"$schema": "https://flagd.dev/schema/v0/flags.json", "flags": flags}},
                           timeout=10)
        w.raise_for_status()
        return self._result(action, role, f"Set feature flag '{name}' to '{off}'.",
                            {"endpoint": "/feature/api/write", "flag": name, "variant": off})
