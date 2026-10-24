"""Demo fault injector: readiness checks, injection and restore, and following an incident to its end."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import datetime, timezone
from itertools import chain, repeat
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "inject_fault", Path(__file__).resolve().parents[1] / "scripts" / "inject_fault.py")
inj = importlib.util.module_from_spec(_SPEC)
sys.modules["inject_fault"] = inj
_SPEC.loader.exec_module(inj)

SINCE = datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc)


def healthy_state(**changes):
    state = {"mode": "LIVE", "setup": {"rules": True, "webhook": True, "errors": []}, "runs_today": 1,
             "max_runs_per_day": 4, "kill_switch": False, "incidents": [], "firing": [], "last_error": None}
    state.update(changes)
    return state


def levels(checks):
    return {c.name: c.level for c in checks}


def test_a_ready_autopilot_passes_every_check():
    assert set(levels(inj.autopilot_checks(healthy_state())).values()) == {inj.OK}


@pytest.mark.parametrize("changes, name, level", [
    ({"mode": "DRY RUN"}, "Autopilot mode", inj.WARN),
    ({"setup": {"rules": False, "webhook": False, "errors": ["Grafana not reachable"]}}, "Grafana alert rules", inj.FAIL),
    ({"setup": {"rules": True, "webhook": False, "errors": []}}, "Grafana alert rules", inj.WARN),
    ({"runs_today": 4}, "Daily investigations", inj.FAIL),
    ({"kill_switch": True}, "Kill switch", inj.WARN),
    ({"incidents": [{"id": "INC-1", "status": "awaiting_approval"}]}, "Open incident", inj.FAIL),
    ({"firing": [{"alertname": "ServiceHighErrorRate", "service": "checkout"}]}, "Firing alerts", inj.FAIL),
    ({"last_error": "Grafana unavailable: boom"}, "Autopilot", inj.WARN),
])
def test_autopilot_problems_are_reported(changes, name, level):
    assert levels(inj.autopilot_checks(healthy_state(**changes)))[name] == level


def test_a_finished_incident_does_not_block_a_new_run():
    state = healthy_state(incidents=[{"id": "INC-1", "status": "resolved"}])
    assert "Open incident" not in levels(inj.autopilot_checks(state))


def test_pending_alerts_are_only_a_warning():
    states = [{"state": "Pending", "labels": {"alertname": "ServiceHighLatency", "service_name": "quote"}},
              {"state": "Normal", "labels": {"alertname": "ServiceHighLatency", "service_name": "ad"}}]
    check = inj.pending_check(states)
    assert check.level == inj.WARN and "quote" in check.detail and "ad" not in check.detail
    assert inj.pending_check(states[1:]) is None


def test_only_incidents_opened_after_the_injection_are_followed():
    old = {"id": "OLD", "key": "OLD", "created_at": "2026-10-24T09:59:59+00:00"}
    first = {"id": "NEW-1", "key": "NEW-1", "created_at": "2026-10-24T10:03:00+00:00"}
    later = {"id": "NEW-2", "key": "NEW-2", "created_at": "2026-10-24T10:04:00+00:00"}
    assert inj.first_incident_since([later, old, first], SINCE)["id"] == "NEW-1"
    assert inj.first_incident_since([old], SINCE) is None


def test_a_step_is_printed_again_only_when_it_changes():
    printed = {}
    step = {"id": 1, "title": "Investigating", "detail": "", "status": "running"}
    assert inj.new_steps([step], printed) == [step]
    assert inj.new_steps([dict(step)], printed) == []
    done = dict(step, title="Investigation complete", status="done")
    assert inj.new_steps([done], printed) == [done]


def test_steps_are_labelled_and_long_details_are_clipped():
    label, text, _ = inj.format_step({"kind": "tool", "title": "system_overview", "detail": "x" * 300, "status": "done"})
    assert label == "TOOL" and text.startswith("system_overview: ") and len(text) < 140 and text.endswith("…")
    assert inj.format_step({"kind": "agent", "title": "Investigating", "detail": "", "status": "running"})[1] \
        == "Investigating ..."


class FakeDocker:
    def __init__(self, state="running healthy", fail=False):
        self.state, self.fail, self.calls = state, fail, []

    def __call__(self, *args, timeout=60.0):
        self.calls.append(args)
        if args[0] == "inspect" and "{{.State.ExitCode}}" in args:
            return subprocess.CompletedProcess(args, 0, "137\n", "")
        if args[0] == "inspect":
            return subprocess.CompletedProcess(args, 0 if self.state != "missing" else 1, self.state + "\n", "")
        if self.fail:
            return subprocess.CompletedProcess(args, 1, "", "daemon error")
        self.state = "exited" if args[0] == "stop" else "running healthy"
        return subprocess.CompletedProcess(args, 0, "", "")


def test_injection_stops_payment_immediately(monkeypatch):
    fake = FakeDocker()
    monkeypatch.setattr(inj, "docker", fake)
    assert inj.inject() is None
    assert ("stop", "-t", "0", "payment") in fake.calls


def test_a_failed_injection_is_reported(monkeypatch):
    monkeypatch.setattr(inj, "docker", FakeDocker(fail=True))
    assert inj.inject() == "daemon error"


def test_restore_starts_a_stopped_payment_and_leaves_a_running_one(monkeypatch):
    stopped = FakeDocker(state="exited")
    monkeypatch.setattr(inj, "docker", stopped)
    assert inj.restore() == 0 and ("start", "payment") in stopped.calls

    running = FakeDocker()
    monkeypatch.setattr(inj, "docker", running)
    assert inj.restore() == 0 and not any(c[0] == "start" for c in running.calls)

    monkeypatch.setattr(inj, "docker", FakeDocker(state="missing"))
    assert inj.restore() == 2


class QuietWatcher:
    fired_at = None

    def __init__(self, settings, started):
        pass

    def poll(self):
        pass


def run_follow(monkeypatch, responses, container="running healthy"):
    answers = iter(responses)
    monkeypatch.setattr(inj, "get_json", lambda url, timeout=5.0: next(answers))
    monkeypatch.setattr(inj, "TelemetryWatcher", QuietWatcher)
    monkeypatch.setattr(inj, "container_state", lambda name: container)
    monkeypatch.setattr(inj.time, "sleep", lambda s: None)
    return inj.follow(None, SINCE, inj.time.time(), 1)


INCIDENT = {"id": "INC-1", "key": "INC-1", "created_at": "2026-10-24T10:03:00+00:00",
            "alertname": "ServiceHighErrorRate", "service": "checkout"}


def detail(status, steps, outcome="", **extra):
    return {"id": "INC-1", "status": status, "outcome": outcome, "steps": steps, "diagnosis": None, "gate": None,
            **extra}


TOOL = {"id": 2, "kind": "tool", "title": "system_overview", "detail": "1 service(s) flagged", "status": "done"}
ALERT = {"id": 1, "kind": "alert", "title": "Grafana alert ServiceHighErrorRate is firing",
         "detail": "Error rate above 5% on checkout", "status": "done"}


def test_follow_narrates_an_incident_from_alert_to_resolution(monkeypatch, capsys):
    diagnosis = {"root_cause": {"service": "payment", "category": "container_down_or_restarting"}}
    code = run_follow(monkeypatch, [
        {"incidents": []},
        {"incidents": [INCIDENT]}, detail("investigating", [ALERT, TOOL]),
        detail("awaiting_approval", [ALERT, TOOL]),
        detail("resolved", [ALERT, TOOL], outcome="RECOVERED", diagnosis=diagnosis,
               gate={"final_confidence": "high"}),
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "opened incident INC-1 for ServiceHighErrorRate on checkout" in out
    assert "system_overview: 1 service(s) flagged" in out
    assert out.count("YOUR TURN") == 1 and "Approve & run" in out
    assert "RESOLVED" in out and "Diagnosis: payment, container_down_or_restarting, confidence high" in out
    assert "--restore" not in out


def test_follow_reports_a_rejected_fix_and_how_to_bring_payment_back(monkeypatch, capsys):
    code = run_follow(monkeypatch, [
        {"incidents": [INCIDENT]}, detail("rejected", [ALERT], outcome="REJECTED"),
    ], container="exited")
    out = capsys.readouterr().out
    assert code == 1
    assert "REJECTED" in out and "payment is now exited" in out and "--restore" in out


def test_follow_survives_the_autopilot_not_answering(monkeypatch, capsys):
    import requests

    def flaky(url, timeout=5.0):
        raise requests.ConnectionError("refused")

    monkeypatch.setattr(inj, "get_json", flaky)
    monkeypatch.setattr(inj, "TelemetryWatcher", QuietWatcher)
    monkeypatch.setattr(inj.time, "sleep", lambda s: None)
    clock = chain([1000.0, 1000.0], repeat(10_000.0))
    monkeypatch.setattr(inj.time, "time", lambda: next(clock))
    assert inj.follow(None, SINCE, 1000.0, 1) == 1
    out = capsys.readouterr().out
    assert out.count("not answering") == 1 and "TIMEOUT" in out and "--restore" in out
