# HANDOFF — LDO behavioral-model builder (as of 2026-06-07)

## UPDATE (2026-06-07) — published to GitHub + made Linux-portable; phase-plan Tasks 1–3 closed
**Repo:** https://github.com/weisbert/LDO_modeling (PUBLIC, branch `main`). The user now also works
the project on a **Linux** box via `git clone`/`git pull`. Added `README.md` (Linux setup +
quick-start), `requirements.txt` (numpy / scipy≥1.15 for AAA / matplotlib / scikit-rf), `.gitignore`.
Tracked = source + GT netlists + device cards + emitted models + `results/ref/*.npz` (so `--reuse`
works after clone) + `matrix.{md,json}` + docs. **Ignored** = `.venv/`, `tools/` (59MB **Windows**
ngspice — `apt install ngspice` on Linux), `work*/`, paper extracts, generated plots/logs.
Portability fix: `harness/ng.py` resolves ngspice as `$NGSPICE` → bundled Win exe → `ngspice` on PATH.

**STATUS — phase-fidelity plan COMPLETE (Tasks 1–3):**
- Task 1 (PSRR non-min-phase PHASE): DONE — complex-conjugate section. pphase v4 25→1, v3 10→2,
  v1 6→3, v2 3→2; composite v4 5.6→4.0, v3 9.0→6.3; zero regression. (2026-06-06c below.)
- Task 2 (V4 e^-sτ delay all-pass): MOOT — v4 hit 1° without it.
- Task 3 (Zout): DONE — scikit-rf passivity gate; Zout-mag residuals proven to be FLOORS (v3 GT
  non-passive; v1/v2 high-ESR cap underdetermined), not fit bugs. (2026-06-06d below.)

**NEXT = Task 4 — Target B (the real Cadence LDO).** Pipeline to build: import Cadence-extracted
Zout/PSRR/noise/spur → fit (existing `harness/fit_model.py`: Zout RLC + PSRR real+complex bank +
Norton noise + spur tones) → emit `.lib`/`.va`. Validation beyond ngspice (no PSS/HB locally):
run the emitted subckt under **Xyce multi-tone `.HB`** to check 304MHz sideband asymmetry (hot-S =
S + conjugate-T; asymmetry is carried by PSRR/Zout PHASE, now fixed), and compile the `.va` via
**OpenVAF→.osdi**, cross-check AC/tran in ngspice v39+ and HB in **VACASK**. Beware SpectreRF
shooting-Pnoise under-reporting LF supply-noise upconversion. Keep switching/SIMPLIS-POP offline.
Current matrix (Target-A synthetic variants): `results/generalization/matrix.md`.

## UPDATE (2026-06-06d) — TASK 3 DONE: scikit-rf Zout passivity gate + Zout residuals proven to be floors
Added the passivity gate and rigorously characterized the remaining Zout-magnitude error.

**Delivered (`harness/score.py`):** scikit-rf passivity gate `_passivity` — converts the 1-port Zout
to S11=(Z−z0)/(Z+z0) and tests |S11|≤1 (= Re(Z)≥0) via `skrf.Network`. Reports synth PASS/FAIL
(our positive-element RLC is passive-by-construction → always PASS = an HB-convergence guardrail that
would catch any future non-passive realization) + a GT-vs-synth min-Re(Z) **diagnostic**. Summary
fields + matrix columns `zpass_ok`/`minre_gt`. **scikit-rf 1.12.0 installed** (BSD-3, pip, user-approved).

