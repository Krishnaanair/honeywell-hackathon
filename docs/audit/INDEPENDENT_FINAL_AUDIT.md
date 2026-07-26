# Independent Final Audit

Role: hostile judge / independent QA. Date: 2026-07-26 (22:00–00:15 IST).
Nothing below is taken from prior documentation claims; every number was
produced by commands executed in this session or recomputed read-only from
`runs/ecoloop.db`. Environment: Windows 11 Pro 10.0.26200, Python 3.11.9,
EnergyPlus **26.1.0** (`ENERGYPLUS_HOME=C:\Users\krish\EnergyPlusV26-1-0`),
OSS model **`qwen3:8b`** on local Ollama (loopback `127.0.0.1:11434`,
100% GPU on an RTX 4060 Laptop GPU), MCP transport **stdio** (FastMCP server +
official client session).

## 1. Commands executed and exit codes

| # | Check | Command | Result | Exit |
| --- | --- | --- | --- | --- |
| 1 | Environment doctor | `python -m ecoloop doctor --json` | `ok: true`, 14/14 checks PASS | 0 |
| 2 | Backend tests | `python -m pytest -q -m "not real_energyplus and not real_ollama and not real_closed_loop"` | **260 passed**, 6 deselected, 57.82s | 0 |
| 3 | MCP server/client tests | included above (`tests/integration/test_mcp_stdio.py` et al.) — real stdio subprocess round trip | pass | 0 |
| 4 | Safety and fault-injection tests | included above (`test_safety`, `test_timeout_circuit_breaker`, `test_fake_closed_loop` failure paths) | pass | 0 |
| 5 | Frontend tests / build | Streamlit AppTest suites included above; no separate JS build exists (Python dashboard) | pass / n-a | 0 |
| 6 | Real EnergyPlus smoke | `python -m pytest -q -m real_energyplus` | **4 passed**, 109.27s | 0 |
| 6b | Real Ollama smoke | `python -m pytest -q -m real_ollama` | **1 passed**, 15.05s | 0 |
| 7+8 | Baseline + agent simulations | `python -m pytest -q tests/real/test_closed_loop_smoke.py::test_real_energyplus_mcp_ollama_closed_loop_smoke -o tmp_path_retention_policy=all` | **1 passed in 472.87s** (fresh baseline + fresh agent run + comparison) | 0 |
| 8b | Live agent simulation (production store) | `python -m ecoloop run agent --period smoke` | completed, verified | 0 |
| 9 | Metric recomputation | read-only SQL recomputation of every published number from raw telemetry/audit rows | all confirmed | 0 |
| 18 | Baseline/agent compatibility | preparation-fingerprint + weather-hash + exact-window gates exercised by suite and by `python -m ecoloop compare` | pass | 0 |
| 20 | Repeat critical closed-loop | smoke executed twice today (22:03–22:11 recovered evidence; 23:03–23:10 captured standalone) | both passed, digit-identical totals | 0 |

Also executed: `python -m ecoloop replay` for both retained agent runs
(digit-identical reproduction, §5), `python -m ecoloop compare` for the
one-day pair, `ruff check` / `ruff format --check` / `mypy src` (all clean).

## 2. Real run IDs used as evidence

| Run | ID |
| --- | --- |
| Retained one-day baseline | `baseline-20260726T151534Z-df519672` |
| Retained one-day agent | `agent-20260726T151601Z-4f076a27` |
| Week baseline | `baseline-20260726T105124Z-4adce000` |
| Week agent | `agent-20260726T130255Z-65de6a32` |
| Live agent run (this audit session) | `agent-20260726T172735Z-00385305` |
| Smoke-test pair (standalone rerun, pytest tmp) | `baseline-20260726T170305Z-ce8c8199`, `agent-20260726T170317Z-cf8ebc53` |
| Replay proofs | `replay-20260726T165649Z-6ff9f492` (one-day), `replay-20260726T171617Z-5ffa3497` (week) |
| Diagnostic rule run (never compared) | `rule-20260726T160009Z-e2de2c52` |

## 3. Quantitative results (recomputed from raw stored data)

### One-day pair (persisted comparison, `runs/agent-20260726T151601Z-4f076a27/export/comparison.json`)

