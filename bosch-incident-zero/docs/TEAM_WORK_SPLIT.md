# Team work split (5 people)

One area per person, so each of us owns a part of the code, can explain it to the judges and can change it.
Fill in the names in the table; do not guess them. Status as of 2026-10-24.

| # | Area | Name | One line |
|---|---|---|---|
| 1 | Eyes: telemetry and tools | | Everything the agent can read |
| 2 | Brain: agent, LLM and report | | Diagnosis, guards, confidence, report |
| 3 | Hands: approval, kill switch, actions | | Everything that can change the demo |
| 4 | Detect and run: alerting, autopilot, demo | | Alert to approval page, and the live demo flow |
| 5 | Proof: evaluation, tests, docs, submission | | Test suite, harness, README, team form |

## Rules for everyone

- The agent changes the demo only through the organisers' Action API, and only after a human approves.
- Never read flag state, edit flagd or `demo.flagd.json`, or use the Feature Flags UI to diagnose or inject.
  Inject and reset only with the official `faultctl`.
- Anything that needs an organiser item (`faultctl`, Action API contract, report schema, practice incidents) is
  marked BLOCKED. Do not invent a substitute.
- Safety controls stay in deterministic code, never in the LLM.
- Commits: no `Co-authored-by:` trailer; disclose AI help with `Assisted-by: <tool and model>`.
- Keys go only in the git-ignored `.env`. Run `pytest -q` before every push.
- Stay in your own files. If you must touch another person's area, tell them first.

## 1. Eyes: telemetry and tools

**Owns**

- `src/telemetry/` (Prometheus, Jaeger, OpenSearch clients)
- `src/agent/tools.py` (the 8 read-only tools)
- `src/evidence/`
- `scripts/verify_environment.py`
- Tests: `test_prometheus_client`, `test_jaeger_client`, `test_opensearch_client`, `test_evidence_collector`,
  `test_container_scoping`, `test_flag_sanitization`

**Be able to explain**: what each tool reads, the four statuses (`ok`, `no_data`, `source_unavailable`, `error`),
why flag data is stripped from every result.

**To do**

- Check every tool against the infrastructure faults in the slides (container stopped or restarting, cache or
  database down, network latency, CPU or memory starved), not only the app flags.
- Known gaps to fix or state honestly: `deploy_history` has no source, `dependency_map` is static,
  `system_overview` has no baseline right after a stack restart, Jaeger Monitor shows "No Data" for `accounting`.
- When the organisers' `faultctl` arrives, run `python -m src.agent --tools-only` against each practice incident.

## 2. Brain: agent, LLM and report

**Owns**

- `src/agent/agent.py`, `llm.py`, `prompts.py`, `guards.py`, `transcript.py`, `report.py`, `reviewer.py`,
  `presenter.py`
- `src/analysis/` (rule-based analyzer), `src/memory/`
- `config/incident_schema.json`
- Tests: `test_agent_loop`, `test_dependency_calls`, `test_root_cause_analyzer`, `test_reviewer`,
  `test_incident_memory`, `test_report_outcome`, `test_models`

**Be able to explain**: phase 1 (tool loop, 12 calls) and phase 2 (diagnosis), citation checks, the confidence
gate (high, medium, low), why low confidence escalates, how the model fallback order works.

**To do**

- Unseen incident types in the hidden test: CPU or memory exhaustion, latency without errors, combinations.
  Check the prompts and guards do not assume "errors only".
- Ask the mentors whether the scored root cause for `paymentUnreachable` is `payment` or `checkout`; the change
  is a small edit to the attribution rule (see `EVALUATION_NOTE.md`).
- Swap the report to the official JSON schema as soon as it arrives (`config/`, `report.validate_report`).
- Optional: record token usage in `llm.py`; use the rule-based analyzer as a cross-check.

## 3. Hands: approval, kill switch and actions

**Owns**

