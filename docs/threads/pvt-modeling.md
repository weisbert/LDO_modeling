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

## Next action — TOMORROW (manual 3-corner route-A for Friday's report)
Plan (user, at the desk): ship TT/SS/FF as THREE SEPARATE `.lib`/`.va` files and switch them by Cadence
corner config by hand. The automated corner-keyed SECTION emit stays a LATER build item (needs ultracode go).

**THE CRUX — re-emit ≠ re-characterize.** The model has ZERO process dependence (METHODOLOGY/DATA §5):
emitting 3× from the SAME npz gives 3 BYTE-IDENTICAL files. Three real corner models REQUIRE three separate
CHARACTERIZATIONS at FF/TT/SS (3 npz from 3 box corner runs). ⇒ the long pole is whether FF/SS box
characterization data EXISTS. If only TT/nominal has been characterized, the FF/SS ALPS/Donau corner runs
must happen first — that, not the fit/emit, is the Friday risk. (Fallback if box data isn't ready: the LOCAL
synthetic 3-corner flow built tonight (`research/pvt_modeling/`) can demo the 3-`.lib`+config-switch MECHANISM
end-to-end for the slides, clearly labelled as synthetic.)

Steps:
0. **Verify the TT model runs** (the Friday baseline) — verified CLIs:
   - fit (+emit VA per output): `python -m harness.fit_multiport --variant <tt_npz_stem>
     --manifest cadence/insitu/manifests/REAL_wur_pmu_top.json --emit`
   - PMU `.va`: `python -m harness.emit_pmu_model --variant <tt_npz_stem> --manifest <same> --out model/PMU_model_tt.va`
   - sanity: local-Spectre parse-gate (`cadence/spectre_run.available()` → True) + `python harness/score.py --variant <stem>`.
1. **Characterize FF & SS** — re-run the in-situ corner flow at each process corner. The flow is ALREADY
   corner-aware (output lands in `<corner>/` dirs — cf. the existing `work/.../tt_25c/` layout;
   `cadence/insitu/pmu_corner.py` + `cadence/cluster/run_corner.py` take the PDK corner / `pdk_model_dir`).
   → `ff_*` and `ss_*` npz.
2. **Fit+emit per corner** → `model/PMU_model_{tt,ss,ff}.va` (same manifest, different `--variant`/`--out`).
3. **Config switch** — wire the 3 cards to the Cadence corner setup by hand.

**Report caveats to carry onto the slides:**
- Each card is BAKED at its fit OP → it does NOT track `vreg`/load WITHIN a corner (tonight's sharpest
  finding). If a demo changes the rail setpoint, the card won't follow — present at the fit OP or note it.
- Tonight's evidence is synthetic-toy METHOD verification: the optimism DIRECTION is solid; the HOT
  magnitudes are toy-inflated (un-bandgap bias). Frame accordingly.

## Later (build, needs the ultracode go) — the proper route-A
1. **Automated corner-keyed `.lib` SECTIONS** emit (today UN-implemented — emit produces ONE card).
2. **Corner-aware assist `iaG`** first — the one signed-dangerous param (hot over-claim; magnitude toy-capped).
3. **`vreg`-continuous headroom dependence** — let ceiling/stability/PSRR track the exposed `vreg` knob
   (headroom = Vin−vreg) continuously, like the PTAT i(T) exception, so the knob is meaningful WITHIN a corner.
4. **Envelope guard** — corner tag + warn/conservative-fallback on un-characterized corners (PVT analog of
   the `floor` seatbelt). And (method) a clean process axis on a robust DUT + adaptive freq sampling at the peak.

## Deployment mechanism — scheme ② (config+symbol flow; Spectre-verified 2026-07-01)
The SWITCHING layer for the automated route-A, chosen for the REAL flow: the LDO enters as a schematic
**SYMBOL** whose config binds a `veriloga` view → the netlister already `ahdl_include`s the `.va`, so a
`.lib` section that RE-includes it would double-define the module (the A1 form is only for a LDO dropped in
as a pure model card, no veriloga view). Scheme ② is the config-safe realization. **Shape = ONE veriloga
view with all PROCESS banks inside + a section-set global selector.**

