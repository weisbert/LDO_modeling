# HANDOFF — LDO behavioral-model builder (as of 2026-06-11, end of day)

## NEXT SESSION = trans-ID Verilog-A probe ON THE REAL PART (user-requested experiment)

**Goal:** test on the real 5.8G LDO whether ONE multitone transient per corner really replaces
the AC Zout/PSRR exports ("是不是真的效果不错") — the R5 idea, already PROVEN on 4 synthetic
parts (Zout ≤0.45dB, PSRR ≤1.6dB RF; Level-2 dComposite base +0.06 / v1 −0.68 / v3 +2.18 /
v2 +2.60) and through a compiled-VA fixture (d_path 0.04). See memories
`finding-trans-id-validation` / `finding-trans-va-pipeline`, results/trans_id/.

**What exists (all on main):** `harness/trans_id.py` — `plan_band`/`emit_stim_va` emit a
band-split multitone STIMULUS `.va` (+ sidecar JSON plan + README; defaults VDD=1.05,
va=0.5mV supply tones, ib=1µA vout current tones, 12 tones/dec, IM-de-aliased mod-3 grid,
half-amp linearity gate ≤0.15dB); `harness/trans_import.py` + **GUI tab 5** import the
(t, vin, vout) waveform back into a fit-ready npz.

**Red-zone recipe (everything runs ON the box — no air-gap problem):**
1. Emit the stimulus `.va` for the part's corners/bands (GUI tab 5 or trans_id CLI); set
   VDD to the real supply.
2. Virtuoso: compile the stimulus (plain VAMS), instantiate it with the TRANSISTOR-LEVEL
   LDO at each load corner, run ONE `.tran` per band per corner with the sidecar's exact
   dt/tstop (coherent windows — do not round them).
3. Export (t, vin, vout) CSV per band → GUI tab 5 import → Fit → Compare. The GO/NO-GO
   judgment = diff this fit's report against the AC-import fit (composite 1.81 @76c630d).
4. Paste both reports back across the gap for the yellow-zone post-mortem.

**Decisions/caveats for next session:**
- **Band ceiling**: synthetic validation ran in-band (≤100MHz). The real part's AC went to
  40GHz; a GHz multitone tran needs ps-scale dt = EXPENSIVE. Sensible first experiment:
  trans-ID the ≤0.5–1GHz band (where the loop lives) and keep the wideband AC for the
  C_ft/GHz tail (hybrid recipe) — fit_cft needs the HF tail, it will NOT fire from a
  low-band trans alone.
- trans-ID recovers Zout+PSRR+DC only — **noise still needs `.noise`, spurs still need the
  spectrum export**; the trans npz must be merged with those (import side handles it).
- Known limitation (R7, negative result — do NOT re-attempt a fitter fix): multi-pole PSRR
  grid sensitivity (+2.2-class on v3). The real part is single-loop + C_ft (base-class
  shape) — expect good Zout, WATCH the PSRR band/phase terms in the compare.
