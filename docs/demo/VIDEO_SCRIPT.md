# EcoLoop — 3-Minute Demonstration Script

Owner: Krishnaa Nair. Narration is pre-recorded; the screen is captured
separately. Every number below is measured — no placeholder remains.

Pace: ~150 words/minute. Total narration ≈ 455 words ≈ 3:00.

---

## 0:00–0:20 — The problem and the product

**SCREEN:** Dashboard header — run ID, `EnergyPlus 26.1.0`, `qwen3:8b`, MCP
chip showing `104 calls`.

> Buildings burn forty percent of the world's energy on schedules written years
> ago that never look out of the window. EcoLoop replaces that fixed schedule
> with a closed loop: a live EnergyPlus building, a local open-weight language
> model, real Model Context Protocol tools, and a deterministic safety layer
> that has the final word on every setpoint. Everything you are about to see is
> a real simulation on this machine. Nothing is mocked.

## 0:20–0:45 — Architecture and system health

**SCREEN:** Scroll the closed-loop pipeline strip: Observe → Decide → MCP →
Validate → Actuate → Evidence.

> EnergyPlus streams zone temperature, occupancy, PMV comfort and facility
> power every fifteen simulated minutes into a durable SQLite bus. The agent
> reads that state through MCP tools over stdio — genuine protocol traffic, not
> function calls renamed. Qwen3 8B runs locally on an RTX 4060; no cloud key
> exists anywhere in this repository. The model proposes. It never actuates
> directly.

## 0:45–1:20 — Live EnergyPlus observations

**SCREEN:** Zone state table and demand chart.

> This is one audited run: ninety-six real observations, zero EnergyPlus
> warnings, zero severe errors, zero fatal errors. At eight-fifteen in the
> morning the building reports setpoints of eighteen and twenty-nine degrees
> with a mean zone temperature of twenty-six point seven — occupants are
> getting warm, and the conventional baseline would simply ride it out.

## 1:20–1:50 — Decision and real MCP calls

**SCREEN:** Agent panel, then the MCP tool-call JSON.

> The agent requests current state, then constraints, then scored candidate
> actions, then commits — an enforced tool sequence, one hundred and four calls
> in this run, every one logged. Twenty-six decisions, average latency thirteen
> seconds, and because inference is decoupled from the simulation callback, the
> physics never waits.

## 1:50–2:15 — Safety validation and actuation

**SCREEN:** Action log — the clamped row.

> Here is the part judges should test hardest. The model proposed a cooling
> setpoint of twenty-six degrees. The deterministic validator overruled it and
> clamped to twenty-four, recording the reason: occupied hot condition requires
> maximum safe cooling response. Five clamps in this run. The model is not the
> safety authority — the code is.

## 2:15–2:40 — The building responds, measured

**SCREEN:** Comparison charts.

> Fifteen minutes later EnergyPlus reports the applied setpoints and the zone
> falls from twenty-six point seven to twenty-six point zero. That is closed
> loop, proven end to end. Across a full week comfort compliance rose from
> sixty-eight to ninety percent — while electricity rose ten and a half
> percent. We report that honestly: this baseline looks lean only because it
> lets people overheat thirty percent of occupied hours.

## 2:40–3:00 — The dial, reliability, and the claim

**SCREEN:** The measured-dial chart, then the test summary.

> So we measured the trade-off directly. One policy weight moves the building
> along a real curve: comfort-max costs nine and a half percent more energy for
> ninety percent compliance; energy-lean costs just two percent for eighty-two.
> Two hundred sixty-eight tests pass, and a replay reproduces our results
> digit for digit. EcoLoop does not claim savings it cannot prove. It proves
> every number it claims.

---

## Screen-capture checklist

1. `python -m ecoloop doctor` — all checks pass.
2. Dashboard at `http://127.0.0.1:8501` — header, zone state, agent panel,
   MCP action log (show the clamped row), comparison charts, pipeline strip.
3. Optional B-roll: `python -m ecoloop run agent --period smoke` console.

## Numbers used (all verified)

| Claim | Value | Source |
| --- | --- | --- |
| Live run counters | 96 observations, 26 decisions, 104 MCP calls, 5 clamps, 0 fallbacks | `agent-20260726T172735Z-00385305` |
| EnergyPlus diagnostics | 0 warning / 0 severe / 0 fatal | same run |
| Sense-act-sense | obs 08:15 18/29 °C, zone 26.70 → applied 19/26 → obs 08:30 zone 26.06 | same run |
| Safety clamp | proposed 26 °C → applied 24 °C | same run, generation 11 |
| Week comparison | 1126.96 → 1245.56 kWh (+10.52%); compliance 67.77% → 90.31% | `baseline-…4adce000` / `agent-…65de6a32` |
| Comfort-max day | 217.912 → 238.681 kWh (+9.53%); compliance 69.62% → 90.38% | `baseline-…df519672` / `agent-…4f076a27` |
| Energy-lean day | 217.912 → 222.457 kWh (+2.09%); compliance 69.62% → 81.92% | `baseline-…48166652` / `agent-…ea3913ad` |
| Deterministic tests | 268 passed | full suite |
| Replay equivalence | digit-identical, 170/170 actions | `replay-…5ffa3497` |
