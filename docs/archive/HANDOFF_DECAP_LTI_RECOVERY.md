# Build spec: decap regime → LTI recovery (drop the slew/recovery crutches) + joint La/Rpl fit + sink g0 fix

Plan written 2026-06-29 after the decap'd coverage.transient came back and was fit locally. Build in
fresh ultracode. This SUPERSEDES the recovery-network / la_override approach in
HANDOFF_EMIT_BAKE_AND_VCO_RECOVERY.md (Part 0 there) — that was a no-decap-regime crutch.

## What the data proved (all local Spectre, scratchpad/tr_pll_decap.py + cov_*.py)
- Box re-ran the transient WITH `coverage.cdecap=20pF` (the new feature). PLL dip shallowed 310mV(bare)
  → 160mV(20pF), clamp-free.
- Replaying the decap'd tr_pll_2m vs the model:
  - slew + recovery network (compensated.va) = RMS **92–105mV** — it CRASHES to the 300mV anti-windup
    clamp: the recovery net's Rreg=750Ω is ~910Ω in series at the 1ns edge, and the slew can't deliver
    current in ns while the real LDO's loop can.
  - PLAIN LTI (en_ls=0, recovery shorted) = RMS **40mV** at the AC-fit La24/Rpl160; tuned **La≈40µH /
    Rpl≈90Ω → RMS 22mV** (dip 667 vs GT 640).
- CONCLUSION: with the deployment decap the LDO stays ~LINEAR → the plain small-signal Zout is the right
  recovery model. slew + recovery network + la_override (built for the harsh no-decap regime) HURT here.
- Residual SS-vs-LS gap is now SMALL: AC Zout wants La24/Rpl160, the decap'd transient wants La40/Rpl90
  (~1.5–1.8×, both LTI) — not the old 5×.

## Part A — default to LTI; deprecate the recovery/la_override/slew crutches
- emit_pmu_model: the model should DEFAULT to pure LTI Zout (no slew, no recovery network) — that is what
  reproduces the decap'd deployment transient. Keep the slew/recovery/la_override CODE as opt-in (manifest,
  byte-identical when absent) for a genuinely decap-free rail, but they are NOT the default.
- REAL_wur_pmu_top.json: REMOVE `v_out.pll.la_override` + `v_out.pll.recovery` (+ reconsider `slew_a` on
  both rails). The GUI fields for these are already removed (commit 63452e1), so this is consistent.
- en_ls: with no slew emitted, the en_ls A/B switch is moot; keep it only on rails that opt into slew.

## Part B — joint La/Rpl fit over AC-Zout + the decap'd transient (the real fit improvement)
- The decap'd coverage.transient is now a CLEAN, gentle, modelable load-step (the cdecap feature). Fit the
  branch-A La (and Rpl) by minimizing a WEIGHTED sum of (i) the AC-Zout residual and (ii) the decap'd
  load-step replay residual, with the SAME cdecap in the replay TB so the extracted La/Rpl are DECAP-FREE
  (de-embedded → the emitted .va stays decap-free; the user's TB adds the decap).
- Acceptance: one La/Rpl set that gives AC Zout ≲ ~1.5–2dB AND decap'd-transient replay ≲ ~25–40mV (the
  AC-only La24/Rpl160 already gives 40mV; the joint fit should beat that without wrecking the AC grade).
- Data: AC Zout (z_<rail>, already measured, decap-free) + coverage.transient WITH cdecap (decap'd). NO new
  box sim needed beyond the cdecap re-run already done.
- Local harness: cadence/wur_real_tb/replay_pll.py + scratchpad/cov_*.py already do the replay-fit loop;
  productionize into fit_multiport (consume coverage.transient + coverage.cdecap; de-embed; emit La/Rpl).

## Part C — sink g0-source bug (long-pending; now trips all 3 sinks)
- emit path derives sink rout from the FULL-sweep I-V chord (g0=(Is[-1]-Is[0])/(Vs[-1]-Vs[0])), which
  crosses the ~1.7V turn-off knee → ~225× too steep → IVrms 29–37% in the emitted .va; the REPORT grade
  re-fits rout from the AC-admittance DC real part → 0.3–1.2% [OK]. Emit ≠ grade = the bug.
  (fit_multiport._fit_one_current_sink rout vs report_multiport rout=1/ac_y[0].real.)
- FIX: in _fit_one_current_sink derive rout from the AC-admittance DC real part when `cp['y']` present
  (mirror report_multiport), else a POST-knee saturation-region chord — NOT the full sweep. Then emit==grade
  and the sinks emit at ~0.3%. Lock: synthetic knee+flat sink, assert emit-path IVrms ≈ grade-path IVrms.

## Validation
- Local Spectre: the joint-fit La/Rpl reproduces AC Zout + the decap'd transient (acceptance above).
- harness+insitu+netlist suite green; GUI selftest green; default emit byte-clean when the opt-in
  slew/recovery keys are absent.
- Re-export note (for the next box report): the default budget trimmed the voltage @z/@psrr/@noise; to carry
  them, use the Report "export waveforms" checkboxes (Zout+transient only) or raise the export cap.
