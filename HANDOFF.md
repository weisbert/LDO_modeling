# HANDOFF — LDO behavioral-model builder (as of 2026-06-07)

## UPDATE (2026-06-07c) — TARGET B PHASES 0–3 EXECUTED (ultracode session): pipeline VALIDATED in Spectre
Ran the Phase 0–3 plan from 2026-06-07b on the Linux/Cadence box. **CLI phases 0–2 + skillbridge phase 3
done and validated**, + a SpectreRF PSS RF acceptance, + a company-side Phase-4 runbook. New tooling in
`cadence/` (see `cadence/README.md`).

**PUSHED to GitHub** — branch **`target-b-cadence-bringup`** (off `main`), 4 commits:
`83641eb` Phases 0–3 · `3933fc7` RF PSS acceptance · `fe7dd71` company runbook · (+ this HANDOFF update).
Pull it on the company machine; merge to `main`/open a PR when ready.

**Phase 4 (real PMU LDO in-situ) is the only thing left, and it runs ON THE COMPANY MACHINE** — the real
PMU can't leave that environment, so the user does the *extraction* there following
**`cadence/COMPANY_RUNBOOK.md`** (setup/env-adapt → in-situ pin-level extraction recipe → `import_cadence`
→ `fit_model` → `score`/PSS-HB drop-in). The npz contract is the firewall; fitting is pure Python.
**One open question:** does the 0.8 V LDO also take the 1.8 V supply (PSRR-relevant)? If yes, `fit_model`
needs a 2nd PSRR path (decision 4) — a no-input change I can make now on request.

**Phase 0 — env (DONE).** `cadence/env.sh` mirrors the live Virtuoso IC618 env. **`spectre -64` is
MANDATORY** — default 32-bit mode compiles the Verilog-A CMI with `gcc -m32` and dies on missing
`gnu/stubs-32.h`; 64-bit compiles clean. Spectre 18.1.0.077 confirmed; **"VA compiles in Spectre" CLOSED.**

**Phase 1 — VA round-trip (DONE, composite 0.2 = numerical floor).** Built the Spectre pipeline:
`psf.py` (psfascii reader) → `spectre_run.py` (`spectre -64`) → `spectre_bench.py` (NET/PIN-parameterized
measurement bench, mirrors `harness/bench.py`) → `extract_ref.py` (→ `results/ref/<name>.npz` per
`CADENCE_EXTRACTION.md`) → `bench_spectre.py`+`score_spectre.py` (reuse `harness/score.py` metrics with a
Spectre backend; ngspice isn't on this box so `score.py` can't grade directly). noise `out` is already
V/√Hz (no V²/Hz convert); per-instance noise contribution is captured (Phase-4 bonus).
- **BUG CAUGHT + FIXED — inverted `.va` PSRR sign.** Verilog-A `I(vout)<+ X` removes current FROM vout,
  but the validated SPICE `.lib` mirror (`Gd 0 vout ...`) injects INTO it → emitted PSRR was 180° flipped
  (would invert 304 MHz sideband asymmetry). `emit_va` already negated the *spur* tones for this reason but
  missed the PSRR lines. Fixed in `harness/fit_model.py:emit_va` (negate the 2 PSRR `I(vout)<+`
  contributions) + the committed `model/ldo_model.va` (hand-applied, coefficients untouched → `.lib` pristine).
  Round-trip Pdeg 180→0, composite 5.6→0.2. **The `.va` was never simulated before — Target A only ran the `.lib`.**

**Phase 2 — transistor GT cross-sim (DONE; full 12-architecture matrix).** Bringing ngspice BSIM3 GT into
Spectre needed three fixes in `cadence/spectre_bench.py:spice_dut` (all caught during bring-up):
(1) **BSIM3 level map** ngspice `level=8` → Spectre `level=49` (8 → generic `mos8`, rejects BSIM3 params);
(2) **strip `{param}`** subckt braces (Spectre spice resolves bare names, not `{}`);
(3) instance the subckt in **spice-lang**, stimuli in spectre-lang, nets bridge.
- **Cross-sim is near-perfect:** base composite **3.8 (Spectre) vs 3.9 (ngspice)**; Zout ΔRMS ≤0.006 dB,
  **1/f noise band Δ 0.000 dB** (the flicker-`kf` worry is moot), 304 MHz Zout 0.724 vs 0.724.
