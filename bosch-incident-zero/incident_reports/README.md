# Incident reports

Reports the agent produced on 2026-10-24, copied from the git-ignored `incident_output/`. Each folder has
`report.json` (validated against `config/incident_schema.json`) and `report.md` (the readable form).

The organisers' practice incidents and `faultctl` were not available, so these incidents were injected with
the demo's feature flags from the flags page. Every action ran in dry run because the Action API contract was
not supplied, so no recovery is claimed.

| Incident | Alert (all the agent received) | Fault actually injected | Agent's diagnosis | Outcome | Right? |
|---|---|---|---|---|---|
| [INC-20261024-065036](INC-20261024-065036/report.md) | Error rate above 5% on checkout (raised by the alert watcher) | `paymentUnreachable` | payment, dependency unreachable, high confidence; reviewer upheld | Restart payment approved by a human; dry run, `MITIGATION_UNVERIFIED` | Yes. Full transcript included. |
| [INC-20261024-070040](INC-20261024-070040/report.md) | p95 latency above 5000 ms on recommendation (raised by the alert watcher) | `cartFailure` at 100% (`adHighCpu` had been turned off about a minute earlier) | product-catalog, CPU saturation, high confidence | No action: the fix maps to scaling, which needs Kubernetes; escalated | No: wrong root cause. It did not act on it. |
| [INC-20261024-052210](INC-20261024-052210/report.md) | Error rate above 5% on checkout | Not recorded | Checkout's timeout towards email, medium confidence | Restart approved by a human; dry run | Unknown: the injected fault was not written down |
| [REPLAY-INC-20261023-121354-115804](REPLAY-INC-20261023-121354-115804/report.md) | Replay of a recorded payment diagnosis | `paymentUnreachable` (recorded run) | payment, dependency unreachable | Kill switch engaged: `BLOCKED_BY_KILL_SWITCH`, nothing executed | Yes: the kill switch blocked the action |