| Metric | Baseline | Agent | Delta |
| --- | ---: | ---: | ---: |
| Facility electricity (kWh) | 217.91226189328628 | 238.68056748751272 | **energy saving −9.5306 % (an increase)** |
| Peak demand (kW) | 22.273230537572715 | 23.35430606664108 | **peak reduction −4.8537 % (an increase)** |
| Comfort compliance (occupied temp) | 69.61538 % | 90.38462 % | **+20.769 pp** |
| Occupied violation | 30.3846 % | 9.6154 % | −20.769 pp |
| Cost (configured tariff) | 26.14947 | 28.64167 | −9.53 % (higher) |
| Carbon (kg CO2e) | 152.53858 | 167.07640 | −9.53 % (higher) |

### Representative week (verified stored comparison)

| Metric | Baseline | Agent | Delta |
| --- | ---: | ---: | ---: |
| Facility electricity (kWh) | 1126.9604940141178 | 1245.564416842928 | energy saving −10.5242 % (an increase) |
| Peak demand (kW) | 23.006487867801226 | 24.2293835189757 | peak reduction −5.3154 % (an increase) |
| Comfort compliance | 67.76923 % | 90.30769 % | +22.538 pp |
| PMV compliance | 94.61538 % | 97.46154 % | +2.846 pp |
| Cost | 135.235 | 149.468 | −10.52 % (higher) |
| Carbon (kg CO2e) | 788.872 | 871.895 | −10.52 % (higher) |

**There is no honest energy-saving claim available from these runs.** The
agent trades electricity for large comfort gains. Every percentage above was
recomputed independently from `telemetry`/`zone_telemetry` raw rows and
matches the stored verified metrics to ≤1e-13 kWh.

### Live audit-session agent run `agent-20260726T172735Z-00385305`

Completed and verified: 26 decisions, 104 MCP tool calls, 26 applied actions,
0 rejected/invalid actions, 0 timeouts, 0 fallbacks, **5 safety clamps**,
0 EnergyPlus warnings / 0 severe / 0 fatal. Facility electricity
222.4567741294708 kWh — digit-identical to both smoke-test agent runs
executed today under the same configuration (deterministic preparation and
`temperature=0, seed=0` inference), giving three independent byte-identical
closed-loop executions.

### Counters on the retained evidence pairs

| Counter | One-day agent | Week agent |
| --- | ---: | ---: |
| LLM decisions | 25 | 170 |
| MCP tool calls | 99 (all success) | 788 (784 success, 4 failed calls surfaced) |
| Applied actions | 25 | 170 |
| Invalid/rejected proposals | 0 | 13 (all rejected safely) |
| Fallback activations | 4 | 11 |
| Safety clamps | 3 | 24 |
| Inference timeouts | 4 (fallback covered) | 0 |
| EnergyPlus warning/severe/fatal | 0 / 0 / 0 | 0 / 0 / 0 |

Disclosed: the week agent's audit log contains 6 deduplicated agent-side
`severe` records (coordinator/MCP shutdown races and fallback diagnostics);
EnergyPlus itself reported zero severe/fatal. Pre-migration runs carry
physical actuation evidence as verified legacy callback messages; runs from
migration 003 onward (including the live run) carry structured
`actuator_applications` rows.

## 4. Closed-loop trace evidence (from the live run, read directly from the store)

Required chain: EnergyPlus observation → OSS LLM decision → genuine MCP call →
safe control plan → active actuator update → subsequent EnergyPlus response →
quantitative comparison.

Tool-call sequence (first 12 of 104): `get_current_building_state` →
`get_constraints` → `generate_candidate_actions` → `apply_control_action`,
repeating — the host enforces this order and the smoke test asserts it.

Five traces (of 26; all have the same structure):

1. obs 4587 (00:15, 18/28 °C, zone 24.313 °C) → `decision-7617ce22…` qwen3:8b,
   13841 ms → `action-4cbaddfd…` gen 1 accepted, applied 18/28 → obs 4588
   reports 18/28, zone 24.208 °C.
2. obs 4591 (01:15) → `decision-b49421ec…` 13193 ms → gen 2 applied 18/28 →
   obs 4592 zone 23.737 °C.
3. obs 4595 (02:15) → `decision-9e0153b3…` 13256 ms → gen 3 applied → obs 4596.
4. obs 4599 (03:15) → `decision-3ac1968f…` 13270 ms → gen 4 applied → obs 4600.
5. obs 4603 (04:15) → `decision-d97662a1…` 13319 ms → gen 5 applied → obs 4604.

Setpoint-changing and safety-overridden traces from the same run:

- gen 9: obs 4619 (08:15, 18/29 °C, zone 26.698 °C) → applied **19/26 °C** →
  obs 4620 (08:30) reports 19/26 with zone 26.059 °C (occupied comfort
  recovery physically visible).