- **12-architecture matrix** (Workflow, parallel + adversarial verify vs committed `matrix.md`):
  **10/12 match, 1 minor, 1 deviation.** match: base 3.8/3.9, cout10n 2.4/2.7, cout4n7 2.6/2.7, esr_hi
  4.0/4.1, iq_lo 4.95/5.0, iq_hi 4.06/4.1, wp_big 7.66/7.8, cg_hi 6.33/6.3, v2_capless 6.51/6.8,
  **v4_ffpsrr 3.9/4.0** (non-min-phase PSRR — validates the sign fix). minor: v1_nmos 9.23/8.9 (+0.33,
  benign — underdetermined Cout on a flat NMOS-follower Zout). **deviation: v3_miller 20.6/6.3.**
  - **v3_miller deviation = a pre-existing FITTER bifurcation, NOT a Cadence-pipeline defect.** Diagnosed:
    GT physics is cross-sim identical (Zout/PSRR agree to ~0.005 dB); fitting the *ngspice* npz reproduces
    the good committed fit, but fitting the *Spectre* npz (identical to 0.005 dB) lands in a different
    least-squares basin (`G1` const +0.029 → −154; huge per-corner G1/w1) that fits 121 µ but whose
    quadratic-in-ln(iload) interpolation blows the off-corner PSRR band error to 15.94 dB at 20 µ
    (Pband weight 2 → composite). Same "low analytic residual but bad realized band" trap documented for
    v4 250 µ in 2026-06-06c. v3 PSRR fit is ill-conditioned (migrating multipole + non-min-phase + the only
    non-passive GT). The fit is deterministic; the sub-0.01 dB cross-sim noise just tips the bifurcation.
  - **v5_spur / v6_spur2 NOT in the matrix** — they have intrinsic on-chip tones; a Spectre intrinsic-spur
    characterizer (PSS or free-run transient-FFT) isn't built yet (the empty-spur extraction is correct for
    the other 12). This is where SpectreRF PSS should be wired (Phase 3+).

**Phase 3 — skillbridge productionization (DONE).** Live Virtuoso IC618 over skillbridge.
- Created lib **`LDO_model_lab`** (`ddCreateLib`, self-contained at `cadence/cds/`) and imported
  `model/ldo_model.va` as a **veriloga cellview** `LDO_model_lab/ldo_model/veriloga` (recipe = the box's
  own dreg_gen: write `veriloga.va` + `master.tag`, `ddUpdateLibList`). Verified it's a valid simulatable
  Cadence cell (characterizing it reproduces the Phase-1 round-trip; PSRR sign correct).
- Reusable drivers: `cadence/skill_lib.py` (`ensure_lib` / `import_va_cellview` / `list_cells`) + its
  CIW twin `cadence/skill/ldo_cellview.il` (`ldoEnsureLib` / `ldoImportVA`, validated: loads + runs).
- **`cadence/import_cadence.py`** — the contract converter: ADE/OCEAN PSF tree OR manual CSV →
  `results/ref/<name>.npz` (`assemble()` = single schema writer). **CSV manual-TB fallback verified
  end-to-end** (array-equality vs source npz + fit-compatible) → the user's manual-TB question answered: yes.
