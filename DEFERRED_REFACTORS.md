# Deferred refactors

Changes we agreed to make but **deliberately postponed** so as not to disturb an in-flight task.
Each item is self-contained and actionable. Append new items at the bottom.

---

## R1 — De-hardcode the large-signal step magnitudes (`trans_big` / `trans_slew`) and the nominal corner

**Status:** OPEN — defer until the *current* real LDO (Target B) is modeled end-to-end, then do
all of R1 in one pass (changing it mid-modeling would invalidate the in-progress reference set).

**Raised:** 2026-06-08, while drafting the Target-B Cadence test matrix.

### Problem
`trans_big = 1 mA` and `trans_slew = 5 mA` are **absolute** values picked for the Target-A
synthetic LDO (≈121 µA-class). They are meaningless on a different part: a 100 mA LDO is still
linear at 1 mA and won't slew at 5 mA; a tiny LDO is already past dropout at 5 mA. They are also
implicitly bound to the `121u` nominal corner. The magnitudes are hardcoded in **two places that
MUST agree** (the scorer re-simulates the model with the *same* step it compares against the GT):

- `harness/bench.py:26` — `STEP_DI = {"lin": 50e-6, "big": 1e-3, "slew": 5e-3}`
- `harness/bench.py:25` — `STEP_BASE = 121e-6`  (and `LIN_FRAC = 0.3`, line 27 — *this one is
  already relative → KEEP*)
- `harness/bench.py:16` — `LOADS = ["20u","121u","250u"]`  (global corner list + nominal)
- `harness/score.py:152-159` — big/slew loop hardcodes `iload=121u` / `iload=121e-6` and
  `bench.STEP_DI[tag]` for the **model re-sim**
- `harness/score.py:234` — `rows[1]` assumes nominal == middle corner (index 1)
- `harness/gen_reference.py:99-104` — GT side: `bench.STEP_DI[tag]`, `iload=121e-6`,
  array name `trans_{tag}_121u`

### Why it's safe to change (context for whoever picks this up)
`trans_big`/`trans_slew` are **NOT model-fit targets** — the only LTI transient fit target is
`trans_lin` (which is already relative: `LIN_FRAC*bias`). `big`/`slew` are large-signal
**diagnostics** run through the nonlinear core (`slew_en=1`) to locate where the linear model
stops being valid (gm-compression onset / slew-dropout edge); they feed the validity-envelope /
linearity-floor reporting. So re-picking their magnitudes does **not** perturb the linear model —
it only moves where we probe the nonlinear edge.

### Fix (make it profile-driven)
1. Add to each variant in `harness/variants.py` (and to the Cadence import profile in
   `cadence/import_cadence.py`):
   - `nominal` — nominal corner key (replaces the `LOADS[1]` / `"121u"` assumption)
   - `imax` — rated max load current [A]
   - `step_big`, `step_slew` — **as a fraction of `imax`** (recommended) or absolute [A].
     Suggested defaults: `big ≈ first-compression step (~few× nominal / ~10% Imax)`,
     `slew ≈ largest meaningful step toward rated max / dropout`.
   - (optional, same spirit) let `loads` / `STEP_BASE` come from the profile instead of the
     global `bench.LOADS` / `bench.STEP_BASE`.
2. `bench.py`: compute `STEP_DI` from the profile (or accept an override) instead of the module
   constant. Keep `lin` relative (`LIN_FRAC*bias`).
3. `score.py:152-159` + `gen_reference.py:99-104`: read `nominal`, the per-tag step, and the
   nominal `iload` from the profile — drop the literal `121u` / `121e-6` / `STEP_DI[tag]`.
4. `score.py:234`: index the nominal row by the profile `nominal`, not `rows[1]`.

### Invariant to preserve
The ΔI you simulate for `trans_big`/`trans_slew` **on the Cadence/GT side** must equal the ΔI the
scorer uses for the **model re-sim**. That is the whole reason these live in ONE profile value
read by both `gen_reference` and `score`.

### Acceptance
- Re-run the existing variant matrix (`harness/run_matrix.py`) → **zero composite-score delta**
  on the current variants (defaults reproduce 50µA/1mA/5mA @121u).
- A new variant with a different `imax`/`nominal` produces sensible big/slew probes with no code
  edits.

