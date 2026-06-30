# Adversarial overfit-probe set — 8 fair LDO/current-ref GT DUTs (build-ready spec)

**Session role:** designed + finalized in a normal session (this doc). **Build it in a fresh ultracode
session** (build netlists → simulate → fit → crossval ×8, plus the new gates flagged below).

**Goal (from the user):** find expert-designed SPICE LDOs that FAIRLY expose problems in the current
modeling method — overfitting first, structural blind spots second. Scope decisions locked with the user:
- **Single-output**, but the 8 must stress **BOTH** the voltage fitter (`fit_model.py`) **and** the
  current fitter (`fit_isrc.py`).
- **In-scope / fair**: every DUT is something the method *claims* to handle (linear single-loop LDO, or a
  DC current reference/sink). A fair failure = a real defect; out-of-scope boundary mapping was excluded.
- All buildable on the `nlv`/`plv` BSIM3 cards (`models/nmos_lv.mod`, `models/pmos_lv.mod`), DC-stable
  (`bringup.py`: converges, ring-decay < 1, sane Vout), added **purely additively** to
  `harness/variants.py` / `harness/isrc_variants.py` (the existing 14 + 8 baselines stay byte-identical).

**Why this set (back-test context):** a held-out re-run of the current fitter (2026-06-25) showed the
modeling tool is NOT broadly overfitting — current template 8/8, noise jointly-fit (no overfit), every
recent DOF gated by keep-best — EXCEPT one proven locus: the **per-parameter quadratic-in-ln(iload)
load interpolation forced through exactly 3 corners (0 residual DOF)**, where held-out PSRR is ~20–30×
in-sample (~5 dB). These 8 are engineered to drive that locus (and the analogous current-path gates) to
failure, **and** — critically — to surface cases the in-sample composite / current crossval are
*structurally blind to* (the dangerous deployment case).

Corners (verify in `bench.py`): loads **20µ / 121µ / 250µ**, nominal 121µ; **asymmetric in ln**
(gaps 1.80 vs 0.73) — the lever a 3-point quadratic overshoots. Current chars at a single compliance `vc`
over `TEMPS=(-40,55,125)`.

---

## A. VOLTAGE-PATH DUTs (stress `fit_model.py`)

### A1. `ldo_qbow` — non-monotonic Zout resonance-Q vs load  *(pure load-interp overfit, Zout)*
- **Attacks:** `fit_zout` per-corner `R_pl`/`L_a`, then the `_pexpr`/`_poly` quadratic-in-ln(iload) interp.
- **GT:** base `ldo_gt` 5T-OTA/PMOS-pass core; engineer loop phase-margin so the Zout LC peak **Q is
  non-monotonic in load** — low@20µ (overdamped), **sharp peak Q≈8–12 @121µ**, low@250µ (ESR-zero damps).
  Mechanism: in-band `Rd–Cd` snubber (`Cd≈30p, Rd≈2k`) on vout + OTA tail bias so gate-pole×pass-gm crosses
  the snubber-undamped window only near 121µ. Target |Z|peak ≈ 1.2/9/1.5 Ω at ~3–6 MHz across corners.
- **Breaks it:** fitted `R_pl` traces a downward parabola in ln(iload); LOCO drops 121µ → 2-point *linear*
  interp predicts a small `R_pl` → nearly-flat Zout where GT has a 9 Ω peak. Off-grid quadratic overshoots
  upward in the interior.
- **Predicted:** LOCO Zout held-out@121µ ≈ **10–18 dB** (in-sample ≈0.3) → 30–60×; offgrid Zout ≈ 6–10 dB;
  in-sample composite stays good (~5).
- **Catches it:** `crossval.loco` `zout.ok` FALSE @121µ (`Z_interp` col) + `crossval.offgrid` `zout.ok` FALSE. *(existing gates)*
- **Accept (problem exposed):** LOCO Zout@121µ ≥ 3× in-sample AND ≥ 4 dB AND offgrid Zout ≥ 4 dB. *Falsifier:*
  if the 3-corner peak heights turn out monotonic, it does NOT expose — verify the bow before accepting.
- **In-scope:** load-varying single-loop RLC Zout resonance is `zmodel`'s home turf; only the non-monotonic
  load trajectory is new (cg_hi/v2 are monotonic).

