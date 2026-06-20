# Noise-Modeling Optimization Review — expert panel

**Date:** 2026-06-20 · **Method:** 5-lens expert panel (noise-physics / numerics / metrics /
Verilog-A-structure / coverage) → per-finding adversarial verification → lead-architect synthesis →
completeness critic. 29 findings raised, **19 survived verification, 10 dropped** (verifiers caught
them as misreads of the source or net-negative). Review only — **no code was changed.**

---

## Headline

**The noise *model* is already in good shape — the highest-value improvements are in the *grading*,
not the emit.** All 14 graded synthetic refs fit **0.3–1.5 dB in-band**. The Norton white+Lorentzian
core, the gated hybrid series-voltage bank, the all-passive+VCCS+`white_noise` emit (which is exactly
what buys the 9e-6 cross-engine lock), and the v10_3lc order-limit are all **correct — keep them.**

The single most consequential finding: the headline noise metric (`psd_rms`, unweighted log-RMS over
10 Hz–100 MHz) **over-penalizes a fine model ~6×**, because a deep-floor post-rolloff tail carrying
only ~0.3 % of the integrated noise energy dominates a 7-decade flat average.

---

## Recommendations (ranked, for a follow-up build round)

### Rec 1 — Sg-energy-weight `psd_rms` [effort S, risk LOW] ★ top pick
- **What:** in `score._noise_metrics`, replace the flat unweighted log-RMS with a GT-PSD-energy-weighted
  dB residual: `w = Sg² / Σ Sg²` over the 10 Hz–100 MHz band, `psd_rms = sqrt(Σ w·db²)`. **Keep the
  full band** — the weighting (not a hard 1 MHz cap) is what suppresses the deep-floor tail, and a cap
  would blind the energy-bearing resonance/spur band (v5/v6/v8).
- **Measured gain:** base @20u **5.91 → 0.41 dB**; @121u 0.95 → 0.33; @250u 1.46 → 0.48. The composite
  noise term (W=0.5) starts tracking delivered µVrms instead of a 0.3 %-energy floor.
- **Risk:** `score.py` only — cannot touch the cross-engine 9e-6 lock (separate file, never imports
  score), the fitter (decoupled), emit, or HB/PSS/pnoise. Sole cost: one-time scorecard re-baseline.
- **Where:** `harness/score.py:104-109`.

### Rec 2 — Make the noise resonance metric frequency-aware [effort S, risk ~0]
- **What:** the resonance window is hardcoded `res=(0.5e6,3e6)` and takes `model.max()` and `GT.max()`
  *independently* — so a resonance of correct height at the **wrong frequency** scores ~0 dB. Anchor the
  window to the **GT noise-Sv peak** (`argmax(Sg)` ± 1 decade, clamped to grid) and add a peak-frequency
  ratio alongside `pkdb`, mirroring the Zout `pkfratio`. Diagnostic-only this round.
  - **Critic correction:** anchor to the GT **Sv** peak (`argmax(Sg)`, already in scope as `gn[:,1]`),
    **not** the GT Zout peak. The Sv peak = In·|Zout|; it coincides with the Zout peak only where In is
    locally flat — **false on v8_dlc**, whose interior In-notch sits exactly at the Zout resonance. The
    Sv anchor is free (no extra plumbing) and correct on the notch variants too.
- **Why it matters:** v2_capless/cout10n/esr_hi/v5/v7 deliberately move the noise resonance out of
  0.5–3 MHz; today the metric measures off-peak ripple on them. Same failure mode as last session's
  PSRR_BAND hardcoded-band bug.
- **Where:** `harness/score.py:106,110` (window + independent `.max`); `132,144,163` (Sv/peak scope).

### Rec 3 — Fold the *right* noise sub-metrics into the composite [effort S, risk LOW; do after Rec 2]
- **What:** add a small `W['npk']·mean(|matched-freq noise pkdb|)` term **and** a new **LF-windowed
  integrated-RMS** (`ir_lf`, e.g. 10 Hz–100 kHz — the datasheet µVrms window) at low weight. Mirror the
  Z/PSRR `zband`/`pband` pattern. **Do NOT** fold in the as-written full-band `ir_pct` (it's blind to
  localized errors and redundant with `psd_rms`).