---

## R2 — Emitted `.va` (and `.lib`) has no GND terminal

**Status:** OPEN — batch with R1/R3 after the current real LDO is modeled.

**Raised:** 2026-06-08, user instantiated the emitted Verilog-A symbol in Cadence.

### Problem
`emit_va` declares `module ldo_model(vin, vout); inout vin, vout;` (`harness/fit_model.py:868-869`)
— only two terminals. Every `V(node)` inside is referenced to the simulator's **global ground**,
so the Cadence **symbol has no GND pin**. The `.lib` subckt has the same shape
(`.subckt ldo_model vin vout iload=… slew_en=0`, `harness/fit_model.py:733`, uses node `0`).
Fine for a quick ngspice deck; wrong for a real schematic where ground must be an explicit,
wireable terminal.

### Fix
- Add an explicit `gnd` (a.k.a. `vss`) port to the module: `module ldo_model(vin, vout, gnd);`
  and reference all branch voltages relative to it — `V(x)` → `V(x, gnd)` throughout `emit_va`
  (and the matching `.lib` subckt nodes). Mirror in the symbol (3+ terminals).
- Decide terminal count with R3: at minimum `vdd/vin, vout, gnd`.

### Acceptance
- Symbol shows a GND pin; the model netlists into a bench with a non-global ground.
- With `gnd` tied to `0`, the variant matrix composite is **unchanged** (pure re-referencing).

---

## R3 — VDD is not a settable / sweepable supply on the emitted model

**Status:** OPEN — batch with R1/R2 after the current real LDO is modeled.

**Raised:** 2026-06-08. User: "Q'ing the symbol there's nowhere to set VDD; I will run high /
nominal / low supply corners, so VDD must be changeable."

### Problem
`vin` is a port, but the operating reference is **hardcoded**: `V(vrf) <+ {VREF:g};`
(`harness/fit_model.py:896`, the nominal vin baked in at emit). Only the *small-signal*
deviation `(V(vin) - V(vrf))` couples to vout via the PSRR bank (lines 925-940); the **DC**
output is pinned by `VREG121` (line 876/897). So:
  - there's no meaningful VDD to "fill in" on the symbol, and
  - sweeping VDD high/nominal/low produces **no correct DC line-regulation** response.
The `dc_linereg` array IS characterized (`ref["dc_linereg"]`) but is **unused** in `emit`/
`emit_va` (only `dc_loadreg`/`dc_dropout` feed the dropout `.tbl`, line 827) — i.e. the data
needed for the fix is already in the npz.

### Fix — two layers
- **L1 (interface + DC line-reg; cheap, do first):** expose VDD as the supply port/parameter and
  make `VREF` a settable `parameter real` (default = characterized nominal). Drive the DC output
  from the unused `ref["dc_linereg"]` curve so Vout tracks VDD (a `$table_model` on vin, like the
  dropout table), with the small-signal PSRR riding on `(V(vin) - VDD_op)`. Gives correct DC across
  the supply sweep + a settable VDD with no re-characterization.
- **L2 (small-signal across VDD; larger scope):** the model's small-signal blocks (Zout / PSRR /
  noise / spur) were extracted at ONE vin. True accuracy at high/low VDD needs them characterized
  at multiple supply corners — i.e. extend the per-corner scheme from `iload`-only to `vin × iload`
  (a second interpolation axis), analogous to the load corners. Touches `gen_reference` /
  `import_cadence` / `fit_model` / the per-corner interpolation.

### OPEN QUESTION FOR USER (decides L1-only vs L1+L2)
Do you need **small-signal** fidelity (Zout/PSRR/noise/spur) *at* the high/low VDD corners, or is
**correct DC line-regulation + nominal-VDD small-signal** enough? If the latter, L1 alone suffices
and is cheap; if the former, we add the `vin × iload` characterization axis (L2) and you'll need to
export the AC/noise/spur sets at each supply corner too.

### Acceptance
- Symbol exposes a settable VDD (param or pin); DC `Vout` tracks the `dc_linereg` curve across a
  VDD sweep; nominal-VDD scores unchanged.
- (If L2) per-(vin,iload) corner fit reproduces GT Zout/PSRR within the usual tolerances at each
  supply corner.

