"""Fix candidates for a diagnosis. Each is an allowlisted RemediationAction; an approved one is applied
through the Action API (src/action_api) by HttpActionBackend.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ..agent.tools import TOPOLOGY
from ..agent.transcript import Transcript
from ..models.action import ActionType, RemediationAction, is_allowlisted
from ..remediation.planner import LOAD_GENERATOR, NON_TARGETS, _build, plan_with_reason

THROTTLE_USERS = 1
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
        return f"Throttle {LOAD_GENERATOR} to {action.parameters.get('users', THROTTLE_USERS)} user(s)"
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
        add(_build(transcript, ActionType.THROTTLE_LOAD_GENERATOR, LOAD_GENERATOR, why, {"users": THROTTLE_USERS}),
            why)
    return out