- **Why:** `pkdb`/`ir_pct` are computed and printed (`npk`/`nir`) but never graded, so a narrow
  resonance/notch miss is diluted in the 6-decade RMS. The value is the resonance term + the flicker-
  window µVrms the customer signs off on — neither exists in the composite yet.
- **Risk:** LOW for the locks; MEDIUM admin — re-baseline + over-weighting (ir_pct hits 100–300 % on
  v8/v10; bound + gate it via the explicit-collection pattern like `sspur`, ~0.02 weight with saturation).
- **Where:** `harness/score.py:203` (composite), `109-111` (sub-metrics), `38-40` (W).

### Rec 4 — Held-out off-corner **LOAD** validation [effort M, risk ~0] ⬆ promoted by the critic above temperature
- **What:** add ONE noise PSD at a current strictly **between** the fit corners (e.g. 60 µA, between
  20 u and 121 u) used for **validation only, not fitting**, with a non-composite held-out delta gate.
- **Why (critic, HIGH):** the noise model is fit AND graded at only the 3 fitted corners
  (`gen_reference.py:61` / `score.py:117-119` touch exactly `bench.LOADS`), yet the emit **interpolates
  every section amplitude quadratic-in-ln(iload) with the corner freqs frozen** (`_pexpr`/`_poly`
  `fit_model.py:1103-1147`). So the entire off-corner *spectral shape* is a 3-point quadratic extrapolant
  of per-section gains over fixed poles — the exact overshoot pattern the `CLAMP_*` machinery was added
  to fence (it bounds gains to the corner envelope but does **not** guarantee the off-corner Sv is near
  GT). This is the **most-exercised axis** (every PMU load line sweeps current continuously), so a 1–3 dB
  interpolation error here is more likely and more consequential than the 125 °C case — and currently
  100 % unobserved.
- **Where:** `harness/gen_reference.py:61`, `cadence/extract_ref.py`, `harness/score.py` (held-out gate).

### Rec 5 — Held-out off-nominal **temperature** noise corner [effort M, risk ~0]
- **What:** add a 125 °C (optionally −40 °C) noise reference with matched `temp=` on both engines + a
  non-composite held-out delta gate. **Do NOT modify the emit temperature law.**
- **Panel self-correction:** the model is **not** frozen at 300 K — both emitters already carry a
  symmetric first-order linear-in-T law (VA `white_noise(4·P_K·$temperature)`, SPICE real resistors).
  The originally-proposed √T gain scale is **wrong** (double-counts to T²). The genuine unmeasured gap:
  the Lorentzian sections standing in for flicker **over-apply** the thermal T-law while the BSIM3 GT
  flicker is ~T-independent — only a held-out hot corner can bound that (~1–2 dB at 125 °C expected).
- **Caveat:** if ever "fixed" by making the emit T-law flicker-aware, the 9e-6 cross-engine lock must be
  re-blessed with a non-300 K case (SPICE resistors and VA `white_noise` must move together).
- **Where:** `gen_reference.py:64`, `extract_ref.py`; **do NOT touch** `fit_model.py:357` (KT4) this round.

---

## Keep as-is (panel verified these are already the right call)

- **Basis primitive** (white + Foster-Lorentzian, passive R/C + `white_noise` + VCCS): correct realizable
  structure, and exactly what buys the 9e-6 cross-engine lock. Explicit stochastic RTN would be non-LTI,
  invisible to `.noise`, and break PSS/HB; a stationary RTN-trap PSD is already a Lorentzian the bank spans.
- **`flicker_noise()` / `noise_table()` for the 1/f tail — stays rejected.** Spectre-only, no byte-matching
  ngspice `.noise` analog → would forfeit the cross-engine lock. The RC-thermal staircase is the only
  ngspice mechanism for a 1/f-like slope (emitted `.lib` carries no `.model` device). The scratch probe
  also measured analytic-1/f as **equal-or-worse** than the free Lorentzian bank on every variant
  (v3_miller ~3 dB worse) — "prototyped but not adopted" = measured and rejected.
