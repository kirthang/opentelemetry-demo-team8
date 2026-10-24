# Incident INC-20261024-052210

> **DRY RUN - nothing was changed.**

**Status:** MITIGATION_UNVERIFIED - Recovery NOT verified - not claimed as resolved.

| Field | Value |
|---|---|
| Diagnosed at | 2026-10-24T05:25:19+00:00 |
| Target service | email |
| Severity | HIGH |
| Confidence | 0.6 |
| Category | dependency_unreachable |
| Impacted services | checkout, frontend, frontend-proxy, load-generator |

## Root cause

Checkout's HTTP client timeout (~1-3s) is too aggressive for email service, causing 'context deadline exceeded' errors when email calls occasionally exceed the deadline or fail to establish connection (reached=false). Email service itself is healthy (0% error, p95 147ms, healthy container).

## Evidence

- trace `adc536daff7450407aaca37c5486f84e`
- trace `f10582acca8309bdf9de55c777ba5960`
- `metric:checkout:client:POST:error_rate` = 0.0155
- `metric:checkout:client:POST:p95_ms` = 229.6
- `metric:email:server:POST /send_order_confirmation:error_rate` = 0
- `metric:email:server:POST /send_order_confirmation:p95_ms` = 147.5
- `container:email:state` = running
- `container:email:restart_count` = 0
- `container:email:health` = healthy
- log: failed to send order confirmation: failed POST to email service: Post "http://email:6060/send_order_confirmation": context deadline exceeded
- log: failed to send order confirmation: failed POST to email service: Post "http://email:6060/send_order_confirmation": context deadline exceeded

## Remediation

- Recommended: Increase checkout's HTTP client timeout for email calls (e.g., to 5-10s) or make the email call async/fire-and-forget so checkout doesn't fail the user-facing order. Investigate product-catalog CPU/latency separately as a capacity issue.
- Approval: APPROVED (required: True)
- Action: RESTART_SERVICE on email (ACT-INC-20261024-052210)
- Reason: dependency_unreachable diagnosed on 'email' (medium confidence): restart 'email'.
- Parameters: `{"reason": "dependency_unreachable diagnosed on 'email' (medium confidence): restart 'email'.", "diagnosed_service": "email", "category": "dependency_unreachable"}`
- Approver: vishwasreddy via cli at 2026-10-24T05:25:20+00:00
- Backend: dry_run, dry run: True
- Executed at: 2026-10-24T05:25:20+00:00, result: DRY RUN: RESTART_SERVICE on 'email' was recorded, not sent. No Action API call was made.
- Rollback: n/a

## Timeline

| At | Event | Status | Detail |
|---|---|---|---|
| 2026-10-24T05:25:19.366+00:00 | PLAN | n/a | RESTART_SERVICE -> email; rollback: none |
| 2026-10-24T05:25:19.367+00:00 | KILL_SWITCH | n/a | plan: clear |
| 2026-10-24T05:25:20.367+00:00 | APPROVAL_REQUESTED | n/a | RESTART_SERVICE -> email with rollback NONE (irreversible) via cli, timeout 120s |
| 2026-10-24T05:25:20.872+00:00 | APPROVAL_DECISION | n/a | APPROVED by vishwasreddy via cli: approved at the terminal prompt |
| 2026-10-24T05:25:20.872+00:00 | KILL_SWITCH | n/a | post_approval: clear |
| 2026-10-24T05:25:20.873+00:00 | KILL_SWITCH | n/a | pre_execute: clear |
| 2026-10-24T05:25:20.874+00:00 | DRY_RUN | n/a | DRY RUN: RESTART_SERVICE on 'email' was recorded, not sent. No Action API call was made. |
| 2026-10-24T05:25:21.400+00:00 | VERIFICATION | n/a | NOT_VERIFIED_DRY_RUN: dry run, nothing changed, recovery not claimed |

## Recovery verification

**NOT_VERIFIED_DRY_RUN** (checked 2026-10-24T05:25:21+00:00)

| Metric | Before | After |
|---|---|---|
| Error rate | 0 | 0 |
| p95 (ms) | 172.5 | 172.5 |
| rps | 0.046 | 0.046 |
| Container | running | running |
| Health | healthy | healthy |
| Restarts | 0 | 0 |
| Source | system_overview | system_overview |

- dry run: nothing changed, recovery not claimed
- after snapshot is informational only (taken without a settle wait)

## Prevention

- Add health checks and an availability alert for email.
- Make callers of email fail fast with timeouts, retries with backoff and a circuit breaker.
- Document a runbook for restoring email.

Audit log: `/Users/vishwasreddy/Desktop/bosch/team8-otel-demo/bosch-incident-zero/audit_logs/audit.jsonl`
