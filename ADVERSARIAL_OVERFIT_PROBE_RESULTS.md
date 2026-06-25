# Adversarial overfit-probe results — 8 GT DUTs vs the current modeling method

**Built + run 2026-06-25** (ultracode). Spec: `HANDOFF_ADVERSARIAL_OVERFIT_PROBE.md`.
8 fair, in-scope ground-truth DUTs (4 voltage, 4 current) engineered to drive the modeling
method's overfit locus + structural blind spots to failure, **plus 5 new held-out gates** built to
*expose* (not just suffer) the cases the existing harness is blind to. Every GT was independently
re-verified (convergent + on-target) before scoring; every "exposed" finding is a re-simulation of
the **emitted model** vs GT (the miss is attributed to the model, not a scorer artifact).

## Headline

**8/8 GT DUTs converge and are exposed by the method.** The most important result is the
**meta-finding, CONFIRMED**: **4 of the 8 fail where the existing harness is structurally BLIND** —
the in-sample composite, the scalar `crossval_isrc` PASS gate, and the small-signal held-out
crossval all say "good," and only the **new gates** catch the defect:

| probe | existing harness says | the NEW gate catches |
|---|---|---|
| **A4 classab** | composite **1.62** (< base 3.95!); LOCO/offgrid/structloco/ident all clean | `a4_verdict`: slew wrms 30%, GT asym 0.35 vs model 0.12 |
| **B2 double_cascode** | scalar PASS gate **passes** (rout 2.1%) | `y_rms` step-counter: **2** admittance zeros, 2.09 dB |
| **B3 bias_flip** | in-`vc` PSRR sign = **ok** | `psrr_offvc`: sign flips +8340→−1490 nS across compliance |
| **B4 tempload** | `ptat_err` = **0.000** (passes) | `iv_temps`: compliance knee moves **47 mV** with T |

**Bonus finding (the false-positive control surfaced a real one):** the B4 knee-shift gate flags
**`v8_wilson` — a BASELINE — at 139 mV.** The temperature×compliance gap pre-existed, undetected,
in the original 8-archetype set; the scalar PASS gate (`ptat_err`, `rout_err`) never saw it.

## Voltage path (stress `fit_model.py`) — `run_matrix` + `crossval --variant`

| DUT | composite | held-out crossval | large-signal (A4) | mechanism exposed | class |
|---|---|---|---|---|---|
| **A1 qbow** | **18.96** (pkdb 15.8 dB) | LOCO FAIL · OFFGRID FAIL · **STRUCT FLIP** (PSRR-cpx @250µ) | — | 2-branch RLC Zout **can't represent** the non-monotonic-Q bow | structural-can't-represent |
| **A2 pzmig** | 5.03 | **PSRR LOCO 0.22→4.30 dB (19.5×)** · OFFGRID FAIL | — | non-log-linear PSRR-corner migration breaks the load-interp fitter | overfit-interp (PSRR) |
| **A3 swbleed** | 5.15 | LOCO Zout 0.31→1.10 (3.5×) + PSRR 0.18→5.50 (30×) · OFFGRID FAIL · **R_pl SWITCH** (5.15k@20µ → off@121/250µ) | — | load-threshold damper → load-dependent structure | selector/param-switch |
| **A4 classab** | **1.62** | LOCO/OFFGRID/STRUCT/IDENT **all clean** | **a4_exposed=TRUE** (slew 30%, asym GT 0.35 vs MD 0.12) | class-AB swing-dependent gm; LTI premise broken | **LTI-boundary / composite-blind** |

- **classab** is the cleanest blind-spot: the symmetric linear model fits the *small-signal* better
  than base (1.62 < 3.95) and passes **every** small-signal held-out gate; only the large-signal
  step verdict catches the swing-dependent gm. `base` is the control: `a4_exposed=False` (slew 1%).
- **qbow** exposes via a channel different from the spec's prediction: the strict "Zout LOCO ≥3×"
  was **not** met (LOCO Zout 1.39→1.57) — instead the 2-branch RLC structurally cannot *represent*
  the sharp non-monotonic-Q mid-peak (in-sample pkdb 15.8 dB, composite 18.96), confirmed by offgrid
  FAIL + a PSRR-complex structloco flip. The build agent also proved a real **design tension**: the
  sharp Q that makes the bow (Q₁₂₁≈45) conflicts with the bringup Q≤30 stability gate, so a
  bringup-PASSING GT with 3× peak ratios on *both* load sides is infeasible on this topology — a
  finding about the probe-feasibility envelope, not a gate failure.

## Current path (stress `fit_isrc.py`) — `crossval_isrc` (PASS gate **stays 8/8** on baselines)

| DUT | scalar PASS | new gate | mechanism exposed | class |
|---|---|---|---|---|
| **B1 inflect_ctat_ptat** | NO (idc 16%, ptat 0.158) | **B1 = 15.0% interior miss** | U/convex Idc(T); 3-temp default misses 25/85 °C | genuine-defect + metric-gap |
| **B2 double_cascode_2zero** | **YES (clean)** | **B2 = 2 zero-steps, 2.09 dB** | two admittance zeros (1.2e5 & 1.3e7 Hz); single zero-pole can't hold both | genuine-defect / **gate-gap (clean)** |
| **B3 bias_dependent_psrr_flip** | NO (iv 96%) but **in-vc sign = ok** | **B3 = sign flip** (+8340→−1490 nS) | bias-dependent supply-coupling sign; single-`vc` gdd is self-fulfilling | self-validating-metric gap (**clean on sign**) |
| **B4 tempload_xterm** | NO (rout 93%) but **ptat_err = 0** | **B4 = 47 mV knee shift** | T×compliance cross-term; separable Idc(T)·knee(Vo) can't bend | gate-coverage gap (**clean on temp**) |