**KEY FINDING — v3 GT Zout is NON-PASSIVE** (min Re Zgt = −0.23 Ω): a regulated LDO actively
sources/sinks, so Re(Zout)<0 in the loop band. Our passive RLC has Re(Z)≥0 by construction → it
*fundamentally cannot* reproduce v3's negative-Re regions. So v3's residual (zrms 1.14 / zband 0.78 /
pkf 5.01) is a **passive-model floor, not a fit bug**. v3 is the ONLY non-passive GT (13 others
Re>0). A non-passive realization would need controlled-source negative R = HB-stability risk →
rejected; passive-by-construction stays (it's the #1 HB convergence lever) and the floor is documented.

**Tried and reverted (net-negative):** (1) AAA-seeded `fit_zout` multi-start — matrix-neutral
(existing multi-start already finds adequate basins) + a tiny v3 +0.1 → reverted. (2) Joint
Rp‖(ESR+1/jwC) LS Cout/ESR extraction for the high-ESR no-cap-band case — **underdetermined** (when
ESR≫output-R the cap is electrically near-invisible: unbounded LS sent v2 to 1e269 F, bounded+keep-best
to 1 pF) → reverted to the legacy median; documented v1's 381 pF-for-true-1 nF as a known edge case.
New helper `harness/analyze_zout.py`. Final matrix = post-Task-1 (zero regression); all `zpass_ok=True`.

**NEXT = Task 4 (Target B).** Phase-plan Tasks 1–3 closed; Task 2 (delay all-pass) was moot.

## UPDATE (2026-06-06c) — TASK 1 DONE: non-minimum-phase PSRR PHASE closed (matrix-validated)
Implemented + validated the lead task from 2026-06-06b. The SOLE-real-gap (non-min-phase PSRR phase)
is closed; all inside the no-`laplace_nd` constraint (R/L/C + controlled sources).

**What changed (`harness/fit_model.py`):**
- `psrr_model` gains ONE signed **complex-conjugate 2nd-order section**:
  `i_c += (b0 + b1·s)/(1 + s/(Q·w0) + (s/w0)²)`. **N2=1 is the sweet spot** — N2≥2 overfits /
  destabilizes (v3 20µ blew to 107°). Realized as a series **Rpc-Lpc-Cpc** lowpass *state* x=V_C with
  two VCCS taps: `Gqb0` reads V_C=x (b0 path), `Gqb1` reads V_R=a1·dx/dt (b1 path) → exact
  `(b0+b1 s)/(1+a1 s+a2 s²)`. Pole always stable (Re<0 by construction); Q≤0.5 degrades to real.
- New `_bank_fit`: **AAA-initialize** (`scipy.interpolate.AAA`, conjugate samples → real-coeff poles)
  the dominant complex pair + 3 real poles, then **least_squares-polish** on the EXACT realizable form
  (residual = complex-log ⇒ mag-dB + phase-deg jointly). Raw AAA OVER-FITS (3–6 spurious pairs, Q~1700,
  artifact pairs at 220–415 MHz) ⇒ AAA is **only an initializer**, never dumped in.
- Selector = **prefer-complex keep-best** (zero-regression by construction): shelf short-circuit if
  `e_shelf<0.05 AND shelf-phase<2.5°` (protects base + 8 A-layer + spur DUTs); else candidates
  {shelf, real-SK, complex} and PREFER complex when its residual ≤ max(2× best, 0.15). **Lesson:** a
  pure-REAL SK fit of a NOTCH shows a lower *analytic* residual but *realizes* with huge phase error
  (v4 250µ read **25°** in ngspice though analytic said 0.4° — fragile near-pole-zero cancellation), so
  never let analytic residual rank a real fit of a notch.
- **Holistic noise fix:** SPICE PSRR LP-filter resistors → **noiseless VCCS-conductances**
  (`Grp1/2/3`, `Grpc`). The PSRR path is a *signal* path; its filter Rs must add no thermal noise.
  Matches the `.va` mirror (already noiseless) and **fixed v4 noise 3.6→0.7 dB** (old `Rp1-3` leaked).
- `emit` + `emit_va` both updated (params `pcb0,pcb1` linear-interp; `pcw0,pcq` log-interp; nodes
  `ncs1,ncs2`). New analysis helper **`harness/analyze_psrr_phase.py`** (AAA decomposition + N1/N2 sweep).

**Results (`run_matrix.py --reuse`):** pphase_max **v4 25→1, v3 10→2, v1 6→3, v2 3→2**; composite
**v4 5.6→4.0, v3 9.0→6.3** (also pband 1.78→0.44), v1 9.2→8.9, v2 7.0→6.8. **ZERO regression** —
base/cout10n/cout4n7/esr_hi/iq_lo/iq_hi/wp_big/cg_hi/v5/v6 composites IDENTICAL.

**Task 2 (V4 `e^-sτ` delay all-pass) is MOOT** — the single complex section reached v4 1° without it.
**NEXT = Task 3 (Zout):** AAA auto-order Zout fitter — fixes v3 `pkf_121=5.01` (migrating resonance)
and the Zout MAGNITUDE that now dominates the remaining composite (v1 zrms 1.94/zband 1.47 [ESR=30],
v2 1.20/0.95 [small Cout], v3 1.14/0.78); + scikit-rf passivity gate on Zout-ONLY (NEW pip dep — confirm
before adding to a vendor-facing deliverable). Then Task 4 = Target B.

## UPDATE (2026-06-06b) — RESEARCH ROUND (no code changed): modeling-method + OSS surveys done
Before resuming the Zout/PSRR fidelity work, we ran two multi-agent surveys (data/plan only,
NO code touched this round). Full writeups in **`research/`**:
- **`research/MODELING_SURVEY.md`** (+ `modeling_survey_raw.json`) — mainstream LDO/supply modeling
  methods vs ours. Verdict: our per-block 2-port (Zout + PSRR + shaped-Norton noise + injected spur
  tones, folded by PSS/HB+PAC/PXF/Pnoise) IS the field's "recommended composite"; we are AHEAD on
  noise-decoupling, spur discipline (fundamentals-only + GCD manifest + aggressor-at-vin),
  phase-aware multi-variant scoring, and convergence-by-construction. **SOLE real gap = non-minimum-
  phase PSRR/migrating-Zout PHASE** — our PSRR uses STRICTLY-REAL first-order signed sections
  (`_sk_fit` reverts to a min-phase shelf when poles come out complex) → bounded phase ceiling →
  V4 mag 0.04 dB but phase 25°, V3 phase 10°. It's an UNDER-EXPLOITATION problem (we run SK then
  discard its complex/RHP content at realization), not reinvention.
