# Incident INC-20261024-070040

**Status:** ESCALATED - Escalated to a human - no verified fix.

**Final remediation status:** UNRESOLVED_ESCALATED - Not resolved - escalated to a human.

NO_ACTION_ESCALATED: cpu_saturation maps to SCALE_SERVICE, which needs Kubernetes (DEPLOY_PLATFORM=docker)

| Field | Value |
|---|---|
| Diagnosed at | 2026-10-24T07:05:06+00:00 |
| Target service | product-catalog |
| Severity | HIGH |
| Confidence | 0.85 |
| Category | cpu_saturation |
| Impacted services | recommendation, frontend, frontend-proxy |

## Root cause

product-catalog service is experiencing sustained high CPU utilization (74-77%) causing severe latency increases in its GetProduct and ListProducts operations, which cascades to recommendation and frontend services.

## Evidence

- trace `dc7f62e5130c2039276a273ae1949a67`
- trace `3fb4bcceec802b180efd95097c54d73b`
- `metric:product-catalog:p95_ms` = 5877
- `metric:product-catalog:cpu_pct` = 74
- `metric:product-catalog:server:oteldemo.ProductCatalogService/GetProduct:p95_ms` = 6579
- `metric:product-catalog:server:oteldemo.ProductCatalogService/ListProducts:p95_ms` = 4391
- `metric:product-catalog:client:astronomy-db:p95_ms` = 824.2
- `metric:recommendation:client:oteldemo.ProductCatalogService/ListProducts:p95_ms` = 6046
- `metric:recommendation:internal:get_product_list:p95_ms` = 6046
- `metric:recommendation:cpu_pct` = 1.2
- `container:product-catalog:cpu_pct_5m` = 75.9
- `container:product-catalog:state` = running
- `container:recommendation:cpu_pct_5m` = 1.2
- `container:astronomy-db:cpu_pct_5m` = 1.7

## Remediation

- Recommended: Investigate product-catalog CPU hot spots (profiling, query plans, recent config/code changes). The service is the single point of contention driving the entire frontend latency cascade.
- Approval: PENDING (required: True)
- No action taken.

## Remediation attempts (1 of max 3)

| # | Action | Approval | Executed | Verification (before -> after) | Outcome |
|---|---|---|---|---|---|
| 1 | none | not asked | n/a | n/a | NO_ACTION_ESCALATED: cpu_saturation maps to SCALE_SERVICE, which needs Kubernetes (DEPLOY_PLATFORM=docker) |

## Timeline

| At | Event | Status | Detail |
|---|---|---|---|
| 2026-10-24T07:05:06.789+00:00 | PLAN | n/a | attempt 1/3: no action: escalated (cpu_saturation maps to SCALE_SERVICE, which needs Kubernetes (DEPLOY_PLATFORM=docker)) |
| 2026-10-24T07:05:06.789+00:00 | ESCALATION | n/a | attempt 1/3: UNRESOLVED_ESCALATED: NO_ACTION_ESCALATED: cpu_saturation maps to SCALE_SERVICE, which needs Kubernetes (DEPLOY_PLATFORM=docker) |

## Recovery verification

Not verified - no verification was run.

## Prevention

- Alert on sustained CPU utilisation of product-catalog.
- Review CPU limits and the scaling policy for product-catalog.
- Profile the hot path of product-catalog under load.

Audit log: `/Users/vishwasreddy/Desktop/bosch/team8-otel-demo/bosch-incident-zero/audit_logs/audit.jsonl`