- Run the linearity gate at the lightest corner (100µ) before trusting amplitudes.
- GUI launch gotcha: user runs `./run_gui` in a desktop terminal (agent can't pop X11).

**Red-zone actions queued for the next package update (independent of the experiment):**
build `.\deploy\package.ps1 -Mode incremental` → `./update` on the box → **re-create the
Cadence symbol (now 3 pins: vin/vout/gnd)** → re-Emit on the box (single-file `.va`: inline
PWL dropout, no .tbl dependency, vdd/voutdc instance params) → paste the new report (its
[7] digest now carries `# dcblock` DC curves → the local replica's DC turns real).

## UPDATE (2026-06-11c) — R2 + R3-L1 SHIPPED: gnd port + settable vdd/voutdc on the emitted model

User confirmed the deferred interface gaps were real ("the .va still has only vin/vout, and I
can't set Vout's DC") — both were R2/R3 in DEFERRED_REFACTORS.md, deferral condition (Target-B
modeled) met this morning. Shipped in one pass (user chose **R3 = L1 only**; L2 = vin×iload
small-signal axis stays OPEN pending need):
- **R2**: `.va` = `module ldo_model(vin, vout, gnd)`, `.lib` = `.subckt ldo_model vin vout gnd`;
  ALL internal references re-railed (zero global-ground access — floating-gnd test: lift gnd
  −0.317V, Zout delta 0.00dB). Benches auto-detect DUT port count (`bench.xline`) so 2-port GT
  and 3-port model share one bench. **Red zone must RE-CREATE the Cadence symbol (3 pins).**
- **R3-L1**: instance params `vdd` (PSRR DC reference + Vout-DC tracking along the previously
  UNUSED `dc_linereg` curve, poly deg≤4 clamped to its span) + `voutdc` (>0 pins Vout DC at the
  instance iload). Defaults reproduce the characterized DC exactly.
- **Validated**: `harness/validate_r2r3.py` (NEW, 4 gates) PASS — float-gnd 0.00dB / vdd
  tracking ≤3.7mV incl. the 0.9V dropout knee / voutdc pin exact / default DC exact; matrix
  **19/19 value-identical**; validate_capless PASS (one stale text assertion updated);
  GUI selftest PASS; OpenVAF-compiled 3-port `.va` == `.lib` to **0.0000dB/0.0000°** (OSDI AC).
- Still open in the R-batch: R1 (profile-driven step sizes), R3-L2, R8 ($table_model).

**Follow-up (same day): slew_en=1 table-path bug + digest now carries DC curves.**
- User hit `$table_model error: Open data table file 'myldo_dropout.tbl' failed` in ADE:
  emit_va wrote a BARE filename, which the simulator resolves against its RUN DIR. Fixed:
  the `.va` now embeds the **emit-time ABSOLUTE path** (red zone emits on the red box ->
  path valid there; re-emit or edit one line if the table moves). A `parameter string`
  override was tried first and REVERTED — it **crashes OpenVAF** (rc=101 internal panic)
  and is unverified on the red-zone's non-Spectre simulator. Workaround for an already-
  emitted model: copy the .tbl next to input.scs or hand-edit the path.
- User confirmed the RED-ZONE myldo.npz has REAL dc_loadreg/dropout (the "synthesized DC"
  caveat applies only to the LOCAL digest replica). To close that gap, report [7] now
  appends `# dcblock dc_loadreg/dc_linereg/dc_dropout` (≤64 rows each) and digest_import
  parses them (real curves win, synthesis = fallback for old digests; sufficiency INFO
  says which). Roundtrip base: DC curves recovered to ≤0.0005mV.
- slew_en guidance (user asked): **0 for PSS/HB** (linear validated core, convergence);
  **1 only for large-signal transient/dropout studies** — legitimate on the red box (real
  DC data), meaningless on the local replica until a dcblock-carrying digest arrives.
- Re-validated: matrix 19/19 value-identical, GUI selftest PASS, validate_capless PASS,
  OSDI AC == .lib 0.0000dB/0.0000° on the literal-path 3-port `.va`.

**Follow-up 2 (same day): `$table_model` ELIMINATED — dropout PWL now INLINE (R8 CLOSED).**
User asked why the table was a separate file at all (it isn't big) — no hard reason, it was
just Spectre's built-in. `emit_va` now emits the dropout curve as a closed-form sum-of-max
PWL expression (== 1-D linear interpolation, linear end extrapolation): single-file `.va`
deliverable (the table-path bug class is gone for good), pure-VAMS portable, and the FULL
`.va` now OpenVAF-compiles — slew_en=1 locally verified for the first time (OSDI DC sweep
1µA–5.9mA vs the `.lib` pwl: max 0.012 mV). `.tbl` still written but as a data record only.
GOTCHA recorded: openvaf's linker drops an import-lib `<name>.lib` next to its output — it
CLOBBERED `model/ldo_model.lib` once during validation (recovered by re-emit; compile COPIES
in a scratch dir, never in `model/`).

> **Deferred refactors:** see `DEFERRED_REFACTORS.md` (do as one batch AFTER the current
> Target-B LDO is modeled). Open: **R1** de-hardcode `trans_big`/`trans_slew` + nominal corner
> (profile-driven); **R2** emitted `.va`/`.lib` has no GND terminal; **R3** VDD hardcoded —
> not settable/sweepable for HV/nom/LV supply corners (`dc_linereg` characterized but unused).
> R3 has an OPEN QUESTION for the user (small-signal accuracy needed at off-nominal VDD?).
> Design-level concerns also recorded: **R4** the feedback loop never tests the real use case
> (LDO + buffer-at-carrier, model vs real — only block metrics + an 8MHz sanity gate exist);
> **R5** automate the ~30 manual characterization exports; **R6** real-LDO quality bugs (poor
> fit / output rail droops / no buffer ripple — tied to R3 DC + Zout-at-carrier coverage).

## UPDATE (2026-06-11b) — RED-ZONE VERIFICATION **PASSED**: Target B small-signal CLOSED

The user applied build **76c630d** on the red box and returned the report: **composite
1.81** (expected ~2–3), header shows `C_ft: 174.4fF` + `noise: HYBRID series bank 4
sections` ✓, Zout sign auto-correct fired ✓, diagnosis = "All analytic metrics within
tolerance" — Zout 0.5dB/2.4°, PSRR 0.38dB/2.2°, noise 0.4dB at every corner. **The round-6
loop (digest→replica→fit→one package) is validated end-to-end; Target B's analytic blocks
(Zout/PSRR/noise) are DONE.**

Round-7 digest re-imported (51 pts/corner — the peak-densified export worked through the
loop; sufficiency screen 0 warnings): replica TOTAL **2.01** vs red 1.81 (digest loss, same
dominant term pband). One flag, BOTH sides: the hybrid bank's 4th section is dead fat —
red fit put it at 49.4GHz (beyond the 6.3GHz noise-data top, bound-riding), replica at
2.63MHz and the new noise-pole ident marks it INVISIBLE (+ sn4). Harmless (npsd 0.4dB);
a fit change (M=3 cap or prune-invisible) is NOT worth re-opening emit for now. Structure
LOCO: stable on every fold of the round-7 data.

**NEXT (the real frontier is now system-level):** real-part spurs/transient exports (the
digest has synthesized DC + no transient → `score.py --variant myldo_digest` still crashes
at `trans_lin_100u`, known); GHz systest against the real 5.8G carrier; then the R1–R6
deferred batch (VDD corners = R3 first). The composite is BLIND to HF on digest refs (no
`*_hf` arrays) — the in-band z already reaches 40GHz there, but never skip systest.

## UPDATE (2026-06-11) — METHODOLOGY REVIEW ROUND: composite RE-BASELINED (P0) + structure gates (P1)

A fresh 3-track methodology review (fitter overfit risks / validation blind spots / verified
in-code) found the remaining gaps were **phase-blind regression gates** and **in-sample-only
structure selection**. Six fixes shipped (P0 = scoring, P1 = guardrails). **The composite
DEFINITION changed → matrix.md is a NEW BASELINE** (old vs new below, every delta attributed).

**P0 — scoring (score.py + report.py + run_matrix.py + spur_char.py):**
1. **Discrete-spur PHASE now scored** (`W["spurph"]=0.03`/deg, mean|phase err| of matched
   tones; `spur_worst_ph` matrix column). HB sidebands superpose coherently — the round-6
   `.va` −H sign bug (180°, invisible to magnitude gates) is the realized failure mode.
2. **Phase errors WRAPPED to ±180°** (`np.angle(Zmi/Zg)`; score.py:113/123 used raw
   principal-value subtraction). Fired on exactly one variant: v10_3lc pphase_max 71.5→57.6°
   (its true error was being inflated across the ±180 boundary). All others byte-equal.
3. **HF extension terms `zhf`/`phf`** (`W=0.5` each): model re-measured with the wideband AC
   sweep vs the stored `z/p_*_hf` arrays, scored ABOVE the in-band top only (no double count);
   gated on the ref having `*_hf` (digest refs don't → **replica composite 2.30 UNCHANGED**,
   the air-gap loop stays comparable). report.py mirrors both terms analytically.

**Matrix re-baseline (run_matrix --reuse, all 19):** every composite delta = 0.5·(zhf+phf)
to 3 decimals (+ v10's −0.27 wrap correction). A-layer/base variants +0.1–0.5 (real HF
extrapolation error, mostly PSRR plateau); **v7_esl 4.2→16.7, v8_dlc 5.6→18.1, v10_3lc
57→71** — the documented "composite is BLIND to HF" variants now show their tails/notches
IN the composite (these are model floors pointing to the known series-L extension, not fit
bugs); v1_nmos 8.9→10.6 (the documented high-ESR Cout floor, now visible at HF too).

**P1 — guardrails (crossval.py + digest_import.py + report.py):**
4. **Structure-stability LOCO** (`crossval.structloco`, gate #4, `--no-struct` to skip):
   re-runs the WHOLE structure-selection pipeline (C_ft gate, Cout/ghost-cap, Zout branch-B,
   PSRR shelf/SK/complex selector, noise topology+adaptive bank, spur table) on each N-1
   corner subset; any decision that FLIPS sat on its in-sample threshold = data-noise.
   Full `crossval --all`: **17/19 STABLE** incl. **myldo_digest (real part: C_ft + hybrid
   reproduce on every 2-corner fold)**. **2 FAILs = real findings: v1_nmos + v2_capless flip
   the PSRR shelf↔complex selection on the nominal-held-out fold** — both sit at pphase
   2–3° ≈ `SHELF_PH_TRIG=2.5°` (the review's #1-flagged magic threshold), v1 cascading from
   a 3.1x Cout shift when the `*_hf` sweep is lost. Known-floor variants, flagged not fixed
   (a selector retune touches fit/emit -> separate round). crossval_matrix.md now has a
   `struct` column.
5. **Identifiability extended** (round-6 deferred item CLOSED): hybrid `snw/sn*` keys (found
   real-part `sn1` INVISIBLE, ratio 1.3e11), shared noise POLE positions (stacked-corner
   Jacobian; found `f6@76MHz` invisible on base, `f1@220Hz` on the real part — greedy-fit
   artifacts, flagged not failed), spur `sa_k` SWITCH guard.
6. **Digest sufficiency screen** (`digest_import.py check_sufficiency`): WARNs on <4 pts/dec,
   under-resolved |Z| resonance (<3 pts above half-power), band-edge peaks, missing/LF-blind
   noise data; always INFOs that DC curves are SYNTHESIZED. Export side: report.py [7] digest
   now DENSIFIES ~12 extra points around each corner's |Z| peak (current digest: 0 warnings).

**Validated:** fit_model --selftest PASS · GUI offscreen selftest PASS · systest base PASS
(numbers == Bcover) · replica report TOTAL 2.30 byte-stable · crossval base/v5/digest run
clean (LOCO/off-grid FAILs are the pre-existing known few-corner gaps, unchanged).
**Emitted .lib/.va untouched — models are bit-identical; only scoring/validation changed.**

## UPDATE (2026-06-10) — TARGET B ENGAGED: real 5.8G capless LDO, composite 268 → 2.3 (replica)

**State: VERIFIED 2026-06-11 (see UPDATE 2026-06-11b above) — composite 1.81 on the real
box with build 76c630d, C_ft + HYBRID lines present. This section kept for the round-6
history.**

**The air-gap iteration loop (now fully tooled — this is THE workflow for all future rounds):**
1. Red zone: GUI Compare → "Save text report" → user pastes the report (build fingerprint in
   the header tells you which code produced it; section **[7] GT DIGEST** carries log-resampled
   real curves as text).
2. Yellow zone: `python harness/digest_import.py results/ref/myldo_digest.txt` rebuilds
   `results/ref/myldo_digest.npz`; iterate locally with
   `python harness/report.py --variant myldo_digest` (composite 2.30 now); regression =
   `harness/validate_capless.py` (8 parts) + matrix/crossval/systest + GUI offscreen selftest.
3. Ship ONE validated incremental package (`.\deploy\package.ps1 -Mode incremental`); user
   verifies the build sha printed by `bash update`.

**Six rounds of findings on the real part (full story: memory `finding-target-b-first-contact`
+ `finding-target-b-round6`):**
- R1 ghost-cap: 14nF "extracted" vs 681Ω peak @10MHz = capless part; ghost-cap gate + envelope
  fallback (`b6a14b7`), z_hf-vs-z guardrail. (No z_hf existed — z swept to 40GHz directly.)
- R2 Zout sign: phase uniformly ~180° off = testbench V/I inverted; import auto-negates when
  LF Re(Z)<0 (`ba080ae`). After this, Zout block closed: 0.5dB/2.3°.
- R3-R5: noise −20dB@1kHz root-caused as STRUCTURAL (In=Sv/|Z| falls −34dB/dec, beyond any
  Lorentzian sum); grid equalization + adaptive bank (`8a9d453`) shipped but insufficient;
  build fingerprint (`da46639`) + GT digest channel (`fff0d7a`, `85e19eb`) shipped.
- R6 (`e0bc231`, agent-flow: 2 analysts ∥ → 2 builds → adversarial review ∥ full regression →
  fix): **C_ft=174.4fF vin→vout feedthrough** (gated; pband 4-5→0.4dB, GHz PSRR plateau,
  shelf degeneracy all fixed) + **hybrid series-noise** (voltage bank at branch-A rail node
  `vrgn` + Norton white floor; gated by Norton-fail>4dB AND win>0.5dB; npsd 7.9→0.4-0.8dB,
  ngspice-verified) + **emit_va PSRR sign fix** (PRE-EXISTING: compiled .va realized −H, 180°
  phase, invisible to magnitude checks; OSDI-verified fixed) + **ghost-gate adjudication**
  (v10_3lc 57.2→160.8 regression from R1 found & fixed: fit both C candidates, keep winner).
- Deferred minors from the adversarial review: crossval identifiability blind to hybrid
  sn-keys; fit_cft flat extrapolation when p-grid exceeds z-grid; .lib/.va chain sign comment.

**After red-zone verification, the frontier is system-level:** real-part spurs/transient
exports (the digest has synthesized DC + no transient → `score.py --variant myldo_digest`
crashes at `trans_lin_100u`, known); GHz systest against the real 5.8G carrier; then the
R1-R6 deferred batch. The composite is BLIND to HF features — never skip systest/z_hf gates.

## UPDATE (2026-06-08b) — 黄区→红区 deploy VALIDATED end-to-end + one-command update workflow
The GUI modeler + airgap bundle is now **proven on the real red zone** (EDA box, CentOS7-class,
**tcsh**, airgapped) at `/data/RFIC3/Hi1108V100_Pilot_C1Xplus/w84368867/workarea/LDO_modeling`:
`GUI selftest PASS` on the box (analytic core import→fit→predict→emit + Qt render). A chain of
cross-platform/EDA issues was found & fixed — all on `main`, commits **374ec63..42cb7cc**. Full
zh ops flow: **`deploy/部署与更新流程.md`**; gotchas: memory `reference-powershell-gotchas`.

**Fixes shipped this session (each pushed):**
- `deploy/package.ps1` — 黄区 one-command packager (wrapper over `package.py`): auto-find Python 3.11,
  PyPI preflight, full/incremental. Saved **UTF-8 BOM** (PS 5.1 zh-CN parse). PS 5.1 strips embedded
  `"` to native exes → version probe is quote-free (`print(maj*100+min)`).
- `package.py` text artifacts now **LF** (`newline="\n"`): sidecar/lock/MANIFEST were CRLF →
  `sha256sum -c` failed (filename+`\r`). MANIFEST checksum keys now **`.as_posix()`** (were
  `str(WindowsPath)`=backslash → bootstrap integrity read ALL files MISSING on Linux); bootstrap also
  tolerates `\` keys. `.gitattributes` forces LF on `*.sh`, `requirements*.txt`, `deploy/{run_gui,update}`.
- Install = **self-contained under one user folder; PREFIX = the folder itself** (`bash
  bundle/bootstrap.sh "$PWD"`), flat (`.venv app wheels results model` directly in it, no `install/`).
  Use shell **`$PWD`, NOT `ROOT=`** (red box is tcsh → `VAR=val` errors; EDA already exports `$ROOT`).
  `/opt` is unwritable on the shared box.
- **Qt isolation (last hurdle):** Cadence/Virtuoso put a conflicting `libQt5Core.so.5` on
  `$LD_LIBRARY_PATH` (`/software/public/qt/5.15.3_xcb/lib`) → PyQt5 import dies
  `symbol _ZdaPvm, version Qt_5 not defined`. Fix = prepend the wheel's `PyQt5/Qt5/lib`. `bootstrap.sh`
  + `update.sh` do it before the smoke; bootstrap writes a `run_gui` that does it for everyday launch.
- **One-command update:** `deploy/run_gui` + `deploy/update` are standalone executable launchers
  (single source of truth; bootstrap copies them to PREFIX root, update.sh refreshes them there).
  Red-box update = drop `ldo_modeler_incremental.tar.gz` in the folder → `./update`.
- Docs rewritten to the $PWD/flat/run_gui flow: `deploy/操作手册_OPERATIONS.md`, `deploy/README.md`,
  NEW `deploy/部署与更新流程.md`.

**State of the real red-box install:** it was a **MANUAL** install (the transferred bundle predated
the LF/posix fixes, so bootstrap's integrity gate tripped; used manual `cp`+venv+offline-pip instead).
Works (selftest PASS via Qt isolation). Launchers were hand-copied to the root
(`cp app/deploy/{run_gui,update} . && chmod +x ...`). For a pristine state later: rebuild a fresh
FULL on 黄区 (now all-fixed) and re-bootstrap — optional; current install is functional.

**Deploy/update — turnkey for FUTURE bundles:**
- 黄区: `git pull` → `.\deploy\package.ps1` (full) | `-Mode incremental`.
- 红区 first: `sha256sum -c …`, `mkdir -p bundle && tar xzf …_full.tar.gz -C bundle`,
  `bash bundle/bootstrap.sh "$PWD"` → `./run_gui` (needs X11/VNC).
- 红区 update: drop the incremental tar → `./update`.

**NEXT = Target B (unchanged — the real frontier).** Deployment was the enabler; the tool is now live
where real designs are. Feed a real Cadence LDO's Spectre extraction (`CADENCE_EXTRACTION.md`) →
`cadence/import_cadence.py` → Fit → Compare → Emit `.lib`/`.va`. See `next-zout-psrr-phase` Task 4.

## UPDATE (2026-06-08) — GUI modeler + offline airgap deploy BUILT, reworked for usability, reviewed
Built the whole **manual-TB → modeler** product from `GUI_DEPLOY_PLAN.md` (all 5 phases), then
reworked it for usability after user feedback. Full detail: **`GUI_DEPLOY_BUILD.md`**; ops runbook:
**`deploy/操作手册_OPERATIONS.md`** (中文) / `deploy/README.md` (EN); memory `finding-gui-deploy-build`.

**What exists now (all NEW unless noted):**
- `harness/fit_model.py` (refactor, **zero numerical change**): `predict(P_il,f)` analytic Zout/PSRR/noise
  (== the fitter), `FitResult` + `fit_variant()` in-process entry, de-hardcoded `121u`→`NOMINAL`,
  `VREF` param, `--selftest`. `harness/ng.py`: canonical `ng.amps()` (corner-key→amps, p/n/u/m/k) used
  at all 6 sites (was `float(il.replace("u","e-6"))`, crashed on mA corners).
- `cadence/import_cadence.py`: Cadence CSV/PSF-ASCII → `results/ref/<name>.npz` (mirrors
  `CADENCE_EXTRACTION.md`); complex auto-detect; `validate()` guardrails; `match_dir()` folder-matcher.
- `gui/ldo_modeler.py`: PyQt5 4-tab (Profile/Import/Fit/Compare) over a Qt-free `ModelerCore`;
  analytic `predict` overlay; **self-contained `--selftest`** (synthesizes a ref when none present, and
  now CLICKS every button handler with dialogs stubbed).
- `deploy/`: `audit_wheels.py` (glibc-2.17 gate), `package.py` (full/incremental bundler),
  `bootstrap.sh`/`update.sh` (red install/update), `dryrun_manylinux2014.sh`, `requirements-gui.txt`.

**Validated (as of this handoff):** matrix gate **0.00 composite delta** on all 14 variants (byte-identical
`.lib`); GUI `--selftest --require-qt` **PASS**; wheel **AUDIT PASS 15/15 ≤ glibc 2.17** (auditor also
rejects 2.28/musl/wrong-arch); full(146 MB)+incremental(92 KB) bundles build; red-box smoke is
self-contained. **Two adversarial review rounds** (multi-agent, each finding verified): 13 + 6 = 19
findings fixed (critical GUI picker-wipe, mA-corner crash, MEAS_HINTS click-crash, nominal-change grid
desync, `--ref` widget desync, incremental req-hash guard, update.sh user-data persistence, MANIFEST
integrity check, emit DUT-desync, Fit re-entrancy/missing-data guards, importer fmt/guardrail hardening).

**Open items / gotchas:**
- **NOT run locally:** the Docker `manylinux2014` dry-run (no Docker on the Win box) — script provided;
  the audit already proves the offline install is glibc-2.17-valid. Rehearse with
  `deploy/dryrun_manylinux2014.sh dist/ldo_modeler_full.tar.gz` where Docker exists.
- **`dist/` freshness:** FRESH FULL rebuilt 2026-06-08 for the 红区 first deploy (git SHA `204ade9`,
  req-hash `832a726`, AUDIT PASS 15/15 ≤ glibc 2.17, sha256 sidecar verified, 49-file MANIFEST). It
  supersedes the old stale full + incremental — `ldo_modeler_full.tar.gz` (145.9 MB) is what ships to
  red. Re-run `package.py full` only if `deploy/requirements-gui.txt` changes; `incremental` for
  code-only updates after the first bootstrap.
- **PyQt5 5.15.10** was pip-installed into the dev `.venv` for offscreen Qt validation (not in
  `requirements.txt`; it IS in `deploy/requirements-gui.txt` for the red zone).
- **Tracked `.va` files show as modified** — cosmetic float-format only (e.g. `121e-6`→`1.210000e-04`,
  numerically identical; `.va` is not scored). Safe to commit or leave.
- **Uncommitted:** everything above is unstaged (user's call to commit). `results/ref/myldo.npz`,
  `dutA/dutB/probe*.npz` are user/legacy scratch (not the 15 tracked refs) — left in place.
- GUI **Emit** writes to `model/<npz-stem>.{va,lib,_dropout.tbl}` AFTER a successful **Fit** (Emit is
  disabled until then); a popup now shows the full path + "Open folder".

**Run it:**
```
python gui/ldo_modeler.py --ref results/ref/v5_spur.npz     # GUI (Fit -> Emit -> Compare)
QT_QPA_PLATFORM=offscreen python gui/ldo_modeler.py --selftest --require-qt   # headless gate
python deploy/package.py full --out dist/                   # build airgap bundle (yellow zone)
./deploy/bootstrap.sh /opt/ldo_modeler                      # red-zone install (then update.sh)
PYTHONPATH=harness python harness/run_matrix.py --reuse     # regression matrix (needs ngspice)
```

**NEXT = Target B (the real Cadence LDO)** — the sole remaining modeling frontier; GUI + `import_cadence`
are ready to consume real Spectre exports the moment they arrive. See the 2026-06-07 section below +
memory `next-zout-psrr-phase` (Task 4) and branch `target-b-cadence-bringup`.

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