- **B2/B3/B4 are clean blind-spots** on the dimension they target: B2 fully passes the scalar gate;
  B3's in-`vc` sign check is green (it's the *off*-`vc` flip that's caught); B4's `ptat_err` is 0
  (it's the *knee movement* that's caught). B1 is caught by both the new gate and the existing one
  (its monotonic-convex Idc(T) also breaks the endpoint ratio).
- **B1 caveat:** a pure-convex Idc(T) cannot be collinear at 3 temps (the middle always sits below
  the chord → `idc_err`/`ptat_err` fire), so a *clean* blind-spot B1 (existing gate passes, B1
  fires) needs an S-shaped / off-center-U current — delicate in this strongly-CTAT-Vth PDK. The B1
  **gate** is validated independently (synthetic lock test + clean on all 8 baselines + 15% miss).

## The 5 new gates (the deliverable infrastructure)

Built alongside the GT, each locked by a synthetic unit test (`harness/test_adv_probe_gates.py`,
14 tests) that fires the metric on a known-bad input and stays quiet on a benign one. All
**observational** (per the chosen policy): they report EXPOSED and never change the existing
PASS/composite verdict, so the deliverable can demonstrate "clean score yet defect present."

| gate | file | metric | discriminator (why it doesn't false-positive) |
|---|---|---|---|
| `HELDOUT_IDC_TEMPS` (B1) | `isrc_char` + `crossval_isrc.gate_heldout_idc` | interior-temp (25/85 °C) Idc miss % | baselines ≤0.4% |
| `y_rms` two-zero (B2) | `crossval_isrc.gate_y_rms` + `_count_y_zero_steps` | band \|Y\| dB-rms gated on **≥2 separated Re(Y) rising steps** | step-count, not raw rms — v4/v5 have high rms but 1 step |
| `psrr_offvc` (B3) | `isrc_char` + `crossval_isrc.gate_psrr_offvc` | off-`vc` dIout/dVdd **sign flip** | requires GT sign to genuinely flip AND model to miss it |
| `iv_temps` (B4) | `isrc_char` + `crossval_isrc.gate_iv_temps` | GT compliance-**knee shift** [mV] + near-knee model error | knee-shift (benign mirrors 7–11 mV vs 40 mV bar), not plateau-RMS |
| `a4_verdict` (A4) | `score.a4_verdict` + `_trans_metrics` asym | big/slew wrms + droop-vs-recovery asymmetry mismatch | `base` control passes (a4_exposed=False) |

Engineering note: `gate_y_rms` uses the model's *analytic* admittance `predict_y` (the form the
Cadence VA emit realizes with a physical internal node), **not** a re-sim of the offline
`emit_isrc` ngspice twin — that twin deliberately omits the fitted pole-zero, so re-simming it would
false-positive on every baseline with even one real cascode zero (v6_ptat: predict_y 0.08 dB vs the
offline emit's 7 dB). The B2 *step-counter* (not raw rms) is what makes it specific to a 2nd zero.

## No-regression (verified)

- **`crossval_isrc` PASS gate stays 8/8** on the baselines (existing fits byte-identical — only
  additive npz fields + observational gates were added).
- **`score.py` composite is byte-neutral**: with the edit stashed, `base` re-scores to the identical
  `3.952783` (the 3.88→3.95 vs the committed baseline.json is pre-existing environment drift, not the
  edit).
- **`fit_model --selftest` PASS**; **full suite `pytest harness/` = 145 passed, 0 failed**.
- The 14 voltage + 8 current baselines are untouched (variants registered additively); the
  anti-overfit regression guard was scoped to `BASELINE_VARIANTS` (the adversarial probes are
  excluded from it by design — a poor fit on a probe IS the finding).

## Classification summary

- **LTI-boundary / composite-blind:** A4 classab (the deepest — fools the composite *and* all
  small-signal crossval).
- **Structural-can't-represent:** A1 qbow (2-branch RLC vs non-monotonic-Q bow) + the Q≤30 design tension.
- **Overfit-interp (the known load-interp locus, confirmed/extended):** A2 pzmig, A3 swbleed.
- **Gate-coverage gaps (now closed by the 5 new gates):** B2 (two zeros), B3 (off-`vc` sign),
  B4 (T-knee), B1 (interior temp); A4's large-signal verdict.
- **Genuine pre-existing gap surfaced in a baseline:** v8_wilson B4 (139 mV knee shift).

## Reproduce

```bash
python3 harness/isrc_char.py                                   # regen npz incl. held-out grids
python3 harness/crossval_isrc.py                               # PASS table + the 4 observational gates
python3 harness/run_matrix.py qbow pzmig swbleed classab       # voltage composites + A4 big/slew
for v in qbow pzmig swbleed classab; do python3 harness/crossval.py --variant $v; done
python3 -m pytest harness/test_adv_probe_gates.py -q           # 14 synthetic gate locks
```
