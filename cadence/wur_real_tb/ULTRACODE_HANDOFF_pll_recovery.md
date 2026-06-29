# ULTRACODE TASK — fix the PLL LDO load-transient RECOVERY dynamics so the behavioral .va reproduces real silicon

Paste this whole file into a fresh **ultracode** session (it will orchestrate the build via Workflow).
This is a self-contained handoff from a debugging session that already DIAGNOSED the problem and
PROVED a local validation loop. Do not re-derive — build the fix and validate it locally.

---

## GOAL (measurable)
Make the in-situ PMU **voltage-rail** behavioral model reproduce the **real-silicon load-transient
VDD** of the PLL rail when driven by the real load current. Specifically, replaying the real current
through the model in local Spectre must match `real_V_VDD0P8_PLL.txt`:
- **dip** ≈ 712 mV @ ~25 ns (currently 712 @ 19 ns — depth ok, ~6 ns early)
- **steady (switched-average)** ≈ 776–779 mV (currently matched only by a param hack)
- **DC setpoint preserved = 0.8 V** (HARD constraint — silicon DC @0.5 mA IS 0.8 V, confirmed by the
  manifest load sweep; do NOT lower vreg / bake a setpoint offset)
- **recovery = slow, MONOTONIC, OVERDAMPED** (silicon climbs 712→776 over ~100 ns with NO overshoot)
- **target: overall RMS < ~5 mV over 0–200 ns** (current best param-tune = 16.8 mV, all residual is the
  recovery-shape overshoot in the 30–125 ns window: +18 mV mean)
- **stable**: a clean 0.5→1 mA load step must dip+recover monotonically, NOT swing past 0…1.0 V.
- **no regression** on the other ports/metrics (current sinks, other rails) or the test suites.

## THE VALIDATED LOCAL LOOP (use this to iterate — NO box runs needed)
Local Spectre is available (`cadence/spectre_run.py`, `spectre_run.available()==True`).
Harness: `cadence/wur_real_tb/replay_pll.py` — feeds a PWL current into a single-rail `ldo_pll` .va
(Vs=1.0 V, Cd=20 pF external decap), runs local Spectre, compares vout to the real silicon V.
- `python3 replay_pll.py <model.va>`            → replay real current, RMS + windowed residual vs silicon
- `python3 replay_pll.py <model.va> --dc 0.5e-3` → DC load-reg point (MUST stay ~800 mV)
- `python3 replay_pll.py <model.va> --step`      → clean step (MUST NOT ring past rails / overshoot)
This loop is PROVEN faithful to box Spectre to **1.43 mV** with the 2000-pt current (the box `model_V`
was the SAME current fed to the model on the box; local replay reproduces it).

## DATA / ARTIFACTS (all in `cadence/wur_real_tb/`)
- `real_pmu_iload_PLL_2000.txt`   — real silicon PLL load current, 2000 pt, 0–200 ns (replay INPUT; t,I)
- `real_V_VDD0P8_PLL.txt`         — real silicon VDD (the TARGET), 0–300 ns
- `model_V_VDD0P8_PLL.txt`        — box model output for the same current (loop self-check)
- `ldo_pll_baseline_noslew.va`    — old model (Zout branches only, no slew)
- `ldo_pll_shipped_slew.va`       — current shipped model (slew core, La=24 µH, SRa=12000)
- `ldo_pll_besttune.va`           — param-tuned best (La=120 µH, SRa=9000): RMS 16.8 mV, dip+ss+DC ok,
                                    but STILL overshoots in 30–125 ns (the unsolved part)
- plots: `model_vs_silicon_clean.png`, `new_slew_vs_silicon.png`, `tuned_va_vs_silicon.png`,
  `zoom_0_130ns.png` (the overshoot), `model_step_ringing.png` (0.28→1.01 V clean-step ring)

## DIAGNOSIS (established — don't redo)
The current rail model = ideal source `vreg=0.8` + Zout branches (branch A `(La||Rpl)+Ra`, branch C
`Cout+ESR`, branch B off) + a branch-A `slew(V(nA,vrg)/Ra, SRa)` core. Findings:
1. **DC is correct (0.8 V).** The "20 mV steady gap" is NOT a setpoint offset — it's that the model's
   RECOVERY is too fast, so under the continuous switching load it sits higher than silicon (which is
   still slowly recovering). Raising La (slower recovery) lowers the switched-average to 776 AND keeps
   DC=0.8 — confirming it's recovery-speed, not setpoint.