- **`research/OSS_SURVEY.md`** (+ `oss_survey_raw.json`) — no OSS builds an LDO behavioral supply
  model (every OSS LDO generator omits Zout/noise/spur extraction = our moat). ADOPT (ranked):
  (1) **`scipy.interpolate.AAA`** — VERIFIED already in our venv (scipy 1.17.1), auto-order, returns
  COMPLEX poles + `.residues()`, BSD-3, zero new dep; (2) **scikit-rf** `passivity_test/enforce`
  (= Gustavsen-Semlyen half-size Hamiltonian) as the Zout passivity gate, BSD-3, pip; (3) **Xyce**
  multi-tone `.HB` + (4) **OpenVAF-reloaded/VACASK** to HB-validate sideband asymmetry and compile
  the `.va` outside Cadence (ngspice has no PSS/HB). Keep bespoke: extraction, the 4 behavioral
  blocks, the physical synthesizer, the `e^-sτ` delay all-pass, and OP-parameterization.

**NEXT (resume coding here) — tighten non-min-phase PSRR/Zout PHASE, in this order (all inside the
no-laplace constraint):**
1. **PSRR complex-conjugate sections.** Fit `i_c = H/Zout` with `scipy.interpolate.AAA`, KEEP the
   complex poles (stop discarding them in `_sk_fit`, fit_model.py:147). Extend `psrr_model` G-bank
   from `G0 + Σ Gᵢ/(1+s/wᵢ)` (real poles only) with **signed 2nd-order (complex-conjugate) RLC+VCCS
   sections** so notch PHASE is exact. Re-score V4 (target: phase 25°→single digits) + zero
   regression on the 9 min-phase variants.
2. **V4 "390° phase race": explicit delay extraction.** `H(s)=e^-sτ·H_rational(s)`; extract τ from the
   linear-phase slope (`bringup.py:_minphase_score` already DIAGNOSES it — add SYNTHESIS), realize
   `e^-sτ` as a low-order Bessel/Padé all-pass of R/L/C+controlled sources.
3. **Zout via AAA auto-order** (replaces fixed 1–2 R-L LS) → fixes V3 migrating multi-pole resonance
   (0.27→10 MHz with load); then synthesize to RLC as now. Add scikit-rf **passivity gate on Zout
   only** (never PSRR) as a hard gate in `score.py`.
4. Then **Target B** (real Cadence LDO): extract→VF/AAA→RLC pipeline; validate sideband asymmetry
   with a hot-S (S + conjugate-T) check under Xyce `.HB`; beware SpectreRF shooting-Pnoise
   under-reporting LF supply-noise upconversion; keep switching/SIMPLIS-POP characterization offline.

## UPDATE — generalization study DONE + per-block model architecture (see GENERALIZATION_REPORT.md)
The generalization experiment has been run. The method generalizes broadly; the harness is
multi-DUT (`harness/variants.py`, `run_matrix.py [--reuse]`, `bringup.py`) and the fitter was
upgraded (auto-Cout extraction, robust multi-start Zout fit, R_pl damping, optional 2nd R-L
branch). Built 4 new GT architectures (`ground_truth/ldo_v{1,2,3,4}_*.lib`) + 7 param sweeps;
results in `results/generalization/matrix.md`.