### Related
Ties to `CADENCE_EXTRACTION.md` handoff note #1 (model hardcodes 1.05 V as `Vrf`; "if different it
gets parameterized") and the tool-generalization intent (don't bake in 1.05 V / 121 µA). R2 and R3
share the port-list change — do them in one pass.

================================================================================
# Open concerns — VALIDATION & QUALITY (design-level, not just mechanical refactors)
================================================================================
Raised by the user 2026-06-08 (record-only; fix in a later pass). These are higher-stakes than
R1-R3: they question whether the method is *validated for its actual use case* and surface real
model-quality defects on the first real LDOs.

## R4 — The feedback loop never validates the REAL use case (LDO + RF buffer at the carrier)

**Status:** OPEN — likely the single most important item. User challenge (verbatim intent): "does
our SPICE validation path ever compare the model vs the real LDO under an LDO + xxxMHz-buffer
TRANSIENT working condition?"

### The gap (verified)
`score.py` compares model-vs-GT on CHARACTERIZATION stimuli only: Zout, PSRR, noise, load-step
transients (lin/big/slew), discrete spurs, DC. Plus an 8 MHz single-tone **load-tone SANITY gate**
(`bench.measure_spur`, `measure_spur` 16/24M < -45 dBc) — but that is (a) a single tone, (b) at
8 MHz not the real carrier (~304 MHz synthetic / GHz-class real), (c) a pass/fail sanity, NOT a
model-vs-GT ripple/sideband comparison, (d) not against a real buffer load. There is **no
implemented system-level test** that drops the model AND the GT each into the actual scenario —
an LDO feeding a representative buffer that draws periodic current at the carrier — and diffs the
resulting vout ripple + sideband spectrum under tran and PSS/HB. `HANDOFF.md:318-319` itself lists
"**system PSS/pnoise acceptance TB**" as a Target-B artifact still TO BUILD — confirming it's absent.
So the whole project goal (PSS/HB spur/sideband fidelity around the carrier) is **asserted via
building-block fidelity, never measured end-to-end.**

### Fix
- Build a SYSTEM ACCEPTANCE TB: a parameterized buffer/aggressor load (periodic current at the
  carrier, representative magnitude) on vout; run BOTH the GT LDO and the emitted model; compare
  vout **ripple amplitude/phase at the carrier** and the **sideband spectrum** (the real deliverable).
- Run it in transient AND (where available) PSS/HB (Xyce `.HB` / VACASK for the `.va`, since ngspice
  has no PSS/HB). Make this the TOP-LEVEL acceptance metric in `score.py`, above the block metrics.
- This closes the loop the user is rightly questioning and is the natural home for R6's symptoms.

### Acceptance
- Model reproduces GT vout ripple at the carrier within target dB across load (and VDD, see R3)
  corners, and the sideband spectrum matches to the spur tolerance.

## R5 — Too much manual characterization input; simplify the user's burden

**Status:** OPEN. User: "the manual sim data I must input is too much; simplify the user input."

### Problem
Modeling one LDO currently needs ~30 hand-exported files (z/p/noise/trans_lin/spurs per corner x3
corners + the two `*_hf` + dc_loadreg/linereg/dropout + spur_500u). Hand-exporting and naming all
of these is the bulk of the user's effort and an error surface (the silent-mismatch traps).

### DECISION (user, 2026-06-08): NO OCEAN/SKILL script (too much to implement). Reduce the
### USER-SIDE input burden only — fewer/required files, multi-corner files, auto-derived scalars.

### Key finding — most of the "~30 files" are OPTIONAL or VALIDATION-only (verified in fit_model.py)
The FIT only consumes: `z_{il}`, `p_{il}`, `noise_{il}` per corner (core) + `dc_loadreg` (used ONLY
to read per-corner DC Vout `vreg`, one number/corner, `fit_model.py:461,472`) + `spurs/spurs_raw`
(only if intrinsic spurs). Everything else is NOT needed to build:
- `trans_lin/big/slew` — **read nowhere in fit_model** (transient is a consequence of Zout; score-only).
- `dc_linereg` — **read nowhere at all** (dead input until R3).
- `dc_dropout` + dropout `.tbl` — only large-signal `slew_en=1`; irrelevant to the linear PSS/HB model.
- `z_hf`/`p_hf` — optional, auto-fallback to `z`/`p` (`fit_model.py:45`); one wide AC sweep covers both.
- `spur_500u`, `ibp_xfer`, `meta_cout/esr` — sanity-gate / optional / self-check.
=> full small-signal+noise+spur model ≈ 10 files; bare linear model ≈ 7. Not 30.