- **OCEAN caveat:** the standalone `ocean` binary is OS-broken here (`sysname` → "unknown" on RHEL8/4.18,
  fatal; live Virtuoso tolerates it). So characterization runs via the validated spectre path (same
  SPECTRE181 engine ADE uses); for Phase 4 in-situ, run the contract analyses in the user's ADE session
  and feed the PSF/CSV through `import_cadence.py`. In-session OCEAN-via-skillbridge was avoided (would
  hijack the user's live ADE/OCEAN globals).

**RF acceptance (SpectreRF PSS @ 304 MHz, `cadence/rf_accept.py`, model vs GT — bonus).**
PSS runs locally (no license gap). (1) **Linear carrier PSRR matches GT to ~0.5 dB** (57.7 vs 57.2 dB),
amplitude-independent → the model is accurate for the linear supply-spur transfer (PSRR-sign fix matters).
(2) **LTI model does NOT capture nonlinear upconversion** — GT 2f scales square-law (−49→−14 dBc over
2→100 mV), model stays at floor; by design (spurs ride the linear PSRR path). (3) **Speedup is
system/convergence-scale, not standalone** — the model's ~18 nodes + VA overhead make toy-GT PSS slower
(0.61 vs 0.17 s); the win is a complex real LDO in a large system (Phase 4 measures it). See
memory `rf-pss-acceptance-findings`.

**OPEN ITEMS (recommended follow-ups, none blocking the validated pipeline):**
- Regenerate ALL `model/*.va` from the fixed `emit_va` (esp. v4_ffpsrr — non-min-phase PSRR, RF-relevant).
  Only `model/ldo_model.va` was hand-fixed; the others still carry the old PSRR sign. (Churns committed files
  with cosmetic re-fit noise on the `.lib`s — user's call.)
- Harden the v3-class PSRR fit (regularize / prefer-smaller-|G|-and-w-spread tie-break that generalizes
  across corners, or pin the band realization) — a CORE `fit_model.py` change → re-validate the matrix.
- Build the Spectre intrinsic-spur characterizer (PSS/transient-FFT) → enables v5/v6 + the real 304 MHz use case.
- Phase 3 (skillbridge/ADE/OCEAN) and Phase 4 (in-situ PMU LDO) still need the 5 open inputs below.

**Uncommitted tracked changes:** `harness/fit_model.py` (emit_va PSRR sign), `model/ldo_model.va` (sign),
`.gitignore` (+Spectre scratch), `HANDOFF.md`. New: `cadence/` (tooling+README). Spectre validation
artifacts (`model/*_spectre.*`, `results/ref/*_spectre.npz`, `cadence/work*`) are .gitignored.

## UPDATE (2026-06-07b) — LINUX/CADENCE BRING-UP: Target B decisions + execution plan (NOT yet executed)
Planning session on the Linux/Cadence box. **Nothing executed yet** — the user will run Phase 0+ in the
NEXT conversation under **ultracode (Workflow multi-agent orchestration)**. This section IS the handoff.
On-disk state: pulled to `fc8fadc`, working tree clean.

**Environment VERIFIED on this box (facts, not assumptions):**
- **Virtuoso IC618** running; **skillbridge 1.8.0** live and drivable from `python3` (`Workspace.open()`
  connects, `hiGetCurrentWindow()` works). Server: `workarea/skill_tools/skillbridge/python_server.py`.
- **Spectre 18.1** (`SPECTRE181`) on disk at `/home/yusheng/Program/eda/cadence/SPECTRE181` → we have
  REAL `pac/pxf/pnoise/pss` LOCALLY. ⇒ the prior handoff's **Xyce / VACASK / OpenVAF workarounds are
  NOT needed** — validate 304 MHz sidebands with **SpectreRF directly**.
- `model/ldo_model.va` is clean `module ldo_model(vin,vout)`, standard VAMS, **no `laplace_nd`** → should
  compile in Spectre (the one untested item; Phase 1 closes it via `ahdl`/VA compiler).
- Reusable SKILL idioms: `workarea/skill_tools/` (`skill_tools.il`, `dreg_gen`, `mytool`, `PLAN.md`).

**DECISIONS made this session:**
1. **Test DUTs (no real LDO on this box) = reuse existing assets as self-validating DUTs, do BOTH, in order:**
   - **Stage A — VA round-trip:** instantiate emitted `model/ldo_model.va` as the DUT in Spectre. Proves
     VA compiles + builds the characterize→import→fit pipeline against a KNOWN answer (≈ identity refit).
   - **Stage B — transistor GT cross-sim:** bring `ground_truth/ldo_gt.lib` (BSIM3 on `models/*.mod`) into
     Spectre via `simulator lang=spice`; characterize; cross-check vs the trusted ngspice `gt_ref.npz`.
     **Watch:** BSIM3 level mapping (ngspice level 8 ↔ Spectre `bsim3v3`) + flicker `kf`.
   - Do NOT design a new LDO (no extra coverage, pure risk).
2. **Integration style = HYBRID:** validate VA-compile + characterization via **spectre CLI on netlists** first
   (fast de-risk), THEN wrap into the **skillbridge → ADE/OCEAN** production "SKILL side".
3. **Real PMU LDO = IN-SITU, reconciled with `CADENCE_EXTRACTION.md`:**
   - User will NOT pull the LDO out standalone (bias/VREF/IBP hard to recreate; normal sim IS the full PMU top).
   - But the contract demands ideal-vin DECOUPLED extraction. **RECONCILIATION = "in-situ OP + pin-level
     decoupled extraction":** LDO stays fully wired in the PMU top (bias/VREF/IBP/enables all real). Apply the
     contract stimuli at the LDO's OWN pins — 1 A AC into `vout` pin (Zout), 1 V AC on supply pin (PSRR),
     `pnoise` at `vout` pin. **Idealize ONLY the supply pin's AC:** read the real rail level from the in-situ
     OP, place an ideal DC source (= AC ground) at the supply pin during the Zout/noise AC runs. **Noise
     attribution** = Spectre **per-instance noise contribution** summed over the LDO instance only (=
     contract "intrinsic LDO noise"); external IBP/bias noise rides the optional `ibp_xfer_*` port.
   - **Drop-in acceptance:** swap the LDO instance for the model in the full top; aggressors (other blocks'
     switching, supply ripple) are already there ⇒ spurs arrive via Zout/PSRR by construction. Intrinsic
     `spur_F` likely EMPTY for a plain LDO.
4. **Multi-supply:** this LDO has **1.05 V + 1.8 V** supplies → extend the single-vin contract with a 2nd
   PSRR path: extract `p_*` from each supply pin → two PSRR blocks in the model (additive extension).
5. **Extraction mechanism is DECOUPLED from fitting** (the contract is the firewall). **Manual-TB fallback is
   first-class:** a hand-built ADE TB emitting the contract arrays feeds `fit_model.py` identically. Build
   `import_cadence.py` to accept **ADE manual CSV exports**. Recommended real-chip path: hand-build & verify
   the in-situ recipe ONCE → OCEAN-sweep the 3 corners → skillbridge orchestrates (ADE state → OCEAN → bridge).

**EXECUTION PLAN (phases):**
- **Phase 0:** source SPECTRE181 env; confirm `spectre` + VA compiler; reusable env script.
- **Phase 1 (VA round-trip, CLI):** TB netlist instantiates `ldo_model.va`; run `ac` (1A→vout) / `ac`|`pxf`
  (vin) / `noise` per contract; write **`import_cadence.py`** (Spectre psfascii/CSV → `results/ref/<name>.npz`
  per `CADENCE_EXTRACTION.md`); `fit_model` + `score` → expect tiny composite. CLOSES "VA compiles in Spectre".
- **Phase 2 (transistor GT cross-sim, CLI):** same TB, DUT = `ldo_gt` via `simulator lang=spice`; 3 corners;
  compare Spectre arrays vs ngspice `gt_ref.npz`; fit→emit→score; tabulate deltas.
- **Phase 3 (skillbridge productionization):** `ddCreateLib` (lib name TBD, default `LDO_model_lab`); DUTs as
  cellviews (veriloga + spice text); OCEAN `ac/noise/dc/pss` via skillbridge; `import_cadence` reads ADE PSF;
  package as reusable `.il` + py driver in `skill_tools` style ("point at a cellview → get the npz").
- **Phase 4 (later):** real PMU LDO — in-situ pin-level extraction (decision 3), multi-supply (decision 4),
  drop-in PSS/HB acceptance + speedup measurement.
- **Build the characterization NET/PIN-parameterized from the start** (generalize `bench.py`'s 2-port measure
  to "which net is vout / which is supply / where to inject") so Phase 1/2 bench work transfers to Phase 4 in-situ.

**OPEN INPUTS still needed from the user** (the 3 the contract names + 2 extras):
- **Nominal vin** (model hardcodes 1.05 V as PSRR ref `Vrf`) — confirm PMU LDO supply = 1.05 V; + role of the 1.8 V supply.
- **The 3 load corners** (low/nom/high) bracketing the real operating load, + which is nominal (Target A used 121 µA).
- **Design Cout / ESR** of the 0.8 V output.
- **What else sits on the 0.8 V output net** (external decap? other loads?) — sets the Zout/noise boundary (defect-6).
- **Cadence lib name / location** for Phase 3.

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
