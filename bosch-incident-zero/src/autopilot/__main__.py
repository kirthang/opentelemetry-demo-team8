"""Start the autopilot: install the Grafana alert rules, watch them, and serve the approval UI.

    python -m src.autopilot                 # live fixes after approval, UI at http://localhost:8790
    python -m src.autopilot --dry-run       # record fixes instead of applying them

When an Incident Zero alert fires in Grafana (pushed by webhook, and polled as a fallback), the agent
investigates, suggests fixes, waits for a human to approve one, applies it and verifies the result.
"""
from __future__ import annotations

import argparse
import threading
import time
import webbrowser
from datetime import datetime

from ..config import Settings
from .engine import Engine
from .fixers import FLAG_ACTIONS_ENV, flag_actions_enabled
from .grafana import GrafanaAlerts, Thresholds, grafana_url
from .server import WEBHOOK_PATH, AutopilotServer


def log(text: str) -> None:
    print(f"{datetime.now():%H:%M:%S} [AUTOPILOT] {text}", flush=True)


def resync(grafana: GrafanaAlerts, engine: Engine, thresholds: Thresholds, webhook: str | None) -> None:
    """Re-install the rules or webhook route if Grafana lost them; a Grafana restart drops the route."""
    try:
        gone = grafana.missing(want_webhook=webhook is not None)
    except Exception as e:
        log(f"could not check the Grafana setup: {e}")
        return
    if not gone:
        return
    log(f"Grafana lost the {' and '.join(gone)}; re-installing")
    engine.setup = grafana.ensure_setup(thresholds, webhook)
    for err in engine.setup.get("errors") or []:
        log(err)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8790, help="UI port (default 8790)")
    parser.add_argument("--dry-run", action="store_true", help="record approved fixes instead of applying them")
    parser.add_argument("--poll", type=float, default=20, help="seconds between Grafana alert checks")
    parser.add_argument("--error-rate", type=float, default=0.05, help="error-rate alert threshold")
    parser.add_argument("--p95-ms", type=float, default=5000, help="p95 latency alert threshold in ms")
    parser.add_argument("--for", dest="pending", default="1m", help="how long a rule must breach before firing")
    parser.add_argument("--max-runs-per-day", type=int, default=4, help="investigations per UTC day (LLM quota)")
    parser.add_argument("--no-webhook", action="store_true", help="poll Grafana only; do not add a contact point")
    parser.add_argument("--no-browser", action="store_true", help="do not open the UI in a browser")
    parser.add_argument("--resync", type=float, default=180,
                        help="seconds between checks that the Grafana rules and webhook route are still installed")
    args = parser.parse_args()

    settings = Settings()
    if not args.dry_run:
        from ..action_api.server import start as start_action_api

        api = start_action_api(settings.root_dir.parent, settings.root_dir / "audit_logs" / "action_api.jsonl")
        log(f"Action API at {api.url}" if api else "port 8798 is in use; assuming the Action API runs there")
        settings.action_api_endpoint = settings.action_api_endpoint or "http://127.0.0.1:8798"
        settings.action_api_catalog_path = settings.action_api_catalog_path or "/v1/catalog"
    grafana = GrafanaAlerts(grafana_url(settings), log=log)
    engine = Engine(settings, grafana, dry_run=args.dry_run, max_runs_per_day=args.max_runs_per_day, log=log)
    server = AutopilotServer(engine, port=args.port)
    server.start()
    log(f"UI at {server.url} ({'DRY RUN' if args.dry_run else 'LIVE: approved fixes are applied'})")
    if not flag_actions_enabled():
        log(f"feature-flag fixes are off; set {FLAG_ACTIONS_ENV}=true to allow them")

    thresholds = Thresholds(error_rate=args.error_rate, p95_ms=args.p95_ms, pending=args.pending)
    webhook = None if args.no_webhook else f"http://host.docker.internal:{server.port}{WEBHOOK_PATH}"
    while True:
        if grafana.healthy():
            engine.setup = grafana.ensure_setup(thresholds, webhook)
            if engine.setup.get("rules"):
                break
            log(f"Grafana setup failed: {'; '.join(engine.setup['errors'])}")
        else:
            engine.setup = {"rules": False, "webhook": False, "errors": [f"Grafana not reachable at {grafana.base}"]}
            log(f"waiting for Grafana at {grafana.base} (is the demo running? `make start`)")
        time.sleep(10)
    log(f"alert rules installed in Grafana folder 'Incident Zero' "
        f"(error rate > {thresholds.error_rate:.0%}, p95 > {thresholds.p95_ms:.0f} ms, for {thresholds.pending}); "
        f"webhook: {'on' if engine.setup.get('webhook') else 'off, polling only'}")
    for err in engine.setup.get("errors") or []:
        log(err)
    if not args.no_browser:
        threading.Thread(target=webbrowser.open, args=(server.url,), daemon=True).start()
    last_sync = time.time()
    try:
        while True:
            engine.poll()
            if time.time() - last_sync >= args.resync:
                last_sync = time.time()
                resync(grafana, engine, thresholds, webhook)
            time.sleep(args.poll)
    except KeyboardInterrupt:
        log("stopped")
        server.shutdown()


if __name__ == "__main__":
    main()
