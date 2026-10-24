"""Grafana alerting: install the Incident Zero alert rules and webhook, and read which of them are firing.

The rules live in their own folder and rule group, so the demo's own provisioned rules are left alone.
Only alerts labelled incident_zero="true" start an investigation.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import requests

from ..telemetry.prometheus_client import PrometheusClient

FOLDER_UID = "incident-zero"
FOLDER_TITLE = "Incident Zero"
RULE_GROUP = "incident-zero"
CONTACT_POINT = "incident-zero-autopilot"
LABEL = "incident_zero"
DATASOURCE_UID = "webstore-metrics"
WINDOW_SECONDS = 180
WINDOW = f"{WINDOW_SECONDS}s"
INBOUND = 'span_kind=~"SPAN_KIND_SERVER|SPAN_KIND_CONSUMER"'
CALLS = PrometheusClient.SPAN_CALLS
DURATION = PrometheusClient.SPAN_DURATION_BUCKET


@dataclass(frozen=True)
class Thresholds:
    error_rate: float = 0.05
    min_errors: float = 3.0
    p95_ms: float = 5000.0
    min_rps: float = 0.01
    pending: str = "1m"
    interval_seconds: int = 30


@dataclass(frozen=True)
class FiringAlert:
    alertname: str
    service: str
    summary: str
    severity: str
    value: str
    active_at: str

    @property
    def key(self) -> str:
        return f"{self.alertname}:{self.service}"


def grafana_url(settings: Any) -> str:
    return os.environ.get("GRAFANA_URL", settings.frontend_proxy_url.rstrip("/") + "/grafana").rstrip("/")


def _selector(extra: str = "") -> str:
    infra = "|".join(sorted(PrometheusClient.INFRA_SERVICES))
    return "{" + f'{INBOUND}, service_name!~"{infra}"' + (f", {extra}" if extra else "") + "}"


def _rate(extra: str = "") -> str:
    return f"sum by (service_name) (rate({CALLS}{_selector(extra)}[{WINDOW}]))"


def error_rate_expr(t: Thresholds) -> str:
    errors = _rate('status_code="STATUS_CODE_ERROR"')
    return f"({errors} / {_rate()}) and on (service_name) ({errors} * {WINDOW_SECONDS} >= {t.min_errors:g})"


def latency_expr(t: Thresholds) -> str:
    p95 = (f"histogram_quantile(0.95, sum by (service_name, le) "
           f"(rate({DURATION}{_selector()}[{WINDOW}])))")
    return f"{p95} and on (service_name) ({_rate()} > {t.min_rps:g})"


def _rule(uid: str, title: str, expr: str, threshold: float, summary: str, severity: str,
          t: Thresholds) -> Dict[str, Any]:
    return {
        "uid": uid, "title": title, "orgID": 1, "folderUID": FOLDER_UID, "ruleGroup": RULE_GROUP,
        "condition": "B", "for": t.pending, "noDataState": "OK", "execErrState": "OK",
        "data": [
            {"refId": "A", "relativeTimeRange": {"from": 600, "to": 0}, "datasourceUid": DATASOURCE_UID,
             "model": {"refId": "A", "expr": expr, "instant": True, "range": False, "editorMode": "code"}},
            {"refId": "B", "datasourceUid": "__expr__", "relativeTimeRange": {"from": 0, "to": 0},
             "model": {"refId": "B", "type": "threshold", "expression": "A",
                       "conditions": [{"evaluator": {"type": "gt", "params": [threshold]}}]}},
        ],
        "labels": {LABEL: "true", "severity": severity},
        "annotations": {"summary": summary, "description": "Raised for Incident Zero autopilot"},
    }


def alert_rules(t: Thresholds) -> List[Dict[str, Any]]:
    return [
        _rule("iz-service-error-rate", "ServiceHighErrorRate", error_rate_expr(t), t.error_rate,
              f"Error rate above {t.error_rate:.0%} on {{{{ $labels.service_name }}}}", "critical", t),
        _rule("iz-service-latency", "ServiceHighLatency", latency_expr(t), t.p95_ms,
              f"p95 latency above {t.p95_ms:.0f} ms on {{{{ $labels.service_name }}}}", "warning", t),
    ]


class GrafanaAlerts:
    def __init__(self, base_url: str, session: Optional[requests.Session] = None, timeout: float = 10.0,
                 log: Callable[[str], None] = print) -> None:
        self.base = base_url.rstrip("/")
        self.http = session or requests.Session()
        self.http.headers.update({"Content-Type": "application/json", "X-Disable-Provenance": "true"})
        self.timeout = timeout
        self.log = log

    def _call(self, method: str, path: str, **kw: Any) -> requests.Response:
        return self.http.request(method, self.base + path, timeout=self.timeout, **kw)

    def healthy(self) -> bool:
        try:
            return self._call("GET", "/api/health").ok
        except requests.RequestException:
            return False

    def ensure_setup(self, thresholds: Thresholds, webhook_url: Optional[str]) -> Dict[str, Any]:
        """Idempotently create the folder, the rule group and (when webhook_url is set) the webhook route."""
        status: Dict[str, Any] = {"rules": False, "webhook": False, "errors": []}
        try:
            if self._call("GET", f"/api/folders/{FOLDER_UID}").status_code == 404:
                r = self._call("POST", "/api/folders", json={"uid": FOLDER_UID, "title": FOLDER_TITLE})
                if not r.ok and r.status_code not in (409, 412):
                    raise RuntimeError(f"create folder: HTTP {r.status_code} {r.text[:200]}")
            group = {"title": RULE_GROUP, "folderUid": FOLDER_UID, "interval": thresholds.interval_seconds,
                     "rules": alert_rules(thresholds)}
            r = self._call("PUT", f"/api/v1/provisioning/folder/{FOLDER_UID}/rule-groups/{RULE_GROUP}", json=group)
            if not r.ok:
                raise RuntimeError(f"install rules: HTTP {r.status_code} {r.text[:300]}")
            status["rules"] = True
        except (requests.RequestException, RuntimeError) as e:
            status["errors"].append(str(e))
            return status
        if webhook_url:
            try:
                self._ensure_webhook(webhook_url)
                status["webhook"] = True
            except (requests.RequestException, RuntimeError) as e:
                status["errors"].append(f"webhook not installed, polling only: {e}")
        return status

    def _ensure_webhook(self, url: str) -> None:
        r = self._call("GET", "/api/v1/provisioning/contact-points", params={"name": CONTACT_POINT})
        r.raise_for_status()
        existing = [c for c in r.json() if c.get("name") == CONTACT_POINT]
        body = {"name": CONTACT_POINT, "type": "webhook", "disableResolveMessage": False,
                "settings": {"url": url, "httpMethod": "POST"}}
        if existing:
            body["uid"] = existing[0]["uid"]
            r = self._call("PUT", f"/api/v1/provisioning/contact-points/{body['uid']}", json=body)
        else:
            r = self._call("POST", "/api/v1/provisioning/contact-points", json=body)
        if not r.ok:
            raise RuntimeError(f"contact point: HTTP {r.status_code} {r.text[:200]}")

        r = self._call("GET", "/api/v1/provisioning/policies")
        r.raise_for_status()
        tree = r.json()
        routes = [x for x in tree.get("routes") or [] if x.get("receiver") != CONTACT_POINT]
        routes.insert(0, {"receiver": CONTACT_POINT, "object_matchers": [[LABEL, "=", "true"]],
                          "group_by": ["alertname", "service_name"], "group_wait": "5s",
                          "group_interval": "30s", "repeat_interval": "1h", "continue": False})
        tree["routes"] = routes
        tree.pop("provenance", None)
        r = self._call("PUT", "/api/v1/provisioning/policies", json=tree)
        if not r.ok:
            raise RuntimeError(f"notification policy: HTTP {r.status_code} {r.text[:200]}")

    def all_states(self) -> List[Dict[str, Any]]:
        r = self._call("GET", "/api/prometheus/grafana/api/v1/alerts")
        r.raise_for_status()
        return [a for a in (r.json().get("data") or {}).get("alerts") or []
                if (a.get("labels") or {}).get(LABEL) == "true"]

    def firing(self) -> List[FiringAlert]:
        out = []
        for a in self.all_states():
            state = str(a.get("state") or "")
            if not state.startswith("Alerting") or "NoData" in state or "Error" in state:
                continue
            labels, notes = a.get("labels") or {}, a.get("annotations") or {}
            service = labels.get("service_name") or ""
            if not service:
                continue
            out.append(FiringAlert(alertname=labels.get("alertname", ""), service=service,
                                   summary=notes.get("summary") or f"{labels.get('alertname')} on {service}",
                                   severity=labels.get("severity", ""), value=str(a.get("value") or ""),
                                   active_at=str(a.get("activeAt") or "")))
        return sorted(out, key=lambda f: (f.severity != "critical", f.service))

    def is_firing(self, service: str) -> bool:
        return any(f.service == service for f in self.firing())
