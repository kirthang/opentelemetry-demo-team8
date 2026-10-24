"""Break the demo on cue, then follow the autopilot as it detects, investigates, asks for approval and fixes it.

Usage:
    python scripts/inject_fault.py              # check, kill payment, then follow the autopilot to the end
    python scripts/inject_fault.py --check      # only check that the demo and the autopilot are ready
    python scripts/inject_fault.py --restore    # start payment again by hand
    python scripts/inject_fault.py --yes        # do not wait for Enter before breaking payment

Start the demo (`make start`) and the autopilot (`python -m src.autopilot`) first, and keep the autopilot page
(http://localhost:8790) on screen. The fault is a crash of the payment container: it is stopped with no grace
period (exit code 137), and because Docker counts that as a deliberate stop, its restart policy leaves it down
until the autopilot's approved restart. This script never approves or applies a fix; that stays a human
decision in the autopilot page.

Exit codes: 0 the incident was resolved (or checks passed), 1 it ended without a fix or timed out, 2 not ready.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.agent.llm import LLMConfigError, session_factory
from src.autopilot.engine import AWAITING_APPROVAL, CLEARED, OPEN_STATES, RESOLVED
from src.autopilot.grafana import FOLDER_UID, RULE_GROUP, WINDOW, GrafanaAlerts, Thresholds, grafana_url
from src.config import Settings
from src.telemetry.prometheus_client import PrometheusClient

TARGET = "payment"
ERROR_RULE = "ServiceHighErrorRate"
AUTOPILOT_URL = os.environ.get("AUTOPILOT_URL", "http://localhost:8790").rstrip("/")
SLOWEST_USEFUL_SECONDS = 240
POLL_SECONDS = 2
TELEMETRY_SECONDS = 10
QUIET_WARNING_SECONDS = 360
DETAIL_WIDTH = 110

OK, WARN, FAIL = "OK", "WARN", "FAIL"
RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
RED, GREEN, YELLOW, BLUE, MAGENTA, CYAN = (f"\033[{n}m" for n in (31, 32, 33, 34, 35, 36))
LEVEL_COLOURS = {OK: GREEN, WARN: YELLOW, FAIL: RED}
STEP_STYLES: Dict[str, Tuple[str, str]] = {
    "alert": ("ALERT", BOLD + RED), "agent": ("AGENT", CYAN), "tool": ("TOOL", DIM), "note": ("NOTE", DIM),
    "diagnosis": ("DIAGNOSIS", BOLD + MAGENTA), "gate": ("GATE", MAGENTA), "suggestions": ("FIXES", BOLD + BLUE),
    "approval": ("APPROVAL", BOLD + YELLOW), "verify": ("VERIFY", CYAN), "action": ("FIX", BOLD + GREEN),
    "rollback": ("ROLLBACK", BOLD + YELLOW), "result": ("RESULT", BOLD), "error": ("ERROR", BOLD + RED),
    "info": ("INFO", DIM),
}
USE_COLOUR = sys.stdout.isatty() and "NO_COLOR" not in os.environ


@dataclass
class Check:
    level: str
    name: str
    detail: str


def paint(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if USE_COLOUR and colour else text


def get_json(url: str, timeout: float = 5.0) -> Any:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


def docker(*args: str, timeout: float = 60.0) -> subprocess.CompletedProcess:
    exe = shutil.which("docker") or "/usr/local/bin/docker"
    return subprocess.run([exe, *args], capture_output=True, text=True, timeout=timeout)


def container_state(name: str) -> str:
    run = docker("inspect", "-f", "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", name,
                 timeout=15)
    return run.stdout.strip() if run.returncode == 0 else "missing"


def exit_code(name: str) -> str:
    return docker("inspect", "-f", "{{.State.ExitCode}}", name, timeout=15).stdout.strip()


def error_rates(prom: PrometheusClient) -> Dict[str, float]:
    infra = "|".join(sorted(PrometheusClient.INFRA_SERVICES))
    selector = f'span_kind=~"{PrometheusClient.INBOUND_KINDS}", service_name!~"{infra}"'
    calls = PrometheusClient.SPAN_CALLS
    query = (f'sum by (service_name) (rate({calls}{{{selector}, status_code="STATUS_CODE_ERROR"}}[{WINDOW}])) / '
             f'sum by (service_name) (rate({calls}{{{selector}}}[{WINDOW}]))')
    rows = prom.query(query)["data"]["result"]
    rates = {r["metric"].get("service_name", ""): float(r["value"][1]) for r in rows}
    return {service: rate for service, rate in rates.items() if rate == rate}


def error_rule(base_url: str) -> Tuple[float, str]:
    """The error-rate threshold and pending time the autopilot installed in Grafana, else the defaults."""
    defaults = Thresholds()
    try:
        group = get_json(f"{base_url}/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{RULE_GROUP}")
        rule = next(r for r in group["rules"] if r["title"] == ERROR_RULE)
        return float(rule["data"][1]["model"]["conditions"][0]["evaluator"]["params"][0]), str(rule["for"])
    except (requests.RequestException, StopIteration, KeyError, IndexError, TypeError, ValueError):
        return defaults.error_rate, defaults.pending


def autopilot_checks(state: Dict[str, Any]) -> List[Check]:
    checks: List[Check] = []
    if state.get("mode") == "LIVE":
        checks.append(Check(OK, "Autopilot mode", "LIVE: an approved restart is applied"))
    else:
        checks.append(Check(WARN, "Autopilot mode", "DRY RUN: an approved restart is only recorded, so payment "
                                                    "stays down until you run --restore"))
    setup = state.get("setup") or {}
    if not setup.get("rules"):
        errors = "; ".join(setup.get("errors") or []) or "not installed yet"
        checks.append(Check(FAIL, "Grafana alert rules", errors))
    elif not setup.get("webhook"):
        checks.append(Check(WARN, "Grafana alert rules", "installed, no webhook: the autopilot polls every 20 s"))
    else:
        checks.append(Check(OK, "Grafana alert rules", "installed, webhook on"))
    used, cap = int(state.get("runs_today") or 0), int(state.get("max_runs_per_day") or 0)
    if used >= cap:
        checks.append(Check(FAIL, "Daily investigations", f"{used} of {cap} used: no new investigation will start"))
    else:
        checks.append(Check(OK, "Daily investigations", f"{used} of {cap} used today"))
    if state.get("kill_switch"):
        checks.append(Check(WARN, "Kill switch", "engaged: the fix will be refused (remove .kill_switch to clear)"))
    busy = [i for i in state.get("incidents") or [] if i.get("status") in OPEN_STATES]
    if busy:
        checks.append(Check(FAIL, "Open incident", f"{busy[0]['id']} is {busy[0]['status']}; finish it first"))
    firing = state.get("firing") or []
    if firing:
        names = ", ".join(f"{f.get('alertname')} on {f.get('service')}" for f in firing)
        checks.append(Check(FAIL, "Firing alerts", f"{names}; wait until they are back to Normal"))
    if state.get("last_error"):
        checks.append(Check(WARN, "Autopilot", str(state["last_error"])))
    return checks


def pending_check(alert_states: List[Dict[str, Any]]) -> Optional[Check]:
    pending = [a for a in alert_states if str(a.get("state") or "").startswith("Pending")]
    if not pending:
        return None
    names = ", ".join(f"{(a.get('labels') or {}).get('alertname')} on {(a.get('labels') or {}).get('service_name')}"
                      for a in pending)
    return Check(WARN, "Pending alerts", f"{names} may start an incident of their own")


def readiness(settings: Settings) -> List[Check]:
    checks: List[Check] = []
    state_of_target = container_state(TARGET)
    if state_of_target.startswith("running"):
        checks.append(Check(OK, f"{TARGET} container", state_of_target))
    else:
        checks.append(Check(FAIL, f"{TARGET} container", f"{state_of_target}; run with --restore first"))

    try:
        loadgen = get_json(f"{settings.frontend_proxy_url.rstrip('/')}/loadgen/stats/requests")
        users = int(loadgen.get("user_count") or 0)
        if users > 0:
            checks.append(Check(OK, "Load generator", f"{users} simulated users"))
        else:
            checks.append(Check(FAIL, "Load generator", "no simulated users, so no checkouts will fail"))
    except (requests.RequestException, ValueError) as e:
        checks.append(Check(FAIL, "Load generator", f"not reachable: {e}"))

    prom = PrometheusClient(settings.prometheus_url)
    try:
        orders = prom.query_scalar(f'sum(rate({PrometheusClient.SPAN_CALLS}'
                                   f'{{service_name="checkout", span_kind="SPAN_KIND_SERVER"}}[{WINDOW}]))')
        threshold, pending = error_rule(grafana_url(settings))
        needed = Thresholds().min_errors
        if orders:
            wait = needed / orders
            detail = (f"about {orders * 60:.1f} orders a minute: {needed:g} failed orders take about {wait:.0f}s, "
                      f"then the alert holds for {pending}")
            if wait <= SLOWEST_USEFUL_SECONDS:
                checks.append(Check(OK, "Checkout traffic", detail))
            else:
                checks.append(Check(WARN, "Checkout traffic", f"{detail}; raise the load generator's users"))
        else:
            checks.append(Check(WARN, "Checkout traffic", "no orders seen; the alert needs "
                                                          f"{needed:g} failed ones in {WINDOW}"))
        rates = error_rates(prom)
        noisy = {s: r for s, r in rates.items() if r >= threshold}
        if noisy:
            worst = max(noisy, key=noisy.get)
            checks.append(Check(WARN, "Baseline errors", f"{worst} is already at {noisy[worst]:.0%}; the alert may "
                                                         "fire before payment is down"))
    except Exception as e:
        checks.append(Check(FAIL, "Prometheus", f"not reachable: {e}"))

    try:
        state = get_json(f"{AUTOPILOT_URL}/api/state")
        checks.extend(autopilot_checks(state))
    except (requests.RequestException, ValueError):
        checks.append(Check(FAIL, "Autopilot", f"not reachable at {AUTOPILOT_URL}; start it with "
                                               "`.venv/bin/python -m src.autopilot`"))

    try:
        pending = pending_check(GrafanaAlerts(grafana_url(settings), log=lambda _t: None).all_states())
        if pending:
            checks.append(pending)
    except requests.RequestException:
        pass

    try:
        session_factory()
        checks.append(Check(OK, "Language model", "configured"))
    except LLMConfigError as e:
        checks.append(Check(FAIL, "Language model", f"{e} The investigation would be skipped."))
    return checks


def print_checks(checks: List[Check]) -> None:
    for c in checks:
        print(f"  {paint(f'[{c.level}]'.ljust(6), LEVEL_COLOURS[c.level])} {c.name}: {c.detail}")


def restore() -> int:
    state = container_state(TARGET)
    if state == "missing":
        print(f"{TARGET} does not exist. Is the demo running (`make start`)?", file=sys.stderr)
        return 2
    if state.startswith("running"):
        print(f"{TARGET} is already {state}; nothing to restore.")
        return 0
    run = docker("start", TARGET)
    if run.returncode != 0:
        print(f"docker start {TARGET} failed: {run.stderr.strip()}", file=sys.stderr)
        return 1
    for _ in range(60):
        state = container_state(TARGET)
        if state in ("running", "running healthy"):
            break
        time.sleep(1)
    print(f"{TARGET} restored ({state}).")
    return 0 if state.startswith("running") else 1


def inject() -> Optional[str]:
    run = docker("stop", "-t", "0", TARGET)
    if run.returncode != 0:
        return run.stderr.strip() or f"docker stop {TARGET} failed"
    state = container_state(TARGET)
    if state.startswith("running"):
        return f"{TARGET} is still {state} after docker stop"
    return None


def elapsed(started: float) -> str:
    s = int(time.time() - started)
    return f"+{s // 60}:{s % 60:02d}"


def say(started: float, label: str, text: str, colour: str = "") -> None:
    print(f"{paint(elapsed(started), DIM)} {paint(f'{label:<9}', colour)} {text}", flush=True)


def clip(text: str, width: int = DETAIL_WIDTH) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= width else text[:width - 1] + "…"


def format_step(step: Dict[str, Any]) -> Tuple[str, str, str]:
    label, colour = STEP_STYLES.get(step.get("kind", ""), ("STEP", ""))
    text = clip(step.get("title") or "")
    if step.get("detail"):
        text += f": {clip(step['detail'])}"
    if step.get("status") == "running":
        text += " ..."
    elif step.get("status") == "failed":
        colour = BOLD + RED
    return label, text, colour


def new_steps(steps: List[Dict[str, Any]], printed: Dict[int, Tuple[Any, ...]]) -> List[Dict[str, Any]]:
    fresh = []
    for s in steps:
        signature = (s.get("title"), s.get("detail"), s.get("status"))
        if printed.get(s["id"]) != signature:
            printed[s["id"]] = signature
            fresh.append(s)
    return fresh


def first_incident_since(incidents: List[Dict[str, Any]], since: datetime) -> Optional[Dict[str, Any]]:
    fresh = [i for i in incidents if datetime.fromisoformat(i["created_at"]) >= since]
    return min(fresh, key=lambda i: i["created_at"], default=None)


class TelemetryWatcher:
    """Narrates what Prometheus and Grafana show until the autopilot opens an incident."""

    def __init__(self, settings: Settings, started: float) -> None:
        self.started = started
        self.prom = PrometheusClient(settings.prometheus_url)
        self.grafana = GrafanaAlerts(grafana_url(settings), log=lambda _t: None)
        self.threshold, self.pending = error_rule(grafana_url(settings))
        self.last_percent: Dict[str, int] = {}
        self.alert_states: Dict[Tuple[str, str], str] = {}
        self.fired_at: Optional[float] = None

    def poll(self) -> None:
        try:
            rates = error_rates(self.prom)
        except Exception:
            rates = {}
        for service, rate in sorted(rates.items(), key=lambda kv: -kv[1])[:2]:
            percent = round(rate * 100)
            if rate >= 0.005 and self.last_percent.get(service) != percent:
                self.last_percent[service] = percent
                say(self.started, "TELEMETRY", f"{service} error rate {rate:.1%} over the last {WINDOW} "
                                               f"(alert above {self.threshold:.0%})", YELLOW)
        try:
            states = self.grafana.all_states()
        except requests.RequestException:
            return
        for a in states:
            state = str(a.get("state") or "")
            if "NoData" in state or "Error" in state or not state.startswith(("Pending", "Alerting")):
                continue
            labels = a.get("labels") or {}
            key = (labels.get("alertname", ""), labels.get("service_name", ""))
            if self.alert_states.get(key) == state:
                continue
            self.alert_states[key] = state
            if state.startswith("Alerting"):
                self.fired_at = self.fired_at or time.time()
                say(self.started, "GRAFANA", f"{key[0]} on {key[1]} is FIRING", BOLD + RED)
            else:
                say(self.started, "GRAFANA", f"{key[0]} on {key[1]} is Pending: it must stay above "
                                             f"{self.threshold:.0%} for {self.pending} before it fires", YELLOW)


def follow(settings: Settings, since: datetime, started: float, timeout_minutes: float) -> int:
    watcher = TelemetryWatcher(settings, started)
    printed: Dict[int, Tuple[Any, ...]] = {}
    key: Optional[str] = None
    prompted = warned_quiet = unreachable = False
    next_telemetry = 0.0
    deadline = started + timeout_minutes * 60
    detail: Dict[str, Any] = {}
    while time.time() < deadline:
        try:
            if key is None:
                incident = first_incident_since(get_json(f"{AUTOPILOT_URL}/api/state")["incidents"], since)
                if incident is not None:
                    key = incident["key"]
                    say(started, "AUTOPILOT", f"opened incident {incident['id']} for {incident['alertname']} on "
                                              f"{incident['service']}", BOLD + CYAN)
            if key is not None:
                detail = get_json(f"{AUTOPILOT_URL}/api/incidents/{quote(key, safe='')}")
                for step in new_steps(detail["steps"], printed):
                    label, text, colour = format_step(step)
                    say(started, label, text, colour)
                if detail["status"] == AWAITING_APPROVAL and not prompted:
                    prompted = True
                    say(started, "YOUR TURN", f"open {AUTOPILOT_URL}/ , pick the fix marked Recommended, enter your "
                                              "name and click Approve & run", BOLD + YELLOW)
                if detail["status"] not in OPEN_STATES:
                    return finish(detail, started, watcher.fired_at)
            unreachable = False
        except (requests.RequestException, ValueError, KeyError) as e:
            if not unreachable:
                say(started, "AUTOPILOT", f"not answering ({clip(str(e), 70)}); still trying", RED)
            unreachable = True
        if key is None and time.time() >= next_telemetry:
            next_telemetry = time.time() + TELEMETRY_SECONDS
            watcher.poll()
            if not warned_quiet and time.time() - started >= QUIET_WARNING_SECONDS:
                warned_quiet = True
                say(started, "WAITING", "no incident yet. Check the autopilot terminal, the kill switch and the "
                                        "daily investigation cap.", YELLOW)
        time.sleep(POLL_SECONDS)
    say(started, "TIMEOUT", f"no final result after {timeout_minutes:g} minutes", BOLD + RED)
    print(f"\n{TARGET} may still be down. Bring it back with: python scripts/inject_fault.py --restore")
    return 1


def finish(detail: Dict[str, Any], started: float, fired_at: Optional[float]) -> int:
    status = detail["status"]
    root = (detail.get("diagnosis") or {}).get("root_cause") or {}
    confidence = (detail.get("gate") or {}).get("final_confidence")
    state = container_state(TARGET)
    print()
    say(started, "FINISHED", f"{status.upper()} ({detail.get('outcome') or 'no outcome'}) after {elapsed(started)[1:]}",
        BOLD + (GREEN if status in (RESOLVED, CLEARED) else RED))
    if fired_at is not None:
        print(f"           Grafana fired {int(fired_at - started)}s after {TARGET} was killed")
    if root.get("service"):
        print(f"           Diagnosis: {root.get('service')}, {root.get('category')}, confidence {confidence}")
    print(f"           {TARGET} is now {state}")
    if not state.startswith("running"):
        print("           Bring it back with: python scripts/inject_fault.py --restore")
    return 0 if status in (RESOLVED, CLEARED) and state.startswith("running") else 1


def explain(threshold: float, pending: str) -> None:
    print("\nWhat you will see, in order:")
    print(f"  1. {TARGET} is killed, so every checkout that needs it fails")
    print(f"  2. Grafana's {ERROR_RULE} goes Pending, then Firing once errors stay above {threshold:.0%} for {pending}")
    print("  3. The autopilot opens an incident and the agent reads metrics, traces, logs and Docker state")
    print("  4. It names the root cause and suggests fixes, then waits for you in the autopilot page")
    print("  5. After you approve, the restart goes through the Action API and fresh telemetry is compared")
    print("  6. The incident ends Resolved once Grafana's alert is back to Normal")
    print("This takes a few minutes, mostly waiting for the alert to fire and for the agent.\n")


def confirm(args: argparse.Namespace) -> bool:
    if args.yes or not sys.stdin.isatty():
        return True
    try:
        input(f"Press Enter to kill {TARGET} (Ctrl+C to cancel) ")
    except (KeyboardInterrupt, EOFError):
        print()
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="only check that everything is ready")
    mode.add_argument("--restore", action="store_true", help=f"start {TARGET} again by hand")
    parser.add_argument("--yes", action="store_true", help="do not wait for Enter before injecting")
    parser.add_argument("--force", action="store_true", help="inject even when a check fails")
    parser.add_argument("--no-follow", action="store_true", help="inject and exit; watch the autopilot page instead")
    parser.add_argument("--timeout", type=float, default=30, help="minutes to follow the incident (default 30)")
    args = parser.parse_args()

    if args.restore:
        return restore()

    settings = Settings()
    print(f"Checking the demo and the autopilot at {AUTOPILOT_URL} ...")
    checks = readiness(settings)
    print_checks(checks)
    failed = any(c.level == FAIL for c in checks)
    if args.check:
        print("\nReady." if not failed else "\nNot ready.")
        return 2 if failed else 0
    if failed and not args.force:
        print("\nNot ready: fix the failed checks above, or use --force.", file=sys.stderr)
        return 2

    threshold, pending = error_rule(grafana_url(settings))
    explain(threshold, pending)
    if not confirm(args):
        print("Cancelled; nothing was changed.")
        return 0

    since = datetime.now(timezone.utc).replace(microsecond=0)
    started = time.time()
    problem = inject()
    if problem:
        print(f"Could not break {TARGET}: {problem}", file=sys.stderr)
        return 2
    say(started, "INJECTED", f"{TARGET} killed (exit code {exit_code(TARGET)}); it will not come back by itself",
        BOLD + RED)
    if args.no_follow:
        print("Restore it later with: python scripts/inject_fault.py --restore")
        return 0
    try:
        return follow(settings, since, started, args.timeout)
    except KeyboardInterrupt:
        print(f"\nStopped following. {TARGET} may still be down; bring it back with: "
              "python scripts/inject_fault.py --restore")
        return 130


if __name__ == "__main__":
    sys.exit(main())