2. **The dip** is a slew-limited response to fast load di/dt — the slew mechanism is correct for the dip.
3. **THE CORE DEFECT = recovery SHAPE.** The model is UNDERDAMPED: after the dip it rebounds fast and
   OVERSHOOTS to ~790 mV @50 ns (clean step: swings 0.28→1.01 V, past the supply). Silicon recovers
   SLOWLY + MONOTONICALLY (overdamped), staying below the settled value the whole way up. Residual is
   100% concentrated in 30–125 ns (+18 mV). The model lacks the LDO loop's slow overdamped dominant-pole
   recovery; the Zout-branch fit captured the fast HF shelf/resonance but not the slow loop settling
   (same root as the report's "Zout resonance mislocated 1 MHz vs 10 MHz", in the time domain).
4. **Structural quick-hacks already tried and RULED OUT** (don't repeat):
   - Asymmetric slew `slew(.,SRup,SRdn)`: limiting the falling rate kills the overshoot but RUINS the dip
     (755 @100 ns) — dip & overshoot are both slew-controlled, can't decouple.
   - Series-R damping on La: no effect on overshoot + adds DC droop.
   - Naive behavioral loop (gm err-amp → Rc||Cc dominant pole → pass gmp slew): DC perfect (800.00 mV)
     but TRANSIENT DIVERGES under the GHz switching (vout → ±thousands mV) — a MHz-bandwidth loop +
     output integrator + slew + GHz excitation is unstable WITHOUT proper phase-margin compensation.

## WHAT TO BUILD (the real fix)
A **properly-compensated behavioral LDO rail model** that decouples the three behaviors and is stable:
- **DC/low-freq**: high loop gain → low Zout → regulates to 0.8 V (DC preserved, no baked offset).
- **Recovery**: a slow, OVERDAMPED dominant-pole loop response (monotonic, ~100 ns, matches silicon).
  Needs real compensation (Miller / dominant-pole at an internal high-Z node, an ESR/comp zero for phase
  margin) so it does NOT ring under the GHz switching current.
- **Dip**: fast large-signal undershoot via pass-device slew (keep the validated slew mechanism).
- **Consistency**: must remain consistent with the AC Zout characterization (the rising shelf +
  resonance) — i.e. the SAME model fits the small-signal Zout AND the time-domain recovery.
Explore a small PANEL of topologies and pick the best (e.g. (a) gm-amp + Miller-compensated pass stage +
slew; (b) an explicit overdamped 2nd-order Zout(s) with a slew front-end; (c) asymmetric-slew + a
dominant-pole recovery filter). Score each on: matches silicon recovery shape, stable under GHz load,
DC=0.8 preserved, AC-Zout consistent, emit/compile clean, byte-identical when the new term is off.

## FIT-FLOW INTEGRATION (the productized answer to "what does the modeling flow consume")
- KEEP consuming the EXISTING characterization: the **Zout AC sweep** (`ac 10..500M`, already run — it
  contains the loop dominant pole that sets the recovery; the recovery time ↔ low-freq Zout) PLUS a
  **clean load-step transient** (`coverage.transient`, for the large-signal slew). Do NOT depend on
  ad-hoc long transients — those were only for this debug.
- Fix the fit to extract the **low-frequency loop pole / recovery time-constant** (the current fit
  under-estimates the effective La by ~5×: gives 24 µH, needs ~120 µH-equivalent), the compensation/
  damping, and the slew — from that AC + step data.
- Code touch-points: `harness/fit_multiport.py` (`_fit_voltage_output` [note the hardcoded
  `vout_dc=0.8`], `_voltage_body`, `_branchA_reg`, `_slew_param`, `_build_vreg_schedule`),
  `harness/emit_pmu_model.py` (`_voltage_block`, `_voltage_block_scheduled`, `_branchA_reg`,
  `_voltage_body`). New mechanism must be **opt-in / byte-identical when off** (same gating discipline as
  the existing slew_a / cft).

## VALIDATION (REQUIRED — this is the user's explicit acceptance test)
After building the model + fit, **use the LOCAL data to model and replay the iload, and check it
reproduces the real LDO curve**:
1. Emit the new PLL single-rail `ldo_pll` .va from the new model/fit.
2. `python3 cadence/wur_real_tb/replay_pll.py <new.va>` → must hit the GOAL targets above
   (RMS < ~5 mV, monotonic recovery, dip 712@~25 ns, ss ~776–779).
3. `--dc 0.5e-3` → ~800 mV (DC preserved).  `--step` → no ring past rails, no overshoot.
4. Re-validate against the in-situ digest (the `[MPD1]` report / `report_multiport`) — no regression on
   other ports/metrics. Run the test suites (`harness/`, `cadence/insitu/`) + the local-Spectre parse
   gate on emitted cards + GUI selftest.
5. Produce an overlay plot (new model vs silicon vs the besttune baseline) and report the RMS + windowed
   residuals (use the 0-30 / 30-125 / 125-200 ns windows — the 30-125 ns one is the one to crush).

## CONSTRAINTS / GUARDRAILS
- DC setpoint MUST stay 0.8 V (no setpoint offset).
- New mechanism opt-in; default path byte-identical to today's emit.
- Transient MUST be stable (no divergence / ring past rails) under the real switching current.
- No regression: existing suites green, emitted cards parse in local Spectre, current-sink + multi-rail
  models unchanged.
- Iterate entirely on the local replay loop; no box runs for development.

## NOTE / NICE-TO-HAVE DATA
The real_V tail is truncated at 300 ns; the recovery is still climbing there. A longer real-silicon VDD
(~1–3 µs, same TB run) would let the fit pin the recovery time-constant precisely. The build can proceed
with the 300 ns data + the AC Zout (which carries the pole); flag where the truncation limits the fit.

## SUGGESTED WORKFLOW SHAPE
Phase 1 Understand — read the current model/fit/emit + this diagnosis; confirm the local loop reproduces
  `besttune.va` (RMS 16.8). Phase 2 Design panel — N candidate compensated-loop topologies, each scored
  (shape / stability / DC / AC-consistency) on the local loop. Phase 3 Implement the winner (VA + fit +
  emit, opt-in). Phase 4 Validate (local replay vs silicon + DC + step + suites + digest, adversarial
  stability check). Phase 5 Iterate until RMS target + all guardrails hold; commit + push.
