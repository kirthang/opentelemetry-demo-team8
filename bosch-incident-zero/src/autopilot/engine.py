"""The autopilot loop: a firing Grafana alert starts an investigation, the human picks and approves one
suggested fix, the fix runs, and fresh telemetry plus the Grafana alert state decide whether it worked.

Nothing executes without a recorded human decision: the chosen action goes through ApprovalGate, and the
backend accepts only the ApprovedAction the gate issues. The kill switch is checked before every action.
"""
from __future__ import annotations

import getpass
import itertools
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..agent.agent import IncidentAgent
from ..agent.llm import LLMConfigError, session_factory
from ..agent.tools import ToolBox, ToolContext
from ..agent.transcript import Transcript
from ..approval.gate import ApprovalDecision, ApprovalGate, HumanDecision
from ..approval.pipeline import (BLOCKED_BY_KILL_SWITCH, EXECUTED_DRY_RUN, EXECUTION_FAILED,
                                 NO_ACTION_ESCALATED, NOT_RECOVERED_IRREVERSIBLE_ESCALATED,
                                 NOT_RECOVERED_ROLLBACK_FAILED, NOT_RECOVERED_ROLLED_BACK,
                                 RECOVERED, REJECTED, PipelineOutcome,
                                 ROLLBACK_TTL_MARGIN_SECONDS, _capture, finalize_report, kill_switch_reasons,
                                 verification_services)
from ..config import Settings
from ..events import EventLog
from ..remediation.backends import ActionBackend, DryRunBackend, HttpActionBackend
from ..remediation.verifier import NOT_RECOVERED, VerificationResult, verify_recovery
from .fixers import Candidate, suggest
from .grafana import FiringAlert, GrafanaAlerts

INVESTIGATING = "investigating"
AWAITING_APPROVAL = "awaiting_approval"
FIXING = "fixing"
VERIFYING = "verifying"
RESOLVED = "resolved"
NOT_RESOLVED = "not_resolved"
ROLLED_BACK = "rolled_back"
REJECTED_STATE = "rejected"
ESCALATED = "escalated"
FAILED = "failed"
CLEARED = "cleared"
OPEN_STATES = (INVESTIGATING, AWAITING_APPROVAL, FIXING, VERIFYING)