### A2. `ldo_pzmig` — PSRR corner migrates non-log-linearly with load  *(pure load-interp overfit, the PROVEN weak axis)*
- **Attacks:** `fit_psrr` real-bank corner `w1` (`logspace=True` quad interp).
- **GT:** base core, **min-phase** PSRR (stays on the shelf branch — NOT a v4 notch). Make the PSRR roll-off
  corner = loop UGB, and make UGB(iload) **saturate**: pass-gm∝√iload until a fixed gate-node pole
  (`cg≈6p` + `Rg≈80k, Cg2≈1p` on the OTA output) clamps UGB above ~121µ. Target corner ≈ 0.4/3.0/3.3 MHz
  across 20/121/250µ (steep rise then plateau) — `ln(w1)` vs ln(iload) strongly convex.
- **Breaks it:** quadratic through a knee overshoots the interior; LOCO drops the knee-vertex 121µ →
  straight-line in ln(w1) places the corner at √(0.4·3.3)≈1.15 MHz vs true ~3 MHz → PSRR roll-off lands a
  decade off across the 8/16/24 MHz score points.
- **Predicted:** LOCO PSRR held-out@121µ ≈ **8–14 dB** (in-sample ≈0.2) → 40–70×; offgrid PSRR ≈ 5–9 dB.
- **Catches it:** `crossval.loco` `psrr.ok` FALSE (`P_interp`) + `crossval.offgrid` `psrr.ok` FALSE. *(existing)*
- **Accept:** LOCO PSRR@121µ ≥ 3× in-sample AND ≥ 4 dB AND offgrid PSRR ≥ 4 dB. *Falsifier:* confirm
  `ln(w1)` is genuinely convex across the 3 corners (read the fit print) — a log-linear migration won't break it.