- **Hybrid series-voltage bank `T = Zout/ZA`:** algebraically exact and topology-general; fires at
  0.25–0.37 dB on the loop-shaped synthetic. Keep emit unchanged.
- **Intrinsic-noise validation topology:** genuinely GT-independent (two separate `.noise` passes; target
  built from GT Sv, not the model). No self-consistency circularity. (The xengine test covers engine
  consistency separately — "independent-GT, same-engine".)
- **Grid equalization** (2.5× trigger / 24-pts-per-dec resample): correctly no-ops on all 14 log-uniform
  in-house refs; only re-weights non-log-uniform pnoise exports.
- **v10_3lc noise order-limit:** DESIGNED bound (interior In-notch + rising region a monotone Lorentzian
  bank cannot make; hybrid attempted at 4.03 > 4.0 and correctly fails). **Do NOT raise NOISE_M_MAX** —
  verified inert (M_MAX=20 still stops at M=7 / 4.03 dB). Document at the corrected **~4.0 dB** (not 5.8);
  v8_dlc (1.02 dB) and cout10n (0.39 dB) are NOT noise-structure-limited — drop them from that grouping.
- **Greedy adaptive-M (trigger 4.0 dB, hybrid-switch margin 0.5 dB):** well-reasoned; the greedy loop
  never fires on any graded ref (all 14 under trigger), so BIC-style gates / M-MAX tuning buy nothing.
- **Cold-logspace init / soft separation penalty / default `x_scale`:** correct-in-principle robustness
  items with ~0 measurable gain on the graded set and real byte-stable-emit / lock-flap risk — **defer**
  until a real unseen part exposes a need.

---

## Dropped by the verifiers (don't re-propose without new evidence)

These 10 were raised but killed under adversarial scrutiny — recorded so the next round doesn't repeat them:

1. **Analytic 1/f primitive instead of the Lorentzian staircase** — probe already measured it equal-or-
   worse; slope points the wrong way for the steep-LF stall (gives −10 dB/dec PSD, not the needed steeper).
2. **"Scored noise still carries full Zout/zrms error"** — wrong: `|zmodel|` cancels exactly in the score;
   the proposed `Sv/|Z_GT|` target would *inject* zrms into noise and double-count Zout.
3. **"Load scaling has no ∝Id/∝gm physics, off-corner saturates"** — the closed-loop output Sv exponent is
   genuinely variant-dependent (Id^+0.05 … Id^+1.01 measured); a fixed √/linear law is the *wrong* form,
   the free quadratic is better. (The real residual — off-corner *validation* — is captured as Rec 4.)
4. **Fixed-bank NNLS + vector-fitting pole refinement** — net-neutral on the graded set; byte-stable-emit
   and lock-flap risk; deferred (see "keep as-is").
5. **Importance-weighting the fit residual** — the *fit* is decoupled from the composite; weighting belongs
   in the *metric* (that's Rec 1), not the fitter.
6. **Complex-pole noise-shaping section for the v8_dlc In-notch** — the notch is a DESIGNED order-limit;
   "fixing" it distorts the honest "model order exceeded" signal.
7. **"pkdb/ir_pct computed but never graded"** — real observation, but folded correctly as Rec 3 (the
   as-written full-band ir_pct must NOT be the term used).
8. **"No 1/f flicker basis term, staircase starves the resonance fit"** — duplicate of #1; the staircase
   wins on measured residual.
9. **"cout10n sits just below the 4 dB trigger and never tries to improve"** — by design; it already fits
   0.39 dB.
10. **"Intrinsic vs supply-induced noise are disconnected"** — partially justified (they should agree at
    band overlap) but not an emit/fit optimization; revisit only if a shared metric is wanted.

---

## Suggested sequencing for the build round

`Rec 1` (measured 6× metric-honesty win, S/LOW) → `Rec 2` (frequency-aware resonance, prerequisite for 3)
→ `Rec 3` (fold sub-metrics, gated/low-weight) → `Rec 4`/`Rec 5` (held-out load/temperature validation —
pure observability, zero emit risk). Items 1–3 are all `score.py`-only and cannot touch the cross-engine
lock; 4–5 add references + a held-out gate and touch neither fit nor emit. **No recommendation requires
changing the noise emit or the fitter** this round.