ALERT_STILL_FIRING = "NOT_RESOLVED_ALERT_FIRING"
CLEARED_BEFORE_FIX = "CLEARED_BEFORE_FIX"
ALERT_CLEAR_TIMEOUT_SECONDS = 300
ALERT_CLEAR_POLL_SECONDS = 15
STREAMED_EVENTS = {"TOOL_CALL", "MODEL_NOTE", "DIAGNOSIS", "GATE", "ERROR"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Decision:
    approved: bool
    candidate_id: Optional[str]
    approver: str
    note: str = ""


@dataclass
class Incident:
    id: str
    alert: FiringAlert
    attempt: int = 1
    status: str = INVESTIGATING
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    steps: List[Dict[str, Any]] = field(default_factory=list)
    candidates: List[Candidate] = field(default_factory=list)
    diagnosis: Optional[Dict[str, Any]] = None
    gate: Optional[Dict[str, Any]] = None
    outcome: str = ""
    transcript: Optional[Transcript] = None
    prior: List[Dict[str, Any]] = field(default_factory=list)
    related: List[str] = field(default_factory=list)
    decision: Optional[Decision] = None
    decided: threading.Event = field(default_factory=threading.Event)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def step(self, kind: str, title: str, detail: str = "", status: str = "done", **data: Any) -> Dict[str, Any]:
        s = {"id": next(self._ids), "ts": now_iso(), "kind": kind, "title": title, "detail": detail,
             "status": status, "data": data}
        self.steps.append(s)
        self.updated_at = s["ts"]
        return s

    def update(self, s: Dict[str, Any], **changes: Any) -> None:
        data = changes.pop("data", None)
        changes.setdefault("status", "done")
        if changes["status"] != "running":
            changes["ts"] = now_iso()
        s.update(changes)
        if data:
            s["data"].update(data)
        self.updated_at = now_iso()

    def summary(self) -> Dict[str, Any]:
        root = (self.diagnosis or {}).get("root_cause") or {}
        return {"id": self.id, "title": self.alert.summary, "service": self.alert.service,
                "alertname": self.alert.alertname, "severity": self.alert.severity, "status": self.status,
                "attempt": self.attempt, "root_cause": root.get("service"), "category": root.get("category"),
                "created_at": self.created_at, "updated_at": self.updated_at, "outcome": self.outcome,
                "related": list(self.related)}

    def to_dict(self) -> Dict[str, Any]:
        return {**self.summary(), "steps": list(self.steps), "diagnosis": self.diagnosis, "gate": self.gate,
                "candidates": [c.to_dict() for c in self.candidates],
                "alert": {"alertname": self.alert.alertname, "service": self.alert.service,
                          "summary": self.alert.summary, "severity": self.alert.severity,
                          "value": self.alert.value, "active_at": self.alert.active_at}}


class _StreamingEvents(EventLog):
    """EventLog that also mirrors agent events into the incident's step list for the UI."""

    def __init__(self, base: EventLog, incident: Incident) -> None:
        super().__init__(base.audit_log_path, incident_id=incident.id, run_log_dir=base.run_log_dir, echo=False)
        self.incident = incident

    def emit(self, event: str, summary: str, data: Optional[Dict[str, Any]] = None,
             incident_id: Optional[str] = None) -> Dict[str, Any]:
        record = super().emit(event, summary, data, incident_id)
        if event in STREAMED_EVENTS:
            d = record.get("data") or {}
            kind = {"TOOL_CALL": "tool", "MODEL_NOTE": "note", "DIAGNOSIS": "diagnosis",
                    "GATE": "gate", "ERROR": "error"}[event]
            title = d.get("tool") if event == "TOOL_CALL" else event.replace("_", " ").title()
            self.incident.step(kind, str(title or event), record["summary"],
                               status="failed" if event == "ERROR" else "done", event=event)
        return record


class Engine:
    def __init__(self, settings: Settings, grafana: GrafanaAlerts, dry_run: bool = False,
                 max_runs_per_day: int = 4, refire_seconds: float = 3600.0,
                 decision_timeout: float = 1800.0, log: Callable[[str], None] = print,
                 backend_factory: Optional[Callable[[], ActionBackend]] = None,
                 agent_factory: Optional[Callable[[EventLog], Any]] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.settings = settings
        self.grafana = grafana
        self.dry_run = dry_run
        self.max_runs_per_day = max_runs_per_day
        self.refire_seconds = refire_seconds
        self.decision_timeout = decision_timeout
        self.log = log
        self.sleep = sleep
        self.ctx = ToolContext(settings)
        self.base_events = EventLog.from_settings(settings, echo=False)
        self._backend_factory = backend_factory
        self._agent_factory = agent_factory
        self.incidents: Dict[str, Incident] = {}
        self.lock = threading.RLock()
        self.queue: List[FiringAlert] = []
        self.handled_at: Dict[str, float] = {}
        self.armed: Dict[str, bool] = {}
        self.runs: Dict[str, int] = {}
        self.firing_now: List[FiringAlert] = []
        self.last_poll: Optional[str] = None
        self.last_error: Optional[str] = None
        self.setup: Dict[str, Any] = {}
        self._held: Dict[str, List[FiringAlert]] = {}
        self._wake = threading.Event()
        self._worker = threading.Thread(target=self._work, name="autopilot-worker", daemon=True)
        self._worker.start()

    # ----------------------------------------------------------- alert intake

    def poll(self) -> List[FiringAlert]:
        """Read the firing Incident Zero alerts from Grafana and start or queue investigations."""
        try:
            firing = self.grafana.firing()
        except Exception as e:
            with self.lock:
                self.last_error = f"Grafana unavailable: {e}"
            return []
        with self.lock:
            self.firing_now, self.last_poll, self.last_error = firing, now_iso(), None
            services = {f.service for f in firing}
            for svc in list(self.armed):
                if svc not in services:
                    self.armed[svc] = True
            for alert in firing:
                self._admit(alert)
        self._wake.set()
        return firing

    def _admit(self, alert: FiringAlert) -> None:
        svc = alert.service
        if any(i.alert.service == svc and i.status in OPEN_STATES for i in self.incidents.values()):
            return
        if any(q.service == svc for q in self.queue):
            return
        last = self.handled_at.get(svc)
        if last is not None and not self.armed.get(svc, True) and time.time() - last < self.refire_seconds:
            return
        current = next((i for i in self.incidents.values() if i.status in OPEN_STATES), None)
        if current is not None or self.queue:
            # Likely the same cascade: attach it; it gets its own run only if it still fires afterwards.
            owner = current
            if owner is not None and svc not in owner.related:
                owner.related.append(svc)
                self.handled_at[svc], self.armed[svc] = time.time(), False
                owner.step("info", f"Related alert on {svc}", alert.summary)
            elif owner is None:
                self._held.setdefault(self.queue[0].service, []).append(alert)
            return
        self.queue.append(alert)
        self.log(f"alert queued: {alert.summary}")

    def _runs_today(self) -> int:
        return self.runs.get(datetime.now(timezone.utc).strftime("%Y-%m-%d"), 0)

    def _work(self) -> None:
        while True:
            self._wake.wait(timeout=5)
            self._wake.clear()
            while True:
                with self.lock:
                    if not self.queue or self.busy():
                        break
                    if self._runs_today() >= self.max_runs_per_day:
                        self.last_error = (f"daily cap of {self.max_runs_per_day} investigations reached "
                                           f"(free LLM quota); {len(self.queue)} alert(s) waiting")
                        break
                    alert = self.queue.pop(0)
                    incident = self._new_incident(alert)
                    for held in self._held.pop(alert.service, []):
                        if held.service not in incident.related:
                            incident.related.append(held.service)
                            incident.step("info", f"Related alert on {held.service}", held.summary)
                self._run(incident)

    def busy(self) -> bool:
        return any(i.status in OPEN_STATES for i in self.incidents.values())

    def _new_incident(self, alert: FiringAlert, prior: Optional[List[Dict[str, Any]]] = None,
                      attempt: int = 1, incident_id: Optional[str] = None) -> Incident:
        iid = incident_id or "INC-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        key = iid if attempt == 1 else f"{iid}#{attempt}"
        while key in self.incidents:
            iid, key = iid + "x", key + "x"
        incident = Incident(id=iid, alert=alert, attempt=attempt, prior=list(prior or []))
        self.incidents[key] = incident
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.runs[day] = self.runs.get(day, 0) + 1
        self.handled_at[alert.service] = time.time()
        self.armed[alert.service] = False
        return incident

    def retry(self, key: str) -> bool:
        """Investigate the same alert again, telling the agent what the previous attempt did."""
        with self.lock:
            old = self.incidents.get(key)
            if old is None or old.status in OPEN_STATES or self.busy():
                return False
            prior = old.prior + [self._prior_context(old)]
            incident = self._new_incident(old.alert, prior=prior, attempt=old.attempt + 1, incident_id=old.id)
        threading.Thread(target=self._run, args=(incident,), daemon=True).start()
        return True

    @staticmethod
    def _prior_context(i: Incident) -> Dict[str, Any]:
        chosen = next((c for c in i.candidates if i.decision and c.action.action_id == i.decision.candidate_id),
                      None)
        root = (i.diagnosis or {}).get("root_cause") or {}
        return {"attempt": i.attempt, "action_type": chosen.action.action_type.value if chosen else None,
                "target_service": chosen.action.target_service if chosen else None,
                "diagnosed_service": root.get("service"), "category": root.get("category"),
                "outcome": i.outcome or i.status}

    # ----------------------------------------------------------- the human

    def decide(self, key: str, decision: Decision) -> str:
        with self.lock:
            incident = self.incidents.get(key)
            if incident is None:
                return "unknown incident"
            if incident.status != AWAITING_APPROVAL or incident.decided.is_set():
                return "this incident is not waiting for a decision"
            if decision.approved and decision.candidate_id not in {c.action.action_id for c in incident.candidates}:
                return "unknown suggestion"
            incident.decision = decision
            incident.decided.set()
        return ""

    # ----------------------------------------------------------- one run

    def _backend(self, audit: Callable[[Dict[str, Any]], None]) -> ActionBackend:
        if self._backend_factory:
            return self._backend_factory()
        if self.dry_run:
            return DryRunBackend(audit=audit)
        return HttpActionBackend(self.settings, audit=audit)

    def _agent(self, events: EventLog) -> Any:
        if self._agent_factory:
            return self._agent_factory(events)
        return IncidentAgent(ToolBox(self.ctx), session_factory(), events=events)

    def _run(self, i: Incident) -> None:
        events = _StreamingEvents(self.base_events, i)
        try:
            self._investigate(i, events)
            self._remediate(i, events)
        except Exception as e:
            i.step("error", "Autopilot error", f"{type(e).__name__}: {e}", status="failed",
                   trace=traceback.format_exc()[-2000:])
            i.status, i.outcome = FAILED, str(e)
        finally:
            with self.lock:
                for svc in [i.alert.service, *i.related]:
                    self.handled_at[svc], self.armed[svc] = time.time(), False
            self.log(f"{i.id} attempt {i.attempt}: {i.status} {i.outcome}")
            self._wake.set()

    def _investigate(self, i: Incident, events: EventLog) -> None:
        a = i.alert
        i.step("alert", f"Grafana alert {a.alertname} is firing", a.summary, severity=a.severity, value=a.value,
               active_at=a.active_at, service=a.service)
        if i.prior:
            i.step("info", f"Attempt {i.attempt}", f"{len(i.prior)} earlier attempt(s) did not resolve the alert; "
                                                    f"the agent is told what was tried.")
        work = i.step("agent", "Investigating", "Querying Prometheus, Jaeger, OpenSearch and Docker", "running")
        try:
            agent = self._agent(events)
        except LLMConfigError as e:
            i.update(work, status="failed", title="Investigation skipped", detail=f"LLM not configured: {e}")
            i.transcript = Transcript(incident_id=i.id, alert=a.summary)
            return
        t = agent.run(a.summary, incident_id=i.id, prior_attempts=i.prior or None)
        i.transcript, i.diagnosis, i.gate = t, t.diagnosis, t.gate
        calls = len(t.tool_calls or [])
        if t.diagnosis:
            i.update(work, status="done", title="Investigation complete", detail=f"{calls} tool calls")
        else:
            reason = "; ".join((t.gate or {}).get("reasons") or t.errors or ["no diagnosis"])[:400]
            i.update(work, status="failed", title="No diagnosis", detail=f"{calls} tool calls; {reason}")

    def _remediate(self, i: Incident, events: EventLog) -> None:
        t = i.transcript
        cfg = self.settings
        i.candidates = suggest(t, i.alert.service, cfg.deploy_platform)
        if not i.candidates:
            i.step("result", "No safe fix to suggest", "Escalated to a human: nothing on the allowlist fits.",
                   status="failed")
            i.status, i.outcome = ESCALATED, NO_ACTION_ESCALATED
            self._finish(i, events, PipelineOutcome(NO_ACTION_ESCALATED, reason="no allowlisted fix fits",
                                                    transcript=t))
            return
        confident = not (i.gate or {}).get("escalate", True)
        i.step("suggestions", f"{len(i.candidates)} suggested fix(es)",
               "Pick one and approve it to run it." if confident else
               "The agent is not confident in its diagnosis. Review the evidence before approving.",
               confident=confident, mode="DRY RUN" if self.dry_run else "LIVE")
        events.emit("PLAN", f"{len(i.candidates)} suggestion(s): " + "; ".join(c.title for c in i.candidates),
                    {"suggestions": [c.to_dict() for c in i.candidates]})
        events.emit("APPROVAL_REQUESTED", f"waiting for a human decision in the autopilot UI",
                    {"channel": "chat_ui", "timeout_seconds": self.decision_timeout, "dry_run": self.dry_run})
        i.status = AWAITING_APPROVAL
        wait = i.step("approval", "Waiting for your approval", "", "running",
                      deadline=time.time() + self.decision_timeout)
        deadline, noted = time.time() + self.decision_timeout, False
        while not i.decided.wait(ALERT_CLEAR_POLL_SECONDS):
            if not noted and self._alert_firing(i) is False:
                noted = True
                i.step("info", "Alert went back to Normal while waiting",
                       "The problem may have cleared on its own. A fix approved now will not be credited with it.")
            if time.time() >= deadline:
                i.decision = Decision(False, None, "nobody", "no decision before the timeout")
                break
        d = i.decision
        chosen = next((c for c in i.candidates if c.action.action_id == d.candidate_id), None)
        human = HumanDecision(approved=d.approved and chosen is not None, approver=d.approver, channel="chat_ui",
                              decided_at=now_iso(), note=d.note)
        events.emit("APPROVAL_DECISION", f"{'APPROVED' if human.approved else 'REJECTED'} by {d.approver}"
                                         f"{': ' + chosen.title if chosen and human.approved else ''}",
                    {"approved": human.approved, "approver": d.approver, "channel": "chat_ui", "note": d.note,
                     "action_id": d.candidate_id})
        if not human.approved:
            i.update(wait, status="failed", title=f"Rejected by {d.approver}", detail=d.note)
            i.status, i.outcome = REJECTED_STATE, REJECTED
            self._finish(i, events, PipelineOutcome(REJECTED, reason=d.note or "rejected", approval=human,
                                                    transcript=t))
            return
        i.update(wait, title=f"Approved by {d.approver}", detail=chosen.title)
        self._execute(i, events, chosen, human)

    def _kill_switch(self, i: Incident, events: EventLog, checkpoint: str, action_id: str) -> bool:
        reasons = " and ".join(kill_switch_reasons(self.settings))
        events.emit("KILL_SWITCH", f"{checkpoint}: {'ENGAGED, ' + reasons if reasons else 'clear'}",
                    {"checkpoint": checkpoint, "engaged": bool(reasons), "reason": reasons or None,
                     "action_id": action_id})
        if reasons:
            i.step("error", "Kill switch engaged", f"Nothing was run ({checkpoint}): {reasons}", status="failed")
        return not reasons

    def _execute(self, i: Incident, events: EventLog, chosen: Candidate, human: HumanDecision) -> None:
        cfg, t, action = self.settings, i.transcript, chosen.action
        rb = action.rollback
        i.status = FIXING

        def done(status: str, reason: str, state: str, **kw: Any) -> None:
            i.status, i.outcome = state, status
            self._finish(i, events, PipelineOutcome(status, action=action, reason=reason, approval=human,
                                                    transcript=t, **kw))

        gate = ApprovalGate(lambda _a: human, ttl_seconds=cfg.approval_ttl_seconds,
                            rollback_ttl_seconds=(cfg.approval_ttl_seconds + 2 * cfg.action_settle_seconds
                                                  + ROLLBACK_TTL_MARGIN_SECONDS + ALERT_CLEAR_TIMEOUT_SECONDS))
        review = gate.review(action)
        if review.decision != ApprovalDecision.APPROVED:
            i.step("error", "Approval gate refused", review.reason, status="failed")
            return done(REJECTED, review.reason, REJECTED_STATE)
        if not self._kill_switch(i, events, "post_approval", action.action_id):
            return done(BLOCKED_BY_KILL_SWITCH, "kill switch engaged", ESCALATED)

        services = verification_services(t, action)
        base_step = i.step("verify", "Recording baseline", ", ".join(services), "running")
        baseline = _capture(self.ctx, services)
        i.update(base_step, title="Baseline recorded", data={"baseline": _brief(baseline)})

        firing_before = self._alert_firing(i)
        if firing_before is False:
            i.step("info", "Alert already back to Normal", "The Grafana alert cleared before the fix ran, so the fix "
                   "will not be credited with clearing it.")
        if not self._kill_switch(i, events, "pre_execute", action.action_id):
            return done(BLOCKED_BY_KILL_SWITCH, "kill switch engaged", ESCALATED, baseline=baseline)
        be = self._backend(audit=lambda e: events.emit("ACTION_REFUSED", str(e.get("reason")), e))
        for act in (action, rb):
            if act is not None:
                act.dry_run, act.backend = be.dry_run, be.name
        run = i.step("action", chosen.title, "Applying the fix", "running", backend=be.name)
        try:
            result = be.execute(review.action_token, expected_action_id=action.action_id)
        except Exception as e:
            i.update(run, status="failed", detail=f"{type(e).__name__}: {e}")
            events.emit("ERROR", f"action {action.action_id} failed: {e}", {"action_id": action.action_id})
            return done(EXECUTION_FAILED, f"action failed: {e}", FAILED, baseline=baseline)
        action.executed_at, action.execution_result = result.timestamp, result.message
        events.emit("DRY_RUN" if result.dry_run else "ACTION_EXECUTED", result.message, result.to_dict())
        i.update(run, detail=result.message, data={"request": result.request})

        i.status = VERIFYING
        settle = 0 if be.dry_run else cfg.action_settle_seconds
        v = self._verify(i, events, action, baseline, settle, be.dry_run, "Verifying with fresh telemetry")
        if v.status == NOT_RECOVERED and not be.dry_run:
            # The first after-window can still hold errors caused by the action itself (e.g. a restart).
            v = self._verify(i, events, action, baseline, settle, False,
                             "Re-checking after the transient from the fix has passed")
        kw = {"result": result, "verification": v, "baseline": baseline}

        if be.dry_run:
            i.step("result", "Dry run finished", "Nothing was changed, so recovery is not claimed.")
            return done(EXECUTED_DRY_RUN, "dry run recorded", NOT_RESOLVED, **kw)

        if v.status == NOT_RECOVERED:
            if rb is None or review.rollback_token is None:
                i.step("result", "Fix did not resolve the issue", "No rollback exists for this action. "
                       "Investigate again or take over manually.", status="failed")
                return done(NOT_RECOVERED_IRREVERSIBLE_ESCALATED, "no recovery measured", NOT_RESOLVED, **kw)
            if not self._kill_switch(i, events, "pre_rollback", rb.action_id):
                return done(BLOCKED_BY_KILL_SWITCH, "kill switch blocked the rollback", ESCALATED, **kw)
            back = i.step("rollback", f"Rolling back: {rb.description}", "", "running")
            try:
                rb_result = be.rollback(review.rollback_token, expected_action_id=rb.action_id)
            except Exception as e:
                i.update(back, status="failed", detail=str(e))
                events.emit("ROLLBACK", f"rollback {rb.action_id} FAILED: {e}", {"ok": False})
                return done(NOT_RECOVERED_ROLLBACK_FAILED, f"rollback failed: {e}", FAILED, **kw)
            events.emit("ROLLBACK", rb_result.message, {"ok": True, **rb_result.to_dict()})
            i.update(back, detail=rb_result.message)
            i.step("result", "Fix did not resolve the issue; rolled back", "Investigate again or try another "
                   "suggestion.", status="failed")
            return done(NOT_RECOVERED_ROLLED_BACK, "no recovery measured; rolled back", ROLLED_BACK,
                        rollback_result=rb_result, **kw)

        if firing_before is False:
            i.step("result", "Cleared on its own", f"The alert on {i.alert.service} went back to Normal before the "
                   f"fix ran (telemetry after the fix: {v.status}). The fix ran but is not credited with the recovery.")
            return done(CLEARED_BEFORE_FIX, f"alert cleared before the fix; telemetry {v.status}", CLEARED, **kw)
        cleared = self._wait_alert_clear(i)
        if v.status == RECOVERED and cleared:
            action.recovery_verified = True
            i.step("result", "Resolved", f"Telemetry recovered and the Grafana alert on {i.alert.service} cleared.")
            return done(RECOVERED, "telemetry recovered and the alert cleared", RESOLVED, **kw)
        if cleared:
            i.step("result", "Alert cleared after the fix", f"The Grafana alert was firing when the fix ran and has "
                   f"cleared since, but the telemetry check was {v.status}, so recovery is not confirmed by metrics. "
                   f"Keep an eye on it.")
            return done(RECOVERED, f"alert cleared; telemetry {v.status}", RESOLVED, **kw)
        i.step("result", "Not resolved", f"The Grafana alert is still firing (telemetry: {v.status}). "
               f"Investigate again or take over manually.", status="failed")
        return done(ALERT_STILL_FIRING, f"alert still firing; telemetry {v.status}", NOT_RESOLVED, **kw)

    def _verify(self, i: Incident, events: EventLog, action: Any, baseline: Dict[str, Any], settle: int,
                dry_run: bool, title: str) -> VerificationResult:
        ver = i.step("verify", title, f"Waiting {settle}s for the fix to settle", "running",
                     settle_until=time.time() + settle)
        try:
            v = verify_recovery(self.ctx, action, baseline, settle_seconds=settle, dry_run=dry_run,
                                sleep=self.sleep, events=events)
        except Exception as e:
            v = VerificationResult(status="NO_DATA", recovered=False, before=baseline,
                                   reasons=[f"verification failed: {e}"])
        i.update(ver, title=f"Telemetry check: {v.status}", detail="; ".join(v.reasons[:4]),
                 status="done" if v.status == RECOVERED else "failed",
                 data={"verification": {"status": v.status, "before": _brief(v.before), "after": _brief(v.after),
                                        "services": {s: {k: x.get(k) for k in ("status", "before", "after")}
                                                     for s, x in (v.services or {}).items()}}})
        return v

    def _alert_firing(self, i: Incident) -> Optional[bool]:
        """Whether the incident's alert, or one attached to it, is firing now; None when Grafana can't say."""
        try:
            firing = {f.service for f in self.grafana.firing()}
        except Exception:
            return None
        return bool(firing & {i.alert.service, *i.related})

    def _wait_alert_clear(self, i: Incident) -> bool:
        s = i.step("verify", f"Watching Grafana alert {i.alert.alertname}", "Waiting for it to return to Normal",
                   "running", deadline=time.time() + ALERT_CLEAR_TIMEOUT_SECONDS)
        deadline = time.time() + ALERT_CLEAR_TIMEOUT_SECONDS
        while True:
            try:
                firing = self.grafana.is_firing(i.alert.service)
            except Exception as e:
                firing, err = True, str(e)
            else:
                err = ""
            if not firing:
                i.update(s, title=f"Grafana alert {i.alert.alertname} cleared", detail="Back to Normal")
                return True
            if time.time() >= deadline:
                i.update(s, status="failed", title=f"Grafana alert {i.alert.alertname} still firing",
                         detail=err or f"after {ALERT_CLEAR_TIMEOUT_SECONDS}s")
                return False
            self.sleep(ALERT_CLEAR_POLL_SECONDS)

    def _finish(self, i: Incident, events: EventLog, outcome: PipelineOutcome) -> None:
        t = i.transcript
        if t is None or t.report is None:
            return
        outcome.attempt = i.attempt
        outcome.attempts = [outcome]
        outcome.final_status, outcome.final_reason = i.status.upper(), outcome.reason
        try:
            finalize_report(t, self.settings, outcome, events)
            i.step("info", "Report written", str(outcome.report_paths.get("json") or
                                                  self.settings.resolve_path(self.settings.report_output_dir) / t.incident_id))
        except Exception as e:
            i.step("error", "Report not written", str(e), status="failed")

    # ----------------------------------------------------------- state for the UI

    def state(self) -> Dict[str, Any]:
        with self.lock:
            items = sorted(self.incidents.items(), key=lambda kv: kv[1].created_at + f"{kv[1].attempt:03d}",
                           reverse=True)
            return {
                "incidents": [{**i.summary(), "key": k} for k, i in items],
                "firing": [f.__dict__ for f in self.firing_now],
                "queue": [q.summary for q in self.queue],
                "last_poll": self.last_poll, "last_error": self.last_error, "setup": self.setup,
                "mode": "DRY RUN" if self.dry_run else "LIVE",
                "kill_switch": bool(kill_switch_reasons(self.settings)),
                "runs_today": self._runs_today(), "max_runs_per_day": self.max_runs_per_day,
                "operator": _operator(),
            }

    def incident(self, key: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            i = self.incidents.get(key)
            return None if i is None else {**i.to_dict(), "key": key}


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "operator"


def _brief(snap: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not snap:
        return None
    if "snapshots" in snap:
        return {svc: _brief(s) for svc, s in (snap.get("snapshots") or {}).items()}
    return {k: snap.get(k) for k in ("service", "error_rate", "p95_ms", "rps", "container_state") if k in snap}