**Architecture adopted: per-block swappable model + data-driven selector** (Zout/PSRR/noise/
dropout blocks, each auto-selected from data — composes better than N monolithic models).
**PSRR block DONE & validated:** non-min-phase PSRR identified by Sanathanan-Koerner rational
fitting, realized as a bank of signed first-order real-pole sections (min-phase shelf = the
1-section case, auto-selected). Closed V4 (composite 33->5.5, PSRR band 6.7->0.04dB) with zero
regression. (`_sk_fit`/`_shelf`/`fit_psrr` in `harness/fit_model.py`.)

**NOISE block + SPUR block DONE & validated (2026-06-06)** — see GENERALIZATION_REPORT.md §6/§7.
- **Noise (Part A):** decoupled Norton-@vout (white + 6 Lorentzians, `In=Sv/|Zout|`, joint
  shared-corner fit). Closed V1/V2/V3/V4 noise (npsd 9.6/8.4/21.8/3.9 → 1.0/2.1/2.1/3.6, all
  ≤3.6 dB), zero regression. Bugs fixed: ngspice case-insensitive params (noise g1↔PSRR G1)
  and `exp(quad)` interpolation overshoot (now envelope-clamped in `_pexpr`).
- **Spur (Part B):** deterministic SIN current tones at vout (`I_k=vout_amp/|Zout|`),
  transient-FFT characterized (`harness/spur_char.py`), fundamentals-only (IM excluded),
  PSS/HB manifest (commensurate vs incommensurate). GT aggressors `ldo_v5_spur` /
  `ldo_v6_spur2`. Reproduced to amp 0.00 dB / phase ~1e-5 rad, 0 missed/false. External
  supply spurs ride the existing PSRR port (documented, not emitted).

**NEXT:** (1) tighten Zout/PSRR on the residual hard architectures — V1 flat source-follower
Zout (zrms 1.94), V3 migrating multi-pole resonance (pkf 5.0, pband 1.78), V4 PSRR phase (25°);
these now dominate the composite, not noise/spurs. (2) **Target B** (real Cadence LDO) —
harness is DUT-generic; fitter auto-discovers Cout/Zout/PSRR/noise/spurs. Full writeup +
coverage map in **`GENERALIZATION_REPORT.md`**.

## Where we are (original Target-A handoff below)
**Target A (methodology on a local ground-truth LDO) is DONE.** We built the full
feedback loop AND a fitted behavioral model that reproduces the GT to **composite 3.8**
(stub baseline 403), meeting every small-signal + noise acceptance target, plus exact
large-signal DC/transient dropout. Deliverables: SPICE (`model/ldo_model.lib`) +
Verilog-A (`model/ldo_model.va` + `model/ldo_dropout.tbl`).

## The modeling METHOD (what to re-apply to other LDOs)
A 2-port `ldo_model(vin vout)`, all linear/passive + controlled sources (NO laplace_nd,
PSS/HB-robust), OP-parameterized by `iload`:
- **Zout(s)** = `(R_a + sL_a) || (ESR + 1/sCout)`  — Cout/ESR fixed physical, {R_a,L_a} fit
  per load corner. LF floor=R_a, resonance @ 1/2π√(L_aCout), HF=cap rolloff.
- **PSRR** = shaped supply-coupling current into vout, **filtered by the same Zout**:
  `i = g_hf·(vin-1.05) − (g_hf−g_lf)·LP(vin-1.05)`. Couple only AC ripple (ref node 1.05);
  NO broadband line-reg term in the DC source (it makes a parasitic flat PSRR floor).
- **Noise** = series voltage-noise in branch A → rides the Cout divider → flat(+1/f) floor,
  resonance peak, rolloff (matches GT shape). SPICE: white R + 3-Lorentzian RC pink ladder.
  Verilog-A: native `white_noise + flicker_noise` (exact 1/f). Keep PSRR path for external
  vdd noise. This generalizes the legacy "vdc + worst-case-PVT noisefile" trick.
- **Large-signal** (`slew_en=1`, default 0): branch-A resistor → nonlinear conductance =
  exact GT DC dropout curve via `pwl()` (SPICE) / `$table_model` (VA); La gives di/dt slew.
  MUST be a B-source `I=f(V)` (nonlinear conductance), NOT a series current source (that
  kills the resonance). Per-corner offset-corrected so small-signal R_a stays right.

