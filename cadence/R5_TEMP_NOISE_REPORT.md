# R5 — held-out off-nominal-temperature noise validation

**Date:** 2026-06-20 · Built from the expert-panel review (`cadence/NOISE_MODELING_REVIEW.md`, Rec 5).
Same observability pattern as R4 (held-out, both engines, never fitted, never in the composite).
**Result: the model's pure-kT temperature law is a good approximation (~0.5–1.4 dB, ±1–6% LF bias over
−40…125 °C) on 17/18 variants; the one large flag (v3_miller @125 °C) is a degenerate GT, not a model
fault — exactly the kind of anomaly the gate is meant to surface.**

---

## The gap it closes

Noise is fit at the nominal temperature (27 °C). The emitted model scales **every** noise section as
pure resistor-thermal **kT** (VA `white_noise(4kT·$temperature)`; SPICE Johnson noise of the `Rn*`
nodes) — including the Lorentzian sections that stand in for **flicker**. But BSIM3 GT flicker is
~T-independent, and the GT's operating point shifts with temperature (gm drops at hot), so the model's
T-law was unvalidated. **R5** collects GT noise at ±extreme corners (`−40`, `125 °C`) at the nominal
load into the reference (both engines) and `score._temp_noise_metrics` grades the model's noise there
vs GT — `psd_rms` + a signed LF bias (`ir_lf`, +% = model over-predicts the flicker band). The emit
T-law is **not** touched.

## Observation (18 variants, ngspice GT vs model; v4_ffpsrr omitted — pre-existing sim timeout)

`psd` = held-out T-corner noise log-RMS (dB) vs GT; `LFbias` = signed integrated-RMS error over
10 Hz–100 kHz (+ = model over-predicts). `in-corner` = nominal-temp fit (reference point).

| variant | in-corner | −40 °C psd / LFbias | +125 °C psd / LFbias | note |
|---|---|---|---|---|
| base | 0.33 | 0.86 / +2% | 0.75 / -4% |  |
| base_ghz | 0.33 | 0.86 / +2% | 0.75 / -4% |  |
| cg_hi | 0.28 | 1.39 / +3% | 0.83 / -2% | kT-law residual |
| cout10n | 0.32 | 0.48 / +3% | 0.71 / -3% |  |
| cout4n7 | 0.33 | 0.51 / +3% | 0.69 / -3% |  |
| esr_hi | 0.33 | 0.73 / +2% | 0.74 / -4% |  |
| iq_hi | 0.27 | 0.73 / +1% | 0.55 / -2% |  |
| iq_lo | 0.35 | 0.90 / +5% | 0.89 / -5% |  |
| wp_big | 0.41 | 1.02 / +3% | 0.82 / -3% | kT-law residual |
| v1_nmos | 0.36 | 0.84 / -6% | 0.42 / +0% |  |
| v2_capless | 0.23 | 0.23 / +2% | 0.29 / +2% |  |
| v3_miller | 0.43 | 0.82 / +10% | **24.80 / +2565%** | **GT degenerate at 125 °C — see below** |
| v5_spur | 0.33 | 0.86 / +2% | 0.75 / -4% |  |
| v6_spur2 | 0.33 | 0.86 / +2% | 0.75 / -4% |  |
| v7_esl | 0.33 | 0.86 / +2% | 0.75 / -4% |  |
| v8_dlc | 0.32 | 0.50 / +3% | 0.71 / -3% |  |
| v9_vldo | 0.35 | 0.46 / -1% | 0.36 / +3% |  |
| v10_3lc | 0.28 | 0.47 / +4% | 0.68 / -1% |  |

## Verdict

- **The kT-law is a good approximation (17/18 variants).** Held-out psd is 0.46–1.4 dB and LF bias
  ±1–6% across −40…125 °C — the model carries the dominant thermal T-dependence. The systematic
  residual (model slightly **over**-predicts cold, **under**-predicts hot) is the second-order effect
  the pure-kT law omits: at hot the GT's gm drops, loop suppression weakens, and the GT output noise
  rises **more** than kT — so the model (which scales everything by exactly √(T/T₀)) reads a few %
  low. This is a known, bounded limitation, not a bug; the panel's "Lorentzian flicker over-scaling"
  prediction is present but small (the OP-shift term partly cancels it).

- **v3_miller @125 °C = 24.8 dB is a DEGENERATE GT, not a model fault.** Root-caused directly: the
  synthetic v3_miller transistor netlist loses regulation at 125 °C (Vout drifts to **1028 mV**, +110 mV
  past its 918 mV nominal) and its `.noise` output **collapses ~600×** (104 → **0.18 nV/√Hz** at 10 MHz
  — below any physical thermal floor; a 0.002 Ω-equivalent). The behavioral model scales sensibly
  (×1.15, kT). So the model is the *trustworthy* side and the GT reference is invalid at that corner —
  the synthetic Miller GT was simply not designed for 125 °C. **This is the gate working as intended:**
  a large held-out value flags "model and GT disagree here," and investigation assigns the blame (here,
  to the reference). It is a GT-coverage finding, not a modeling defect.

## What shipped

- `bench.HELDOUT_NOISE_TEMPS = [-40, 125]` + `temp` knob on `bench.measure_noise` /
  `spectre_bench.measure_noise` (`.options temp=` / `options temp=`).
- `gen_reference.py` / `extract_ref.py` collect `noise_temp_<Tlabel>_<il>` at the nominal load (GT,
  both engines).
- `score._temp_noise_metrics` + scorecard line + summary keys; **not** in the composite.
- Tests: `harness/test_temp_noise_metric.py` (7 pure-logic: label round-trip, parse/thread, worst_T,
  signed LF bias, not-in-composite).

Refs are **not** committed regenerated (supply-spur / R4 precedent — mid-stream regen perturbs the
blessed fits); the arrays activate on the next `gen_reference`/`extract_ref` run.

## Takeaways / follow-ups

- The model's T-law is adequate for production over −40…125 °C (~1 dB) **without** any emit change —
  the panel's "do not touch the emit T-law" guidance holds; closing the last few % would require a
  flicker-specific (T-independent) noise term, which would forfeit the 9 e-6 cross-engine lock and
  isn't worth it.
- **v3_miller's GT needs a hot-corner fix** (or an explicit "valid 0–85 °C only" note) before its
  125 °C noise reference is usable — tracked as a GT-coverage item, independent of the behavioral model.
- A future promotion to a *gate* (only if ever needed) should be RELATIVE (`held ≤ k× in-corner`) and
  guard against the degenerate-GT case (a sub-physical GT noise floor should be rejected, not scored).