1. **`.va`** — expose `parameter real corner_sel = 0;` (PROCESS ONLY). In `@(initial_step)`: `cs = corner_sel;`
   then an if/else ladder selects the bank (TT/SS/FF/SF/FS…). `else` = TT + `$strobe("… uncharacterized
   corner_sel=%0d …")` (the PVT `floor`-analog envelope guard). Keep `$temperature` doing continuous-T work
   (noise kT, PTAT i(T)); `vreg_*` stay the exposed setpoint params. DO NOT fold T/vreg into `corner_sel`
   (that re-creates the Cartesian product).
2. **`ldo_corners.scs`** — `library ldo_corners { section tt parameters ldo_sel=0 … section ff parameters
   ldo_sel=2 … }`. Each section ONLY sets the global `ldo_sel` (does NOT `ahdl_include` the `.va` → no clash
   with the config veriloga). Do NOT also define `ldo_sel` at top level (Spectre **SFE-59** "previously
   defined"; the section must be the SOLE definer — so an un-characterized/missing section fails LOUD at
   read-in: `No section found with name 'sf'`).
3. **CDF** — set the symbol's `corner_sel` value to `ldo_sel`. Netlist → `X0 (...) PMU_model vreg_*=…
   corner_sel=ldo_sel`.

**ADE (designer, once per corner):** add `ldo_corners.scs` to the corner's Model Files, `section=` that
corner (same axis the PDK already selects). Section names needn't match the PDK textually — each corner's
Model-File section is set explicitly, incl. `ssMOS_ffRC`-style splits.

**Why NO Cartesian blow-up:** `corner_sel` is NOT an independent swept variable — the PROCESS section carries
its value via `ldo_sel`. ONE axis → N corners stay N, not N×M. (There is NO universal built-in "corner"
system variable; `$temperature` is the only env axis, so you MUST create the single source — here it's the
section the PDK already selects.)

**Still requires N CHARACTERIZATIONS** (N npz from N box corner runs) to fill the banks — re-emit ≠
re-characterize. SF/FS = own bank+section+char UNLESS the MOS/RC separability experiment lets them be
composed (still open).

**Local proofs (2026-07-01, Spectre 18.1, scratch):** (a) `library/section`+`ahdl_include` selects the right
per-corner `.va`, same module name no clash, undefined section fatal at read-in; (b) the REAL 20 KB
`PMU_model.va` compiles (0 errors) and runs op/ac/noise/tran INSIDE a section, LF-Zout fingerprint reads back
per corner; (c) instance-param `corner_sel`+if/else switches banks; (d) scheme ② — section-set `ldo_sel` +
`corner_sel=ldo_sel` switches banks driven by `section=` ALONE (no per-instance literal). (Also: `isource`
AC uses `mag=`, not `ac=`.)

**Build (needs ultracode go):** `emit_pmu_model.py` takes N per-corner npz → one `.va` with N banks + the
`corner_sel` dispatch/`$strobe`; generate `ldo_corners.scs` from the corner list. Supersedes the
"corner-keyed `.lib` SECTIONS" wording in Later-build #1 (that A1 form clashes with the config veriloga).

## In-situ DC-robustness root-fix — SHIPPED to the generator (2026-07-01)
Running the emitted model **inside the real circuit** across PVT exposed a DC-SOLVE pathology distinct from
the accuracy question above: the DC operating point solved to a non-physical rail current (~3.7 mA on a µA
rail) with a ~25 MHz limit-cycle in transient. ROOT (box-diagnosed, then desk-reproduced pure-Python): the
model imposes its OP through near-ideal, NO-compliance sources, so a small live-vs-baked mismatch is
amplified into a huge current —
- **branch-A regulation** ties vout to the baked `vreg` through Ra≈0.02–0.1 Ω (10–48 S). A co-driven /
  off-corner output sitting sub-mV off `vreg` → 100s of mA of DC fight → ill-conditioned DC (VCO 0.078 mV →
  3.7 mA), and the startup slam lights the branch-A-L/decap ring.
- **PSRR is DC-coupled to the supply**: `sum(Gi)·(V(AVDD1P0) − vdc_AVDD1P0)` injects whenever the live supply
  ≠ the baked `vdc` (VCO ΣG=13.5 mS → 0.02 V ⇒ 270 µA). A PVT supply sweep re-injects at every corner.

Both are the "baked-OP stiffness" failure. Hand-aligning `vreg`/`vdc` per corner is a BAND-AID (breaks under a
PVT sweep — the user's key push: robust for ANY set VDD), so the fix is STRUCTURAL and DEFAULT-ON in
`emit_pmu_model.py` (no manifest opt-in):
1. **PSRR supply ref AUTO-TRACKS the live supply** — `vrf` is a slow (~1 Hz) low-pass of `AVDD1P0`
   (`Rtrk_psrr`/`Ctrk_psrr`), so `V(AVDD1P0,vrf)=0` at DC for ANY supply → zero DC injection across the whole
   PVT supply sweep; AC PSRR bitwise-preserved above the corner (baked `V(vrf)<+vdc` source removed; `vdc`
   kept only for the fA-level current-bias gdd term). Retires the supply-axis DC-injection hazard (AC
   line-reg ACCURACY is still the separate `dc_linereg` BACKLOG item).
2. **Regulation = STIFF R_a resistor** (`V(nA,vrg) <+ Ra*I(nA,vrg)`) — pins vout to vreg with conductance
   1/Ra at EVERY excursion, so the DC solve is well-conditioned for any vreg / PVT corner.

   ⚠️ **CORRECTION 2026-07-01 — a "DC current COMPLIANCE" was tried here and REVERTED.** The first cut
   wrapped Ra in `max(-Icomp, min(Icomp, V/Ra))` (later hand-patched to `Icomp*tanh`), intending "EXACTLY
   V/Ra in-band, clamped to ±Icomp beyond." WRONG: the clamp knee is a **voltage** = `Icomp·Ra` ≈ 20 mA ×
   0.095 Ω ≈ **1.9 mV** for the near-zero Ra, so >~10 mV off vreg the regulation saturated to a
   **zero-conductance ~Icomp current source** → the output node lost its DC pin → **FF-corner runaway /
   non-convergence** (user-reported; local Spectre reproduced: floating PLL rail + a small current imbalance
   → **2.31e6 V** with the clamp vs **0.804 V** with the resistor; DC pin-strength sweep shows far-from-vreg
   conductance collapsing to 6e-4 S vs the intended 10.5 S). TT/SS only "worked" by happening to stay inside
   the ±2 mV linear sliver — the corner-dependence was that fragility, not process. **Removed entirely** (no
   `Icomp`/`ICOMP_DEFAULT`/`_icomp_param`): the resistor is bit-identical in Zout/PSRR/noise (**0.0000 %**
   over 1 Hz–1 GHz) AND in the validated large-signal transient (fit against THIS resistor form; OLD-vs-
   resistor load-step overlap ≤3.4 µV). The unbounded-DC-fight hazard the compliance targeted is already
   killed by FIX 1 (PSRR auto-track); a real current-limit/dropout is out of this LTI core's SCOPE (stage 2b)
   and must be a one-sided, conductance-preserving soft limit — never a symmetric hard saturation.

Verified: focused emit suite green (`harness/test_pvt_robust_emit.py`, rewritten to lock the resistor form +
a no-`Icomp` regression), generator re-emit correct, AC-PSRR transparency Spectre-confirmed (0.0000 %), and
the FF floating-rail runaway test passes on the freshly-emitted card (all rails ~0.80 V, 0 convergence
errors). NOTE: the earlier "+2 V unload overshoot" clamp (`himargin`) was a symptom-fix for the same
co-drive event — REDUNDANT post-auto-track, NOT shipped.

## Checklist
- [x] in-situ DC-robustness root-fix — PSRR supply auto-track + STIFF R_a resistor regulation (the "DC current compliance" was tried & REVERTED: its ~1.9 mV knee caused FF-corner runaway; `test_pvt_robust_emit.py` locks the resistor + a no-`Icomp` regression)
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