- `src/approval/` (gate, pipeline, loop, `chat_ui/`)
- `src/remediation/` (kill switch, planner, backends, verifier)
- `src/action_api/` (local Action API)
- `src/models/action.py`
- Tests: `test_safety_controls`, `test_action_boundary`, `test_action_pipeline`, `test_action_planner`,
  `test_chat_approval`, `test_chat_hub`, `test_remediation_loop`, `test_verifier`, `test_verifier_recorded`

**Be able to explain**: one diagnosis maps to one allowlisted action plus rollback; the approval object is
single-use and expires in about 120 s; the kill switch is checked at plan, post-approval and pre-execute;
a dry run is never reported as a verified recovery.

**To do**

- Replace the local Action API with the organisers' contract when it arrives (URL, auth, action catalog, settle
  time). BLOCKED until then.
- Prove the full loop once: approve, act, settle, verify, roll back on failure, escalate.
- Prove the kill switch stops an action at all three checkpoints, and a reject or timeout means no action.

## 4. Detect and run: alerting, autopilot and demo

**Owns**

- `src/alerting/` (Prometheus watcher)
- `src/autopilot/` (engine, Grafana rules, fixers, server, `ui.html`)
- `scripts/inject_fault.py`, `run_demo.py`, `watch_run.py`
- `src/faultctl/` (the interface for the official `faultctl`)
- Tests: `test_alert_watcher`, `test_inject_fault`, `test_demo_flow`, `test_watch_run`, `test_events`

**Be able to explain**: how a Grafana alert starts exactly one run, the daily cap of 4 runs (free LLM quota),
why Grafana rules and the webhook route are re-installed every 3 minutes.

**To do**

- Commit `scripts/inject_fault.py` and `tests/test_inject_fault.py` (still untracked).
- Rehearse the 5-minute live demo end to end at least twice (see `DEMO_RUNBOOK.md`), including a replay run for
  when the LLM quota is used up.
- Plug in the official `faultctl` the moment it arrives, and check the autopilot fires on the organisers' faults.

## 5. Proof: evaluation, tests, docs and submission

**Owns**

- `src/agent/eval_harness.py`, `scripts/calibration_report.py`
- The whole test suite and its health
- `README.md`, `docs/EVALUATION_NOTE.md`, `docs/TEAM_FORM.md`, `docs/ARCHITECTURE_OVERVIEW.md`,
  `docs/DEMO_RUNBOOK.md`
- `incident_reports/` (the readable and JSON reports we hand in)
- Tests: `test_eval_harness`, `test_calibration_report`, `test_config`

**Be able to explain**: what we can and cannot claim, why no accuracy figure exists yet, how the harness keeps
expected answers away from the agent.

**To do**

- Fix the suite: 12 tests in `test_agent_loop.py` fail because the venv has `httpx2` instead of `httpx`
  (`ModuleNotFoundError: No module named 'httpx'`). Run `.venv/bin/pip install httpx`, then `pytest -q` must
  be green.
- Fill every `<!-- TODO -->` in `TEAM_FORM.md`: names, who built what, assistants used. Do not guess.
- Clean run from scratch on a fresh clone following only the README.
- Run the harness on the practice incidents once they arrive, then update `EVALUATION_NOTE.md` with what we got
  right and wrong and why.
- Final check before freeze: five deliverables are in the repo (source and README, architecture overview,
  incident reports, evaluation note, team form), no key in any tracked file, the last commit is the one we want.

## Demo roles (5 minutes live, 3 minutes questions)

| Part | Who |
|---|---|
| Healthy system, then break it | Person 4 |
| What the agent sees and how it investigates | Person 1 |
| Diagnosis, evidence and confidence | Person 2 |
| Approval, action, verification, kill switch | Person 3 |
| Honest limits, evaluation, architecture slide | Person 5 |

Everyone should be able to answer questions on any part, since judges also interview each member individually.

## Waiting on the organisers

Ask the mentors for these first; each blocks work in the area shown.

| Item | Blocks |
|---|---|
| Official `faultctl` | 1, 4, 5 |
| Action API contract | 3 |
| Official report schema | 2, 5 |
| Practice incidents or offline bundles | 1, 2, 5 |