### Fixes (cheap → bigger), user-side
1. **Document a MINIMAL "linear RF" input set** + a mode that skips the large-signal/validation set
   (trans_*, dc_dropout, dc_linereg, spur_500u, ibp) and marks large-signal fidelity "unvalidated".
   (Mostly docs + a guard so the fit doesn't require the absent arrays.)
2. **One MULTI-CORNER file per quantity** instead of one-per-(quantity,corner): accept a "wide" CSV
   (`freq, Re@20u,Im@20u, Re@121u,Im@121u, …`) or "long" (`freq, iload, Re, Im`) — matches an ADE
   parametric load sweep export. 9 core files (z/p/noise ×3) → 3. (Parser change in import_cadence.)
3. **Auto-derive the scalars** so the user types ~nothing: `vref` + per-corner `vreg` from the DC data
   or the transient baseline (instead of asking); `cout/esr` already auto; `nominal`=middle corner;
   corner currents inferred from filenames.
4. **1-corner quick-start mode**: build from the nominal corner alone (constant params, no ln(iload)
   interp) → user exports ONE corner's z/p/noise for a first model; add corners later. (Fit must
   tolerate len(loads)==1/2 → linear/constant interp; biggest "get going" lever.)
5. **Drop the `dc_loadreg` file dependency**: take per-corner DC Vout from 3 typed numbers or the
   transient baseline (it's only used for `vreg`), so no DC sweep file is required for a linear model.
6. Keep folder-drop import (`match_dir`) + GUI "Import from folder" as the one-gesture path; make the
   GUI required-file list reflect the minimal set (don't red-flag absent optional arrays).

### Already shipped toward this
`spurs_raw` auto-FFT (drop the calculator FFT) and `z_hf`/`p_hf` fallback. Connects to the
tool-generalization intent (a general RECIPE, not 30 bespoke exports).

### Acceptance
- A first model builds from **3 multi-corner files** (z/p/noise) + optional spurs, scalars auto-filled.
- 1-corner mode builds from a single corner's z/p/noise with no errors.
- Existing full-input variants still build with **zero score delta** (new paths are additive).

## R6 — Model-quality defects observed on the first real LDOs (root-cause these)

**Status:** OPEN — concrete bugs, not just polish. Investigate with `harness/report.py` (analytic
diff) + a transient/system run. Likely share roots with R3 (DC) and R4 (system/HF).

### Symptoms (user-reported, 2026-06-08)
1. **Model #1: model-vs-GT difference too large** (poor fit). ACTION: run `report.py --variant
   <name>` — its [1] composite split + [6] diagnosis localize which block/band/corner is off; then
   root-cause (often: resonance mislocation = L_a x Cout, or non-min-phase PSRR, or a per-corner
   effect the ln(iload) interp misses).
2. **Model #2 transient: the output rail (VDD feeding the buffer) slowly DROOPS down.** HYPOTHESIS:
   the Verilog-A inductor branches use `idt()` (`fit_model.py:904,909` — `I<+ idt(V)/L`) which can
   drift over a long transient without a settled DC / IC; and/or the DC output isn't pinned to the
   actual VDD (R3 — DC set by `VREG121`, `dc_linereg` unused). Check: DC operating point, idt initial
   conditions, whether `slew_en` path is engaged, and the R3 line-reg fix.
3. **Model #2: NO buffer-induced ripple visible on the rail.** HYPOTHESIS: Zout at the CARRIER is
   missing/wrong — the `*_hf` Zout sweep stops at 500 MHz, so if the carrier is higher the model has
   no valid Zout there (-> ripple ~ I_buf x |Zout(fc)| ~ 0). Also check the `idt` branches actually
   carry HF current in the `.va`. Directly motivates R4 (system test) + extending `*_hf` to cover the
   carrier (the "run a 6-10 GHz exploratory sweep, `*_hf` cutoff != system max freq" point).

### Note
2 and 3 are exactly what an R4 system test would catch automatically; R6 is strong motivation to do
R4. Do R6's investigation alongside R3 (DC) and R4 (system/HF coverage).

---

## R7 — PSRR-phase fit grid-sensitivity on multi-pole parts (trans-ID "D")

**Status:** INVESTIGATED + REVERTED 2026-06-09 — **NEGATIVE RESULT (the gap is NOT a robustly
fixable fitter bug; it is a coarse-grid INFORMATION/CONDITIONING limit).** A flag-gated multi-start
was built, fully gated through the 3-baseline contract, and **reverted** after diagnostics proved it is
not production-robust. The byte-identical AC contract was preserved throughout. Below is the recorded
evidence so this is not re-attempted blindly. **Re-opening requires a RECIPE-side change (see "Path
forward"), not another `fit_psrr` selector.**

**Raised:** trans-ID validation + compiled-VA e2e: on multi-pole parts (`v3_miller`, `v2_capless`) the
trans-built model scores composite **+2.18 / +2.60** vs the AC-built one, while base/v1 are within ±0.7.

### What was tried (this round) and why it was reverted
A `fit_psrr(..., regrid=False)` flag + a deterministic complex-section **multi-start** (RNG-free) +
candidate selectors (min-residual, then smoothness/parsimony) + a coarseness detector, threaded
`fit_all/fit_variant -> fit_psrr` and engaged only by the trans-ID validators. A symmetric
`fit_zout` regrid was also tried. Three oracle diagnostics (analytic per-corner PSRR vs AC GT; end-to-end
{zout,psrr}×{off,on} isolation; a wide zout multi-start) localized everything:

- **v3_miller — pure PSRR, multi-start helps but is NOT production-robust.** The single-start complex
  bank is trapped on the coarse grid (121u trans-resid 0.39 / AC-pband 3.0 dB); a multi-start finds a
  better optimum (121u trans-resid 0.19 / AC-pband 1.5) that *the oracle confirmed is also closest to AC*.
  On the **B-source dev path** this shrank v3 **2.18 → 1.61**. BUT on the **compiled-VA production path**
  the SAME recipe made v3 **WORSE** (baseline 2.14 → 2.37, `d_path` 0.04 → **0.76**) — both deterministic
  and reproduced. The coarse multi-pole PSRR fit is **ill-conditioned** (multiple near-degenerate optima);
  which one the discrete selector picks **flips with the tiny B-source-vs-VA stimulus difference**, and on
  the VA realization it picks one better on the sparse tones but worse against AC. Because no selector may
  peek at AC, there is **no way to guarantee the multi-start won't pick the harmful candidate** on a real
  engineer's single trans. (This is the same instability the "20 tones/dec made it WORSE, v3 +2.2→+5.4"
  note warned about — selection on an under-determined fit.)
- **v2_capless — NOT a fitter problem at all (information limit), oracle-proven twice.** (1) PSRR oracle:
  the min-phase **shelf is already AC-optimal at every corner** (AC-pband ≤0.87 dB); every complex/multi-
  start candidate is catastrophic (AC-pphase ~96°). The default already picks the shelf, so the multi-
  start is correctly inert (and a hard gate makes it provably so — noise-independent). (2) Zout oracle:
  **every** candidate (default 1-br/2-br AND a wide octave multi-start) returns the **identical** AC-zrms
  at every corner (20u 1.797, 121u 0.957, 250u 0.321). The +2.60 is the sparse trans grid under-
  determining a **near-invisible high-ESR cap** (Cout 130 pF / ESR 116 Ω — the documented identifiability
  floor, `finding-zout-passivity`) plus deep-PSRR low-SNR at the 20 µA corner. **No fit-side change can
  touch it.**
- **fit_zout regrid was harmful** (its parsimony/smoothness preference can drop the 2nd RL branch that
  v3 genuinely needs) and **useless** (the oracle multi-start matched the default exactly everywhere).
  Removed.

### Why reverted rather than shipped
The merge gate required `d_composite` to SHRINK on **both** `validate_trans_id` AND `validate_trans_va`.
v3 shrank on trans_id but **regressed on trans_va** (the production fixture), and shipping it would break
the valuable "VA reproduces B-source to numerical noise" property (`d_path` 0.04 → 0.76). v2 cannot move.
So no robust win exists at the fitter layer; keeping the change would trade reproducibility for a
dev-path-only, realization-dependent gain. Reverted to the stable baseline (`d_path` ≈ 0, v3 +2.18 /
+2.14, v2 +2.60). AC stayed byte-identical the entire time; `crossval --all` identifiability PASS;
`systest --all` == `baseline_Bcover`.

### Path forward (if v3/v2 must be closed) — RECIPE side, not the fitter
The limit is coarse-grid **information/conditioning**, so the lever is the **measurement recipe**
(`harness/trans_id.py` `bands_for` / tone density), NOT `fit_psrr`/`fit_zout`:
- denser tones at the hard/light corners (v3's multi-pole PSRR band; v2's 20 µA Zout/deep-PSRR), and/or
  a single cheap **AC anchor point** at the resonance to condition the multi-pole fit;
- this changes the recipe for ALL variants (it perturbs the "one cheap trans" value proposition) → it
  needs a full re-validation pass and is a deliberately separate, larger round.

### Related
`finding-trans-id-validation`, the compiled-VA e2e (`results/trans_id/trans_va_e2e.md`), the
trans-ID productionization round (`REMEDIATION_HANDOFF.md` ROUND R5-prod), and ROUND R7 there.

---

## R8 — Emitted MODEL `.va` does not OpenVAF-compile (`$table_model` dropout branch)

**Status:** OPEN — low priority; only needed if LOCAL OpenVAF/VACASK HB-validation of the *model*
`.va` is wanted (e.g. for Target B). The `.va`'s intended target (Cadence Spectre) DOES support
`$table_model`, so this is a local-toolchain gap, not a model defect.

**Raised:** 2026-06-09, during the round-2 (v7–v10) `.va` compile-check (continuation of the
"a `.va` only counts once it compiles" rule).

### Finding (verified this round)
`fit_model.emit_va` emits the optional large-signal dropout branch as
`I(vrg,nA) <+ $table_model(..., "<name>_dropout.tbl", "1L")` inside the `else` of
`if (slew_en == 0)` (`model/ldo_<k>.va:~89`). `$table_model` is a Spectre/VAMS builtin **OpenVAF
does not implement**, so `openvaf model/ldo_<k>.va` fails (`rc=65`, "'$table_model' was not found")
for **every** variant — confirmed identical on `base` (`model/ldo_model.va`), so it is a
pre-existing `emit_va` property, NOT a v7–v10 or round-2 issue, and shared code was untouched.
The default `slew_en=0` (the AC/small-signal path) never takes that branch.

**The small-signal core IS valid OpenVAF VA.** With the dead `else`/`$table_model` line removed
(behaviorally identical at `slew_en=0`), all four round-2 model `.va` files **compile with OpenVAF,
load via OSDI, run in ngspice, and reproduce their `.lib` Zout to max |va−lib| = 0.0000 dB** across
10 Hz→GHz ceiling (v7 1.2 GHz / v8,v10 600 MHz / v9 300 MHz). So only the dropout builtin blocks a
full OpenVAF build. (NB: the prior "all `.va` compiled" result — `finding-trans-va-pipeline` — was
the trans-ID *stimulus* `.va` (`trans_id.emit_stim_va`), a different artifact from this MODEL `.va`.)

### Fix (when needed)
- Gate `$table_model` behind a target/emit flag: keep it for the Spectre emit; for the OpenVAF/VACASK
  path emit a `$table_model`-free dropout (a piecewise/`analog`-expressible current-limit law fit to
  the same `dc_dropout` curve, or a smooth tanh/clamp), so `slew_en=1` also compiles locally.
- Then add the model `.va` to a local HB/AC self-check (compile → OSDI → ngspice AC vs the `.lib`,
  exactly the round-2 check) as a standing emit-side gate.

### Acceptance
- `openvaf model/ldo_<k>.va` succeeds with `slew_en` both 0 and 1; the compiled OSDI model AC-matches
  the `.lib` (and the GT within the usual tolerances); `.lib`-scored composites unchanged (additive).
