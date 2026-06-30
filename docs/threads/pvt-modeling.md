# Thread: single-corner → PVT modeling — ACTIVE

owner: weisbert · last-touched: 2026-06-30 · opened from the FF "trans negative" reflection (see large-signal-recovery)

## Why this thread exists
The FF "trans goes negative" bug was **load-driven** (established in large-signal-recovery: the behavioral
model has ZERO process dependence; the real PLL/VCO just draws more current at FF → deeper dip). But it
exposed a real, separate blind spot: **the LDO model is fit at ONE corner (house nominal tt_55c / synthetic
tt_25c) and emits identically at every declared PVT corner.** A system sim at SS / hot / min-V gets TT
behavior. Question: is that actually dangerous, and what's the path to PVT-correct?

METHODOLOGY §Core already ADOPTED the answer in principle — **route A: characterize each PVT cell
separately → fit separately → emit `.lib` SECTIONS; cross-PVT interpolation REJECTED; continuous PTAT
i(T) the only exception.** This thread **empirically tested that decision locally** (the standard "prove the
method on synthetic transistor GT before the box" pattern) and **refined it**.

## What was done (2026-06-30, local ngspice, scratchpad → `research/pvt_modeling/`)
A controlled PVT experiment on the synthetic **transistor-level** GT (`ground_truth/ldo_gt.cir`, BSIM3 —
Vth0/U0/Tox are skewable). Synthesized FF/TT/SS by gentle OP-preserving skew (±30 mV Vth, ±8 % µ, ±2 % Tox),
swept Vin (0.95–1.30 V) and T (−40…125 °C), measured Zout(LF/peak/Q), PSRR(LF/worst), DC dropout/current-
ceiling, and the 1 mA load-step dip — with a **per-corner OP saturation guard**. Cross-checked on a 2nd DUT
(`ldo_v3_miller.lib`, two-stage Miller, Cout=1 nF). Scripts: `research/pvt_modeling/pvt_experiments.py`
(+`analyze.py`, `pvt_v3.py`); raw data `results_ldo_gt.json` / `results_v3_miller.json`.
**(Local Spectre is verification-OK; only the PRODUCTION derive path is Spectre-forbidden. This is ngspice.)**

## Headline: a single-corner model is silently OPTIMISTIC at the stressed corners
Model ships TT (`Iceil 48 mA, PSRRworst 9.6 dB, dip 120 mV, Zpk 519 Ω`) → emitted at EVERY corner. Reality:

| corner | I-ceiling claim/real | PSRR-worst claim/real | dip claim/real | Zout-pk claim/real |
|---|---|---|---|---|
| **HOT 125 °C** | 48 / **6 mA (8.5× over)** | 9.6 / 2.7 dB (+7) | 120 / 165 mV (**−27 %**) | 519 / 501 Ω |
| **SS @27** | 48 / 44 mA (1.1×) | 9.6 / **−1.3 dB (+11)** | 120 / 129 mV (−7 %) | 519 / **1592 Ω (0.33×)** |
| **LOW 0.95 V** | 48 / 27 mA (**1.8×**) | 9.6 / **−1.8 dB (+11)** | 120 / 82 mV (+46 %) | 519 / 600 Ω |
| **COLD −40** | 48 / 60 mA (0.8×) | 9.6 / 5.2 dB (+4) | 120 / 151 mV (−21 %) | 519 / 612 Ω |

- **I-ceiling 8.5× over-claimed hot** = the dangerous one (this is the assist `iaG` analog — a TT-fit current
  ceiling tells the system there's 8× more headroom than the silicon has).
- **PSRR worst +11 dB over-claimed** at SS and low-V (model says 9.6 dB rejection; reality is **negative** =
  supply ripple *amplified*). The loop is near-unstable there; the TT model under-states the Zout resonant
  peak **3×** (SS) and can't see it.
- **dip −21…−27 % under-predicted** at temperature extremes (says the rail holds up better than it does).

All optimistic directions are at the **stressed** corners. **Cross-confirmed on v3_miller**: SS raises the
Zout peak/LF Q-proxy 20.5→35.7 (stability erosion, gain stage saturated → trustworthy); HOT is catastrophic
(Zout LF ×138, PSRR→−0.1 dB, ceiling −67 %, dip +659 %, regulation lost). Magnitudes differ (smaller pass
device, 1 nF Cout) but **every sign matches** ldo_gt.

## What it says about the route-A decision (the refinement)
1. **PROCESS → discrete `.lib` SECTIONS, mandatory.** Corners change a device's operating REGION (ldo_gt
   FF tips mirror m4 into triode; both DUTs sit near region edges). You cannot interpolate across a region
   change. TT-from-FF&SS interp error 51–87 % on loop observables. **Confirms route-A; confirms the
   interpolation REJECTION — empirically, not by assertion.**
2. **TEMPERATURE → continuous, but only for MONOTONIC quantities.** Densely-interpolatable for I-ceiling
   (local-neighbor err 6 %) and dip (4 %) — these extend the "continuous PTAT i(T)" exception. But the
   **stability observables are NON-monotonic in T** (Zout peak & PSRR notch peak at an *interior* ~10 °C, not
   an extreme): local-interp err Zpk 75 %, PSRRworst 101 %. → temperature needs **≥3 points (coverage already
   does −40/55/125) AND the stability worst may be interior**, so a stability sweep must FIND it, not just
   sample the box corners.
3. **OUTPUT-SETPOINT `vreg` (≈0.8 V) → the primary voltage axis, and the sharpest gap.** This is the model's
   ONE exposed knob (`vreg_<rail>`), but minimal-emit BAKES Zout/PSRR/ceiling at one OP — turning `vreg`
   shifts only the DC level, the small/large-signal characteristics DON'T track it. The silicon does:
   raising vreg 0.80→0.95 (headroom 0.25→0.10 V, Vin=1.05 fixed, TT/27) erodes Q 2.9→**17.6×**, PSRR worst
   16.8→**4.3 dB**, I-ceiling 60→39 mA. ⇒ a user who sets vreg above the fit point gets a **silently
   optimistic** rail on the knob they control. (`research/pvt_modeling/pvt_vreg.py`, `results_vreg.json`.)
   TENSION the model must hold: higher vreg = SAFER dip floor (min=vreg−dip: 0.874 vs 0.693 V) but WORSE
   ceiling/stability/PSRR — both move with vreg, opposite signs.
   (Secondary: the SUPPLY/line axis — AVDD, fixed ~1.0 V in deployment — moves PSRR/ceiling too; the unwired
   `dc_linereg` is the BACKLOG [MAJOR] line-reg item. Distinct from the setpoint axis above.)
4. **NEW actionable insight:** the worst-case stability corner can be **interior** (≈10 °C / 0.95 V here),
   not a foundry box extreme → corner selection needs a screening sweep, not just the PVT cube vertices.

## Caveats (don't over-read)
- Synthetic TOY DUTs; skew is conservative (OP-preserving) → the *process* sensitivities are **lower bounds**.
- **HOT magnitudes are an UPPER bound (toy bias).** Both toys use a crude fixed-Ibias + ideal Vref + a
  marginal OTA output swing — NOT a bandgap-referenced production LDO. So the absolute hot-collapse numbers
  (incl. the "8.5× hot I-ceiling" headline and the 125 °C regulation loss) are **inflated by toy bias
  fragility**; the **direction (hot worse, less-headroom worse) is robust**, but a real T-compensated LDO
  degrades less hot. At 125 °C the toy can't regulate low setpoints (Vout floats ~0.96–1.0 V) → the hot
  vreg-sweep ceiling entries are regulation-loss artifacts, not capability.
- ldo_gt FF = m4 triode, v3_miller mp = triode (both near edges) → absolute small-signal magnitudes at those
  corners are degraded; the **directions and the cross-DUT agreement** are the load-bearing evidence.
- High-Q Zpk is **under-sampled** by the uniform 30 pt/dec AC grid (HWHM ~111 kHz vs grid ~900 kHz @12 MHz)
  → peak magnitudes are lower bounds; the near-instability is confirmed independently by PSRR going negative.
  (Same lesson as the project's spur-sampling rule — needs adaptive freq sampling around the peak.)
- This is **method verification on synthetic GT**, NOT the deliverable. The real route-A execution is
  per-corner characterization of the actual silicon on the box.

## Next action
Decision for the user / for a build session — NOT yet built (touches the pipeline → needs the ultracode go):
1. **Execute route-A** on the real PMU: characterize a SMALL bounding corner set (the screen says
   SS / hot / min-V bound the optimistic failures; find the interior worst-stability point by a sweep), fit
   each independently, emit corner-keyed `.lib` SECTIONS (today UN-implemented — emit produces ONE card).
2. **Make the assist `iaG` (current ceiling) corner-aware first** — it's the one parameter whose
   TT-blindness is *signed-dangerous* (8.5× hot over-claim). Quick, high-value, ahead of the full section build.
3. **Add an envelope guard** — corner tag + warn/conservative-fallback on un-characterized corners (the PVT
   analog of the `floor` seatbelt + the carrier-band envelope), so off-corner sims are loud, not silently-TT.
4. **(method) clean process axis** on a robustly-biased DUT (or re-bias) to remove the triode caveat; add
   adaptive freq sampling around the Zout/PSRR peak so high-Q magnitudes aren't lower-bounded.

## Checklist
- [x] Confirm local GT is transistor-level & skewable (BSIM3 Vth0/U0/Tox) — yes, all `ground_truth/*.lib`
- [x] PVT grid sweep (process × Vin × temp) on bias-fixed ldo_gt + OP guard
- [x] Optimism quantified (TT-ships-everywhere vs real corner) — 8.5× hot I-ceiling, +11 dB PSRR SS/low-V
- [x] Interpolation-fails analysis — process (region change) & temp (non-monotonic stability) both fail
- [x] Temperature local-vs-extreme interp — monotonic OK / stability not
- [x] Voltage axis quantified — OUTPUT SETPOINT `vreg` (the exposed knob; supply is the separate line axis)
- [x] 2nd-DUT cross-check (v3_miller) — directions confirmed
- [ ] route-A `.lib` SECTIONS emit (UN-implemented today) — build-session item
- [ ] corner-aware `iaG`; envelope guard; clean process axis + adaptive peak sampling

Numbers: this file (verification) + DATA.md §5 (deliverable). Method verdict: METHODOLOGY §Core PVT bullet.