- **In-scope:** min-phase PSRR shelf with a load-moving corner is the path base/A-layer/v1/v2 all use; only
  the non-log-linear (knee) migration is untested (v3's migration is in Zout, not the PSRR corner).

### A3. `ldo_swbleed` — load-threshold MODE SWITCH (a damping branch turns on past a current)  *(selector overfit, INVISIBLE in-sample)*
- **Attacks:** (a) `fit_zout` branch-B selector (`e2 < 0.6·e1`) + (b) continuous `_pexpr` interp of a
  *discontinuous* truth.
- **GT:** base core + a **current-threshold-activated shunt damper**: NMOS `mbleed` (W≈40µ) held
  sub-threshold for iload ≤ ~150µ and conducting above it (gate driven by a replica of the load current via
  a `Vgs`-offset diode `mdet` sized to cross threshold near 180µ), adding a real parallel R‖L at high load
  only. Linear at each fixed OP (small-signal R‖L whose *value jumps* between the 121µ and 250µ OPs);
  DC-negligible by replica cancellation so Vout regulation is unaffected; ring-decay stays < 1.
- **Breaks it:** full fit branchB = {20µ:off, 121µ:off, 250µ:**on**}. structloco dropping 250µ → branch-B
  off everywhere → held-out 250µ misses the high-load second feature; interp of `R_b`/`L_b` (1e9=off at two
  corners, finite at one) across the discontinuity clamps to nonsense in the interior.
- **Predicted:** **structloco FLIP** `branch-B@250µ True→False`; identifiability marks `R_b`/`L_b` **SWITCH**
  (ratio>1e3); offgrid Zout@174µ ≈ 4–8 dB. **In-sample composite excellent** (each corner fits its own structure).
- **Catches it:** `crossval.structloco` flips → `pass_=False` (`struct` col) + `identifiability` SWITCH +
  offgrid `zout.ok` FALSE. *(existing)*
- **Accept:** structloco branch-B flip on ≥1 fold AND (identifiability R_b/L_b SWITCH OR offgrid Zout ≥ 4 dB).
  Headline falsifier: **composite ≤ 6 yet structloco FAIL** (the danger: clean score, unstable structure).
- **In-scope:** a load-dependent damping/aux branch (high-load stability booster) is standard, single-loop,
  linear at each OP; the model literally advertises per-load branch-B for v3 multi-pole Zout. The fair stress
  is that the *structure it selects* is itself load-dependent.

### A4. `ldo_classab` — class-AB output, swing-dependent gm with NO dropout  *(LTI-foundation breaker — the deepest attack)*
- **Attacks:** the LTI premise — only nonlinear DOF is `build_pwl_arrays` (a Vdrop→I dropout clamp). No DOF
  for a signal-amplitude-dependent transconductance away from dropout. Foundational "spur band is linear"
  finding, never stress-tested on the *amplitude* axis away from dropout.
- **GT:** base 5T-OTA core, replace the single PMOS pass with a **class-AB push-pull**: PMOS source
  (`W≈30µ L=0.3µ`) + NMOS sink (`W≈12µ L=0.3µ`), split drive (OTA out `ng` → PMOS gate; level-shifted copy →
  NMOS gate). Small signals: both conduct, well-defined gm, linear regulation. Large load step: NMOS sink
  cuts off, PMOS slams into high-gm sourcing — **effective output gm varies strongly over the swing, 50–100 mV
  headroom retained at all times (NO dropout)**. Vout ≈ 0.9 V.
- **Breaks it:** small-signal Zout/PSRR/noise per corner are perfectly LTI → **in-sample composite fooled**
  (fits like base). PWL dropout clamp never engages → big/slew load step modeled as pure linear `R_a`,
  over/under-shooting by a swing-dependent factor with an asymmetric droop-vs-recovery the symmetric linear
  model cannot produce. systest is *also* largely blind (its `vrip` auto-shrinks to the linear regime).
- **Predicted:** `big_wrms`/`slew_wrms` (1 mA / 5 mA steps) **30–80%**, asymmetric; small-signal composite ~base (3–5).
- **Catches it:** large-signal transient `big_wrms`/`slew_wrms` — **NEW GATE NEEDED** (currently diagnostic-only,
  NOT folded into the composite; see §C). Also needs a systest large-*load-step* mode (not just small supply ripple).
- **Accept:** model reproduces +1mA AND +5mA waveforms to wrms ≤ 15% AND droop asymmetry ≤ 20% → PASS (no problem);
  else FAIL (problem exposed). Expectation: FAIL.
- **In-scope:** class-AB / push-pull output stages are a standard LDO technique; fully in the linear-LDO class
  the tool advertises. **Classification: FUNDAMENTAL boundary for the LTI core + composite gate-coverage gap.**

---

## B. CURRENT-PATH DUTs (stress `fit_isrc.py`)

### B1. `isrc_inflect_ctat_ptat` — U-shaped / inflected Idc(T)  *(temp-curvature: the d2 gate never even fires)*
- **Attacks:** `_fit_temp` — `TEMP_QUAD_MIN_PTS=5` (default 3 temps < 5 → quadratic never attempted) AND the
  parabola form itself (a flat-bottom-with-steep-wings U is not one parabola).
- **GT:** NMOS sink = **CTAT branch ⊕ PTAT branch with a current null between**: resistor-bias CTAT device
  (`Rref≈520k`, net falls with T) summed with a constant-gm PTAT core (source-degenerated 8× mirror), sized
  so I_ctat(−40C) ≈ I_ptat(125C) and they cross ~55C → I(T) has a flat **minimum/inflection at ~40–60C**,
  wings ~10–20% higher.
- **Breaks it:** with `TEMPS=(-40,55,125)`, `_fit_temp` fits a *line* through three points sampling a U →
  endpoints both high, middle low → nearly flat (didt≈0), misses the 55C dip and both wings. Even 5+ temps:
  a symmetric parabola can't track flat-bottom + steep wings; the `TEMP_QUAD_MIN_GAIN=0.10` SSE test can sit
  just under threshold.
- **Predicted:** ~8–15% Idc error at the 55C midpoint. **Invisible to the current PTAT metric** (`ptat_err`
  uses only endpoints `idcT[-1]/idcT[0]` ≈ 1.0 → PASSES).
- **Catches it:** **NEW GATE NEEDED** — add `HELDOUT_IDC_TEMPS` (interior temps e.g. 25C/85C): fit on the 3
  defaults, `predict_idcT` at a held-out interior temp, compare to GT re-sim. *(mirror LDO `HELDOUT_NOISE_TEMPS`.)*
- **Accept:** `|predict_idcT(25C or 85C) − GT|/GT > 5%` while `ptat_err < 0.03` still green → exposed.
- **In-scope:** a summed CTAT+PTAT curvature-corrected "constant-over-T" reference is textbook bandgap-style.
  **Classification: GENUINE DEFECT + metric-coverage gap** (3-temp default + endpoint-only PTAT are blind to interior curvature).

### B2. `isrc_double_cascode_2zero` — two separated output-admittance zeros  *(stresses the NEW |Y| p-z guard from `faa88c6`)*
- **Attacks:** `_fit_admittance` single zero-pole form `g0·(1+s/wz)/(1+s/wp)+sCp`. The keep-best gate
  (`Y_PZ_KEEP_DB=0.5`, `Y_PZ_MIN_SEP=1.05`) *fires* but locks a wrong averaged corner — the failure-of-firing case.
- **GT:** **triple-stacked (double-cascode) NMOS sink** so two internal high-Z nodes each give a zero. Bias
  stack of 3 diode-connected `nlv` (set 3 gate rails) + output stack `mout/mcas1/mcas2`; add small node caps
  (`Cm≈30f`, `Co≈8f`) to separate the two zero corners by ~1.5 decades (~1e5 and ~3e6 Hz) → Re(Y) rises in **two steps**.
- **Breaks it:** the single zero parks *between* the two real zeros, beats `g0+sCp` by >0.5 dB (so it's
  adopted, wp/wz>1.05), but leaves residual at *both* corners and commits emit to a passive RC tuned to the
  averaged (wrong) corner.
- **Predicted:** `rout_err` (DC) stays small; **|Y| dB-rms ~2–5 dB at the two zero corners**, which the scalar
  `rout_err` cannot see.
- **Catches it:** **NEW FIELD NEEDED** in `crossval_isrc` — `y_rms_db`: re-sim model AC `i(vout)` over the
  `ac dec 20 10 500meg` grid, compute `_y_rms_db(model_Y, gt_Y)` (digest already carries `@y`); surface it in the PASS gate.
- **Accept:** `_y_rms_db > 1.0 dB` with residual concentrated at *two* distinct frequencies while `rout_err < 0.20` green → exposed.
- **In-scope:** double-cascode (gm·ro³) is the standard way past a single cascode; its two-zero admittance is
  one order above the cascode/Wilson zero the guard was *built* for. **Classification: GENUINE DEFECT** (single zero-pole structurally cannot hold two zeros).

### B3. `isrc_bias_dependent_psrr_flip` — PSRR real-part SIGN flips across compliance  *(self-fulfilling single-vc metric)*
- **Attacks:** `_fit_psrr` reads one signed complex `gdd = g[0].real` at a single `vc`; no Vo dependence.
- **GT:** NMOS sink whose dominant supply-coupling path *changes sign with Vo*: resistor-to-vdd bias path
  (`Rref≈520k`, +dIout/dVdd, dominant at low Vo) competing with a PMOS cascode (`mp ... plv W≈6µ`) whose gate
  ties to a divider from `out` so as Vo rises the PMOS coupling overtakes and **inverts** the net sign. Size so
  net dIout/dVdd is **+ at vc=0.3, − at vc=0.7**, crossing ~0 near nominal vc=0.5.
- **Breaks it:** if `vc` sits near the crossover, `gdd≈0` fit → model predicts ~zero coupling everywhere; if
  `vc` is on one side, model is sign-flipped on the other. Constant `gdd` has no form for a bias-dependent sign.
- **Predicted:** `sign_ok` at the *fit* `vc` PASSES by construction; at `vc±0.2` the model gets the sign wrong.
- **Catches it:** **NEW GATE NEEDED** — `psrr_offvc`: re-sim GT and model dIout/dVdd at `vc±0.2` (held out) and
  compare sign+magnitude. *(ties to memory: "validate vs independent GT, not self".)*
- **Accept:** GT sign differs at vc−0.2 vs vc+0.2 (real flip exists) AND single-`gdd` model gets ≥1 off-point
  sign wrong while in-`vc` `sign_ok` green → exposed.
- **In-scope:** bias-dependent supply-coupling sign is real in references with competing supply paths; the
  method must model or flag it, not silently fit one point. **Classification: GENUINE DEFECT + self-validating-metric gap.**

### B4. `isrc_tempload_xterm` — temperature × compliance cross-term (broken separability)  *(provably-fooled PASS gate)*
- **Attacks:** `_fit_temp` separability — Idc(T) fit at a single `vc`, no Vo argument; no `Vknee(T)`/`g0(T)` DOF.
- **GT:** reference whose **compliance knee moves strongly with T** while mid-range Idc tempco is benign. PTAT
  beta-multiplier (or Wilson sink) + **PTAT source degeneration** (`Rs` carrying the PTAT current) so the
  output device `Vds,sat` — hence knee `Vk` and near-knee `g0` — shifts with T: cold saturates at low Vo (wide
  compliance), hot the knee climbs (compliance narrows). Tune Idc *at vc* nearly flat so the PTAT-ratio gate is happy.
- **Breaks it:** `tempco` samples Idc(T) at exactly `vc` → model reproduces `ptat_ratio` perfectly and
  `crossval_isrc` (`ptat_err<0.03 @vc`) PASSES; but at any other Vo (the real operating point on a load line)
  `Idc(Vo,T)` is wrong — temp-independent `Vknee`/`g0` cannot bend.
- **Predicted:** at −40/125C near the knee, modeled `I(Vo,T)` off **10–40%** (knee moved past Vo at hot →
  model predicts full current where the device collapsed); `ptat_err@vc` stays < 0.03 (headline clean).
- **Catches it:** **NEW GATE NEEDED** — extend `crossval_isrc` `model_idcT` from a `vc`-only point to the full
  IV grid × TEMPS; compare `model_iv(T)` to GT at hot/cold across the compliance plateau.
- **Accept:** modeled `I(Vo,T)` > 5% RMS over the plateau at any of the 3 temps while `ptat_err@vc < 0.03` → exposed.
- **In-scope:** temp-dependent compliance is real in degenerated references. **Classification: GATE-COVERAGE
  GAP + characterization gap (isrc_char never sweeps Vo×T) + clean model-extension once data exists.**

---

## C. New gates/metrics this exercise requires (a finding in itself)

Half of the 8 fail in places **our own harness currently cannot see** — that is the most important meta-finding:
the in-sample composite and `crossval_isrc`'s scalar PASS gate are blind to several real overfits. To *expose*
(not just suffer) these, the ultracode build must add, alongside the GT netlists:

1. **`HELDOUT_IDC_TEMPS`** (interior-temperature Idc residual) — catches B1; mirrors LDO `HELDOUT_NOISE_TEMPS`.
2. **`y_rms_db` field in `crossval_isrc`** (band |Y| RMS, not just DC `rout_err`) — catches B2; digest already has `@y`.
3. **`psrr_offvc`** (off-compliance PSRR sign+mag) — catches B3; honors "validate vs independent GT".
4. **`model_iv × TEMPS` in `crossval_isrc`** (IV at hot/cold, not just Idc@vc) — catches B4.
5. **Large-signal load-step verdict folded into the composite** + a systest large-load-step mode — catches A4.

(A1/A2/A3 are already caught by the existing `crossval.py` LOCO/offgrid/structloco — no new gate needed.)

---

## D. Build & validation plan (for the ultracode session)

1. Write `ground_truth/ldo_{qbow,pzmig,swbleed,classab}.lib` + `ground_truth/isrc_{inflect_ctat_ptat,
   double_cascode_2zero,bias_dependent_psrr_flip,tempload_xterm}.lib` on `nlv`/`plv`; vet each with
   `bringup.py` (converge, ring-decay<1, sane Vout/Iout) BEFORE fitting — a non-converging GT is a build bug, not a finding.
2. Register additively in `harness/variants.py` / `harness/isrc_variants.py`. **No-regression gate:** the 14
   voltage + 8 current baselines re-fit byte-identical (`matrix_baseline_r1.json`, `crossval_isrc` 8/8) and
   `fit_model --selftest` PASS — a poor fit on the NEW DUTs IS the finding; a changed fit on an OLD one is a bug.
3. Add the 5 new gates (§C). Lock each with a synthetic unit test (the metric fires on a known-bad input).
4. Run `run_matrix.py` + `crossval.py --all` + `crossval_isrc.py` (+ new gates) → collect per-DUT scorecards.
5. For each DUT, evaluate its **falsifiable acceptance** (above). Report: exposed / not-exposed, the magnitude,
   and the classification (overfit-interp / selector-flip / structural-can't-represent / gate-coverage-gap /
   LTI-boundary). Adversarially verify each "exposed" finding by re-simulating the EMITTED model (not the
   scorer) — attribute the miss to the model, not a harness artifact.

## E. Alternates (swap-in candidates, fully specced by the design agents)
- `ldo_ghostcap` — two-cap output net (on-die + bulk behind `Riso`), load-dependent effective Cout, frozen
  shared `C` → offgrid Zout bad, in-sample clean. *(overlaps A3's "invisible/offgrid" theme + v10 Cout-latch.)*
- `ldo_rhpz` — right-half-plane Zout zero (non-min-phase output impedance): magnitude right, phase 30–60° wrong;
  composite fooled (`zphase` weight 0.04), systest sideband phase catches. *(high value; systest already sees it → weighting note, not coverage gap.)*
- `isrc_softknee_subthreshold` — soft weak-inversion I-V knee dropped by keep-best-vs-none + hidden by the
  `0.5·max` plateau mask. *(labeled mostly a TUNING question, not a structural defect.)*
- `isrc_vbias_noise` — flicker corner walks with compliance Vo; `crossval_isrc` has NO noise check at all
  (gate-coverage gap). *(strong alternate if you want a 5th current-path gate-gap.)*

## F. Reproduce / commands
- Back-test that motivated this: `python3 harness/crossval.py --all` ; `python3 harness/crossval_isrc.py`
- After build: `python3 harness/run_matrix.py --reuse {new voltage ids}` ; `python3 harness/crossval.py --variant {id}`