- gen 10: obs 4623 (09:15) → applied 19/25 → obs 4624 zone 25.068 °C.
- gen 11: obs 4627 (10:15) → model proposed 19/**26** but the deterministic
  validator **clamped cooling to 24.0** with recorded reason
  `"occupied hot/PMV condition requires maximum safe cooling response"`;
  obs 4628 (10:30) physically reports 19/24 and zone 24.123 °C. The model is
  demonstrably not the safety authority.

Average decision latency 13253.87 ms, p95 14132.85 ms; the EnergyPlus callback
uses a bounded handshake and never blocked on inference indefinitely.

## 5. Replay reproduction (executed this session)

`python -m ecoloop replay <agent-run>` rebuilds an actuation schedule from the
source run's verified physical acknowledgements and immutable model snapshot,
then reruns EnergyPlus without any model in the loop:

| Source | Replay | Facility kWh (source → replay) | Identical? |
| --- | --- | --- | --- |
| `agent-…-4f076a27` | `replay-20260726T165649Z-6ff9f492` | 238.68056748751272 → 238.68056748751272 | yes, all metrics |
| `agent-…-65de6a32` | `replay-20260726T171617Z-5ffa3497` | 1245.564416842928 → 1245.564416842928 | yes, all metrics (170/170 actions) |

## 6. Authenticity checks

- **Model is open-weight and self-hosted:** Ollama `qwen3:8b`, loopback-only
  allowlist, `trust_env=False`, redirects refused; the backend raises if
  `OLLAMA_API_KEY` is set; zero matches for hosted providers in `src`; every
  decision row records the model name.
- **MCP is genuine:** stdio server subprocess + official client session with
  `initialize`/`list_tools`/`tools/call`; protocol integration tests launch
  the real transport; 104 audited calls in the live run alone.
- **Invalid actions rejected:** schema-invalid and unknown-field tool inputs
  rejected (suite); 13 invalid proposals on the week run rejected at
  validation; stale/expired/wrong-run/duplicate actions rejected (store
  suite).
- **Timeout → fallback:** deterministic timeout/circuit-breaker suite green;
  the one-day agent run shows 4 real inference timeouts absorbed by 4 safe
  fallbacks with the loop completing cleanly.
- **Baseline/agent compatibility:** identical preparation fingerprint
  (`4ea4d121…`), weather SHA-256 (`c7d4efcf…`), period, exact simulation
  window; enforced at selection and publication; incompatible pairs
  (including the diagnostic rule run) refuse to compare.
- **EnergyPlus logs:** 0 warnings / 0 severe / 0 fatal on every retained and
  audit-session run; the one run with a severe error today (a scratch-file
  collision between two concurrent simulations) was honestly marked `failed`,
  root-caused, fixed with per-run child working directories, and covered by
  two new regression tests — the fix is why check 20's standalone rerun is
  clean.
- **Mock/fake/hardcoded sweep:** zero submission blockers; all fake adapters
  are hermetic test fixtures or the explicit `--fake`/`is_fake` boundary
  excluded from every production selector; no hardcoded KPI anywhere in the
  dashboard or reporting paths; no skipped/xfail tests (only precise runtime
  dependency skips inside `tests/real/`).
- **Dashboard authenticity: AUTHENTIC.** Every KPI is computed in
  `dashboard/queries.py` from `runs/ecoloop.db` (the same store the
  coordinator writes); fake rows are excluded; comparison and replay labels
  fail closed without verified evidence; Streamlit AppTest runs inside the
  260-test suite; the live server on `127.0.0.1:8501` was inspected in a
  browser against the store during this audit.

## 7. Verdict

**SUBMISSION READY WITH DISCLOSED LIMITATIONS**

The full required chain — EnergyPlus observation → OSS LLM decision → genuine
MCP call → safe control plan → active EnergyPlus actuator update → subsequent
EnergyPlus response → quantitative comparison — is proven end-to-end,
repeatedly, with digit-identical reproducibility and a measured replay.

Disclosed limitations that prevent an unqualified verdict:

1. **The evaluated configuration does not save energy.** Both compatible
   pairs show 9.5–10.5 % more electricity with large comfort gains. All
   artifacts state this plainly; no savings claim may be made.
2. Physical actuation evidence for pre-migration runs uses the verified
   legacy callback-message path (structured rows exist from migration 003
   onward).
3. No dedicated `smoke-energyplus` CLI command; the equivalent evidence comes
   from the marker-gated real pytest suites and the doctor command.
4. The submission bundle carries reports, hashes, and run IDs, not the full
   local `runs/` evidence store (documented).
5. The callback→decision handshake is bounded rather than fully asynchronous
   (documented design note).