## Files (core)
```
harness/ng.py            ngspice subprocess driver + wrdata parser
harness/bench.py         DUT-GENERIC measurements (zout/psrr/noise/loadstep/dc) — reusable
harness/gen_reference.py GT -> results/ref/gt_ref.npz (23 arrays: z/p/noise/trans/dc/dropout/ibp/hf)
harness/fit_model.py     scipy fit + EMIT ldo_model.lib + .va + .tbl
harness/score.py         feedback loop: grade model vs reference (Zout/PSRR mag+phase,
                         transient, noise, spur gate, weighted composite)
ground_truth/ldo_gt.lib  GT LDO (PMOS-pass + 5T NMOS OTA). models/*.mod have flicker (kf=4e-29)
model/ldo_model.{lib,va} + ldo_dropout.tbl   DELIVERABLES
```
Scratch (ignore): harness/{recon,characterize,spur_test,tune_loop,verify_*}.py
Run: `.venv/Scripts/python.exe harness/gen_reference.py` then `harness/score.py`
Re-fit: `.venv/Scripts/python.exe harness/fit_model.py`

## Verified results (slew_en=0 unless noted)
Zout band 0.02–0.04 dB · peak <1 dB exact-freq · phase <1° · PSRR band 0.1 dB ·
transient-lin droop <0.3% ring-correct · noise PSD ~1 dB / peak −0.7 dB / int −3% ·
(slew_en=1) DC dropout exact · 5 mA dynamic dropout exact (wrms 1%) · 1 mA wrms 13%.

## Open items (not blocking next phase)
1. **1 mA step initial droop spike +18%** (dynamic gm-expansion) — needs current-dependent
   damping if wanted; 5 mA dropout & DC are exact.
2. **Verilog-A is untested locally** (ngspice can't run VA) — faithful translation of the
   validated SPICE topology; verify on Spectre/OpenVAF. `$table_model` control string may
   be version-specific.
3. **Target B (real LDO in Cadence)** — workflow drafted (characterize via ac/noise/dc →
   import → fit → use). User will do this in a Cadence environment later. Artifacts to build
   then: OCEAN characterization script, `import_cadence.py` (CSV→gt_ref.npz), system PSS/pnoise
   acceptance TB. See chat for the full BUILD/USE workflow.

---

# NEXT CONVERSATION: GENERALIZATION EXPERIMENT
**Question:** does the current modeling method generalize to LDO architectures *other* than
the PMOS-pass / 5T-OTA GT — or where does the fit topology break, and how to extend it?

## Plan
1. **Refactor first (small):** `gen_reference.py` and `fit_model.py` hardcode `ldo_gt` /
   `gt_ref.npz`. Parameterize them by (lib, subckt, ref-path) so multiple LDOs can be run.
   `bench.py` is already DUT-generic — no change needed.
2. **Build a family of alternative GT LDOs** in `ground_truth/` (same nlv/plv cards), varying
   the architecture to stress each assumption:
   - **NMOS-pass / source-follower LDO** — low Zout (≈1/gm), high PSRR, little peaking →
     stresses the PSRR path and the resonance assumption.
   - **Cap-less / small-Cout LDO** — internal dominant pole, higher UGB → tests whether the
     spur band is still ABOVE UGB (still linear) and whether 2-branch Zout still fits.
   - **2-stage Miller-compensated OTA LDO** — extra pole → possibly two resonances or a
     different Zout roll-off slope → tests if 2 RLC branches are enough.
   - **Feedforward / RHP-zero PSRR LDO** — non-minimum-phase PSRR → tests whether PSRR =
     shelf×Zout (shared resonance) is flexible enough (adversarial verifier flagged this).
   - **Cout/ESR & quiescent-current sweeps** — expected to generalize (sanity).
3. **Run the pipeline per variant:** gen_reference → fit_model → score. Tabulate composite +
   which sub-metric breaks (Zrms/Zband/peak/PSRR/noise/transient).
4. **Diagnose & extend:** for each breaker, identify the violated assumption and extend the
   topology minimally (e.g., add a 2nd parallel RLC branch for a 2nd resonance; give PSRR its
   own pole/zero instead of sharing Zout; revisit the noise divider; check UGB-vs-spur-band).
5. **Deliverable:** a generalization report — which LDO classes the method covers as-is, which
   need extensions, and an upgraded `fit_model.py` that auto-selects topology order.

## Watch for (assumptions most likely to break)
- Zout with **>1 resonance** or a non-cap HF roll-off (2-branch RLC insufficient).
- **Non-minimum-phase PSRR** (feedforward/RHP zero) — shelf×Zout won't fit.
- **UGB inside/above the spur band** → disturbances engage the loop → nonlinear; the
  "spur band is linear" finding may not hold → may need a different (nonlinear) approach.
- Noise shape not matching the branch-A-divider form (different loop noise-gain shape).
