# Methodology-audit remediation — HANDOFF

Tracks the implementation of `REVIEW_methodology_audit.md` (the read-only 3-perspective audit) and
`DEFERRED_REFACTORS.md` (R1–R6). The audit's root cause: **the model was only ever validated
in-sample + block-level, never out-of-sample + system-level.** We are remediating in rounds, each a
pure/safe increment guarded by the previous round's safety net.

Status date: 2026-06-08. Env: Windows / PowerShell 5.1 (zh-CN quote traps), ngspice via subprocess,
`.venv` 3.11 (`./.venv/Scripts/python.exe`). Run scripts from the repo root (`C:\code\LDO_modeling`).

---

## ROUND 1 — out-of-sample guardrails  ✅ DONE
Built the missing held-out safety net as a **pure increment** (no change to `fit_model` fit/emit or
`score()` composite).

**Added `harness/crossval.py`** — three guardrails, all general (derive from `ref["loads"]` + relative
thresholds; no 304M/8-24M/121µ/"3-corners" hardcoded):
1. `loco()` — leave-one-load-corner-out, analytic via `fit_model.predict`. Interp from N-1 corners
   (linear for 2 retained), score held-out vs GT. Metrics mirror `score.py` (Zout mag-RMS dB & PSRR
   atten-RMS dB over f>=1e3; noise `score._noise_metrics`). Gate: held-out <= max(2x in, in+0.5dB).
2. `offgrid()` — geometric-midpoint loads (49µ/174µ) through the **emitted `.lib`** in ngspice
   (exercises the real interpolation+clamp); written to `results/crossval/` (NOT `model/`).
3. `identifiability()` — cond(J)/σ of `d ln(transfer)/d ln|p|` (uniform relative perturbation
   `p*(1+δ)`); classify SWITCH / INVISIBLE / OK per param.
- CLI: `python harness/crossval.py --variant base | --all | --strict | --no-offgrid`.
- Output: `results/crossval/{vkey}.json` + `crossval_matrix.md`.

**Edited `harness/score.py`** — `__main__`-only `--crossval`/`--strict` flag (lazy `import crossval`).
`score()` body + weights untouched -> `run_matrix` (which calls `score()` directly) is byte-unaffected.

**Reproduced the audit exactly:** Zout cond=∞ (rank-deficient) on base/v1/v2/A-layer; **2.33e8** v3
(audit 2.3e8); **55** v4 (audit ~46); R_pl SWITCH `[7.81e6,54.5,53.7]` on v1; v3 pcw0 SWITCH
`[60.4,8.23e7,8.33e7]`. **LOCO FAILs on all 14** (overfitting confirmed). Zero composite regression.

## ROUND 2 — composite-safe interpolation hardening  ✅ DONE
Fixed the overshoot pathology + uncontained switches the guardrails caught, **byte-identical composite**.

**Edited `harness/fit_model.py`** (`_pexpr`/`_poly` + new `_body`):
- Tightened the band-aid clamp `[min/1.5, max*1.5]` -> a **tight corner-envelope clamp** with a small
  margin (`CLAMP_M=1.005` log / `CLAMP_ADD=0.005`-of-span linear). The margin keeps the clamp
  **inactive AT the corners** (poly still passes through them) -> the 3-corner score is byte-identical,
  while between/beyond corners every param is bounded to the measured envelope.
- **Clamp linear params too** (were unclamped -> `pcb1/G2/G3` overshot freely).
- `_poly` deg = `min(len(LOADS)-1, 2)` (general).
- DEFERRED `frozen`/`recip` strategies (from the plan): they change the polynomial *form* -> ~1e-6
  rounding drift -> would break byte-identical for ~no gain (the clamp already contains constants+switches).

**Edited `harness/crossval.py`** to stay faithful: `_interp_params` mirrors the envelope clamp + a LOAD
clamp (clip u to retained range, = emit's `ic=min(max(iload,LOADS[0]),LOADS[-1])`); identifiability
gate marks clamped switches **CONTAINED** (FAIL->PASS); `_matrix` split into `Z/P_interp` (overfit) +
`offgrid_Z/P` (deployment).

**Verified:** composite **BYTE-IDENTICAL on all 14** (`results/generalization/matrix_baseline_r1.json`
diff = 0). Overshoots gone (emitted v3 pcw0 clamp 1.25e8->8.37e7; A/B v3 off-grid PSRR@174µ
**21.70->10.11 dB**; base control unchanged 4.07). LOCO held-out PSRR blow-ups collapsed (v4
63.89->8.04, v3 39.26->11.11). **Identifiability PASS on all 14**. `fit_model --selftest` passes.

## ROUND B — system-level acceptance test (LDO + buffer @ carrier)  ✅ DONE
Built the audit's #1 missing test (D2-1 / D4#1; R4 + the merged-in R6#3): the deliverable
(coherent carrier-sideband fidelity) is now MEASURED end-to-end, GT vs the EMITTED model.

**Added `harness/systest.py`** (modeled on `crossval.py`; fits+emits to `results/systest/`,
runs both sides live, never touches `score()`/`matrix.json`):
- **Testbench** = the same representative RF buffer + aggressor dropped on BOTH GT and model.
  KEY PHYSICS: an LTI LDO can't make an `f_c+/-Delta` sideband from tones alone -> the BUFFER
  must mix, so it is a **vout-dependent** B-source `i_buf = I0*(1 + k*(V(vout)-Vdc))*sin(2pi*fc*t)`;
  a supply-ripple aggressor `Vin SIN(1.05 vrip Delta)` makes vout ripple at Delta (via PSRR),
  the buffer mixes it onto `f_c` -> sideband @ `f_c+/-Delta = Zout(fc+/-Delta)*I0*k*PSRR(Delta)*vrip`.
  One TB -> three diagnostics: carrier `f_c` (=Zout(fc)*I0, the R6#3 "no ripple" check),
  baseband `Delta` (=PSRR(Delta)*vrip), and the coherent sideband `f_c+/-Delta` (mag+phase).
- **Coherent FFT**: snap `f_c` to a multiple of `Delta`; window `Twin = NBEAT/Delta` so f_c and
  f_c+/-Delta land on exact bins NBEAT apart (Hann skirt of the big carrier stays << sideband);
  complex extraction at each bin; GT-vs-model = mag-dB + phase-deg.
- **Validity-envelope gate** (detect-don't-assume, R6#3): if `f_c+Delta` > the characterized
  ceiling at the corner (`z_<nom>_hf` ~500 MHz nominal / `z_<il>` ~100 MHz off-nominal) ->
  status **OUT_OF_ENVELOPE**, NO verdict (refuses to pass on an extrapolated rolled-off Zout).
- **General**: `f_c/Delta/I0/k/vrip/corner` are profile/CLI params (`SYS_PROFILE` in-module +
  `--fc/--delta/--ibuf/--k/--vrip/--corner`) with envelope-derived defaults -- `f_c = 0.6 x
  ceiling` lands at 300 MHz for Target-A WITHOUT any 304e6 literal. No 304M/8-24M/121u hardcoded.
- CLI: `python systest.py --variant base | --all | --strict | --k 0`. Output: `results/systest/`.

**Edited `harness/score.py`** (`__main__` only): added `--systest` mirroring `--crossval`
(lazy `import systest`, `systest.run(a.variant)` after `score()`). **Edited `harness/ng.py`**:
`run(..., timeout=180)` kwarg (additive, default-preserving) for the long fine-dt carrier tran.
`score()` body / `W` / `comp` / `run_matrix` UNTOUCHED.

**VALIDATED**: composite **BYTE-IDENTICAL on all 14** (MAX_ABS_COMPOSITE_DELTA = 0.0 vs
`matrix_baseline_r1.json`). Mixer A/B: `k=0` -> model sidebands collapse to the FFT floor
(-118 dBc, LTI can't self-mix) while GT keeps only its tiny residual nonlinearity (-94 dBc) ->
confirms the `k>0` sidebands are real buffer mixing. Envelope gate fires at `f_c=600 MHz`
(>500 MHz ceiling). `score.py --variant base --systest` runs scorecard THEN system test cleanly.

**FINDING (the deliverable, finally measured)**: at the nominal corner (in-envelope, f_c=300 MHz)
13/14 reproduce carrier ripple `Zout(f_c)` to <0.13 dB and most sidebands to <0.5 dB. The test
immediately surfaces the gaps the composite under-weights:
- **`v4_ffpsrr` FAIL**: sidebands **+7.2 dB / +67deg** because `|PSRR(Delta=1MHz)| = +16.8 dB`
  (feedforward/non-min-phase PSRR peaks >0 dB there) and the model's `PSRR(Delta)` is off ~7 dB/67deg.
  The known V4 PSRR-phase residual (masked by `pphase=0.03` in the composite) is the DOMINANT
  deliverable-level failure at the carrier -- exactly the audit's "block fidelity != system fidelity."
- **`v1_nmos`**: `Zout(f_c)` under-predicted **2.2 dB** (GT 27.5 vs model 21.3 ohm; high-ESR-cap
  weak identifiability) -> carrier + sidebands all -2.2 dB (PASS under the 3 dB first-cut, flagged).
- Minor: `cg_hi`/`v2_capless` 1.6-1.8 dB sideband error (v2 phase ~29.5deg). See `results/systest/systest_matrix.md`.

These gaps are this round's FINDING (the test's purpose was to MEASURE) -> they motivate a later
model-fix round; this round did not touch fit/emit.

## ROUND B-fix — small-signal calibration + GT linearity gate  ✅ DONE
The Round-B system test flagged `v4_ffpsrr` FAIL (+7.2 dB / 67deg sidebands). Before touching the
protected fit/emit path, the root cause was measured directly -- and it **OVERTURNED the premise**
(this round was expected to be a model fix; it turned out the model was already correct):
- v4 GT `.ac` PSRR @ Delta=1 MHz = +16.71 dB / +130.1deg; the analytic model = +16.82 dB / +128.1deg
  -> **fit error +0.11 dB / -2.0deg** (both peak +17.2 dB @ 1.059 MHz; the emitted `.lib` realizes
  +16.80 dB). The model already fits v4's feed-forward non-min-phase PSRR peak essentially perfectly.
- The +7 dB/67deg was a **large-signal artifact of the GT**, not a model error: vrip=10 mV onto a
  sharp +17 dB resonance -> ~68 mV output ripple (~8% of Vout) -> the GT regulator COMPRESSES, where
  the LTI model (correctly) does not follow. A vrip sweep confirmed it: GT baseband PSRR 9.66 dB@10 mV
  -> 16.36@3 mV -> **16.75@1 mV** (gap +0.07 dB), plateauing at its .ac value, while the model stays
  LTI at 16.82 dB. So the system test had a **measurement-validity gap**: a fixed vrip can over-drive
  the GT past the LTI model's scope and then mis-report the LTI-correct model as FAIL. USER-CONFIRMED
  direction: fix the TEST, not the fit; document v1 as a known floor.

**Edited `harness/systest.py` ONLY** (fit/emit untouched -> composite byte-identical, stronger than
"no regression"):
- **Small-signal stimulus auto-calibration** (default): `vrip = min(default VRIP, LIN_FRAC_RIPPLE *
  Vout / |PSRR(Delta)|_model)`, keeping the predicted vout ripple <=1% of Vout so the test measures
  the LTI deliverable. Only engages when |PSRR(Delta)| > ~0 dB, so only **v4 (1.296 mV)** + **cg_hi
  (5.567 mV)** recalibrate; the other 12 stay at 10 mV. CLI `--vrip` is honored as-is.
- **Empirical GT linearity gate** (detect-don't-assume): extra GT-only runs at vrip/KLIN and at
  vrip=0; the vrip=0 run isolates vrip-INDEPENDENT baseband content (intrinsic spurs / DC drift),
  COMPLEX-subtracted so a spur landing on Delta can't masquerade as nonlinearity (fixed a v5/v6
  false positive). If the spur-subtracted GT baseband gain shifts > LIN_TOL_DB between vrip and
  vrip/KLIN -> status **LARGE_SIGNAL**, verdict WITHHELD (like OUT_OF_ENVELOPE) -- never a model FAIL.
- New `status` field {OUT_OF_ENVELOPE, LARGE_SIGNAL, OK}; verdict gated on `in_env AND linear`;
  `_report` / `_matrix` (+ vrip & status columns) / `--strict` updated. `score.py --systest --strict`
  guard adds `and srep.get("linear", True)` (`__main__`-only, byte-safe). Knobs are dimensionless
  (LIN_FRAC_RIPPLE=0.01, KLIN=4, LIN_TOL_DB=1, LIN_MIN_SNR=8); vrip is DERIVED from Vout & |PSRR(D)|.
  No 304M/8-24M/121u literal added.

**VALIDATED**: `systest.py --all` -> **all 14 OK + PASS**. v4 FAIL->PASS (carrier/sidebands
+0.08-0.10 dB at 1.296 mV). cg_hi improved (+1.76 -> +0.80 dB; it was mildly over-driven too). v5/v6
correctly LINEAR/PASS (spur subtraction). 11 others byte-identical to `systest_matrix_baseline_B.md`.
**v1_nmos PASS-with-flag** (-2.22 dB carrier = the documented high-ESR-cap Zout(f_c) identifiability
floor; NOT chased -- joint-LS Cout already tried+reverted in `finding-zout-passivity`). Forced
`systest.py --variant v4_ffpsrr --vrip 10e-3` -> **LARGE_SIGNAL** (GT gain shifts 6.95 dB at vrip/4,
verdict withheld, no FAIL). Composite **byte-identical all 14** (MAX_ABS_COMPOSITE_DELTA=0.0 vs
`matrix_baseline_r1.json`). `crossval.py --all` unchanged. New reference:
`results/systest/systest_matrix_baseline_B2.md`.

## ROUND B-cover — profile-driven `*_hf` ceiling (GHz-carrier coverage)  ✅ DONE
The system test can only verify a carrier that is IN-ENVELOPE (below the model's `*_hf` ceiling). That
ceiling was the global hardcoded constant `bench.AC_HF = "ac dec 40 10 500meg"` (500 MHz, for the
Target-A 304 MHz carrier), so the real Target-B carrier (~5.8 GHz) tripped OUT_OF_ENVELOPE and could
not be verified. This round makes the ceiling a per-variant characterization-RECIPE parameter (general,
no hardcoding) and proves the whole pipeline runs end-to-end at a GHz carrier. (Audit R6#3 remainder /
the `modeling-bandwidth` + `tool-generalization` directive: "don't default to 500 MHz; decide the
ceiling from a 6-10 GHz exploratory sweep; `*_hf` cutoff != system max freq".)

**Exploratory 10 GHz sweep first (detect-don't-assume), nominal corner, 4 representative DUTs:**
- **Ideal-cap parts** (`base`, `v3_miller`: 1 nF / 0.5 ohm): Zout rolls off **smoothly to the ESR
  floor** through 10 GHz, phase -> 0 from below (resistive), **NO inductive rise / no ESL term needed**;
  the lumped RLC model extrapolates correctly. Cout/ESR auto-extraction is **byte-stable** vs the
  500 MHz clip (997 pF; ESR 0.4999 @10 GHz vs 0.5000 @500 MHz) because the phase<-45deg cap-band
  selection picks the SAME points.
- **High-ESR / cap-less parts** (`v1_nmos` ESR=30, `v2_capless` ESR=120): show genuine HF rolloff
  structure above 500 MHz, and **naively extending their ceiling BREAKS the simple tail extraction**
  (v1: 419 pF->1.2 pF, ESR 28->19 ohm). ⟹ the ceiling MUST be per-variant / opt-in, never a blanket
  global bump; a real part with HF structure may need extraction care / an ESL term (= Target-B work).

**Edits (general; existing 14 variants' refs UNTOUCHED -> composite byte-identical):**
- `harness/bench.py`: `HF_STOP = 500e6` (default ceiling) + `_fmt_hz()` (ngspice SI: 1e9->"g") +
  `ac_hf_cmd(fstop=None, dec=40, fstart=10)`; `AC_HF = ac_hf_cmd()` is **literally
  `"ac dec 40 10 500meg"`** (verified) -> every existing caller unchanged.
- `harness/gen_reference.py`: HF block reads `fstop = v.get("hf_stop") or bench.HF_STOP` and builds the
  sweep via `bench.ac_hf_cmd(fstop)`; the hardcoded `304e6` print is replaced by a DERIVED probe
  `fcar = 0.6*fstop` and the ceiling goes in the header (informational only -> npz byte-identical at
  the default). The `z_<nom>_hf` array NAME keeps `"121u"` (nominal-corner de-hardcoding is R1).
- `harness/variants.py`: added `base_ghz` = `ldo_gt` with `hf_stop=10e9` (additive). `systest._envelope`
  auto-reads the 10 GHz ceiling and `f_c = 0.6*ceiling = 6 GHz` auto-scales -> **NO systest code change**.
- `fit_model` (fit/emit/predict), `score()`, `run_matrix`, `crossval`, `systest` LOGIC: all UNTOUCHED.

**VALIDATED**: `base_ghz` `systest` -> **env=in, status=OK, verdict=PASS** at `f_c=6 GHz` in ~5 s
(carrier/sidebands +/-0.02 dB; **Zout(6 GHz) GT 0.487 vs model 0.4869 ohm**, i.e. the lumped model
extrapolates to the ESR floor correctly; Cout/ESR 997 pF/0.5 ohm; GT LINEAR). Composite
**BYTE-IDENTICAL on all 14** (`run_matrix.py --reuse`, MAX_ABS_COMPOSITE_DELTA = 0.0 vs
`matrix_baseline_r1.json`; `base_ghz` is a 15th informational row, composite 3.879 ~ base 3.881).
`systest.py --all` -> the 14 baseline rows are **IDENTICAL to `systest_matrix_baseline_B2.md`**;
`base_ghz` is a new in/OK/PASS row at 6 GHz. `crossval.py --all` -> identifiability PASS on all 15;
LOCO/OFFGRID FAIL by documented design; the 14 are unchanged (B-cover cannot touch the crossval path).
`--fc 304e6` override still works (f_c=304 MHz, PASS); `base` default still 300 MHz.

**What `base_ghz` proves / does NOT prove (honesty):** it proves the recipe + validity-envelope are
GENERAL — one profile number (`hf_stop`) carries characterize->fit->emit->envelope->coherent-FFT to GHz
with no code edits — i.e. the PLUMBING works at GHz. It does NOT prove real-silicon GHz physics
(ESL / distributed effects); that is **Target B**, guarded by the same exploratory sweep (which already
flags when a part has HF structure the lumped model / simple extraction can't follow).

---

## ROUND R5-transient-ID — ONE multitone .tran replaces the two AC sweeps (VALIDATION)  ✅ DONE — GO (proof-of-concept, with hardening)
**Goal (R5 input-simplification):** the engineer sets up FOUR analyses/corner (Zout-AC, PSRR-AC,
.noise, DC). Test whether ONE multitone transient can replace the two AC sweeps. Mechanism: drive vin
with voltage tones (A-set) AND inject current tones into vout (B-set) in ONE `.tran`, on DISJOINT
coherent FFT bins; LTI superposition -> A-bins read PSRR=Vout/Vin, B-bins read Zout=Vout/Iinj; per-bin
RATIOS recover magnitude AND phase (the window/time-origin phase reference cancels). Validated on the
EXISTING synthetic GT (AC = ground truth, no Cadence). **ADDITIVE: two new leaf files
(`harness/trans_id.py`, `harness/validate_trans_id.py`), zero edits to any shared module.**

**Result (4 architectures; `results/trans_id/trans_id_validation.md`):**
- **Level-1 (per-freq recovery vs AC):** Zout `<= 0.45 dB` everywhere; PSRR `<= ~1.6 dB` in the RF band
  (>=100 kHz, where the spur/carrier deliverable lives). Mid-band PSRR points are accurate to `<= 0.1 dB`.
  The smoke test independently matched AC to **<1 deg phase**.
- **Level-2 (build the model — support-matched, apples-to-apples):** fit trans z/p (noise/dc reused;
  off-nominal trans truncated to the AC band so the trans and AC fits see the SAME support) -> emit ->
  score vs AC truth. **max |dComposite| = 2.60** (base **+0.06**, v1_nmos **-0.68**, v3_miller **+2.18**,
  v2_capless **+2.60**). Every trans-built model is the same grade as its AC-built counterpart.
- **Linearity/IM gate (half-amplitude rerun): `<= 0.15 dB`** change -> small-signal, no IM on a
  measurement bin. Tones are IM-de-aliased (every tone on a bin == 1 mod 3 -> all 2nd-order products
  a+/-b, 2a fall off all measurement bins).
- **Cost:** 3 cheap coherent transients/corner (low 1k-100k / mid 100k-10M / high 10M-ceiling),
  ~10-15 s for all 3 corners per variant -- vs the band x timestep blow-up of a single 10 Hz-500 MHz
  sweep (~1e8 points). **The band split is REQUIRED; the resonance does NOT need a dense comb (12
  tones/dec + the parametric fit captures the Q~16 peak).**

**Honesty / where it degrades (from an adversarial review, all 4 flaws addressed):**
- The **Zout path is fully validated** (recovery + equivalent/better model on all 4).
- The **PSRR composite gap (v3_miller +2.18, v2_capless +2.60) is a DOWNSTREAM FITTER issue, not a
  trans-ID extraction error:** the trans PSRR DATA is accurate (Level-1), but the existing parametric
  `fit_psrr` (real bank + ONE complex 2nd-order section) lands on a different local optimum when given
  the trans frequency grid vs AC's 40/dec grid. Tested 20 tones/dec -> made it WORSE (v3 +5.4), i.e.
  more tones is NOT the fix; it is fitter conditioning / grid-sensitivity on multi-pole PSRR phase.
- **Deep LF-PSRR at the LIGHTEST load (20u, 1-100 kHz)** sinks toward the multitone IM/SNR floor
  (up to ~19 dB point error) -- immaterial to the model (Level-2), fixable by raising the vin amplitude.
- Noise still needs a separate `.noise` (a deterministic `.tran` has no device noise); DC Vout falls out
  of the settled window mean for free.

**GO with pre-Cadence hardening.** Already applied: IM-de-aliased grid, half-amp linearity gate,
support-matched Level-2. Before a real (mildly nonlinear) Cadence part: (1) tie the settle pre-roll to
the DUT's slowest mode (a per-band `settle_s` param, default band-relative); (2) auto-calibrate per-path
drive amplitude to |Zout|/linear range (the gate flags violations); (3) robustify/grid-match the PSRR-
phase fit for multi-pole parts; (4) noise stays `.noise`. **No regression:** run_matrix composite
byte-identical, `systest --all` identical to baseline_Bcover, `crossval --all` identifiability PASS (the
new files are leaf scripts imported by nothing in the AC path). See `results/trans_id/` +
[[next-r5-transient-id]] / [[finding-trans-id-validation]].

---

## ROUND R5-prod — trans-ID PRODUCTIONIZED: Verilog-A stimulus + Cadence importer + GUI (A/B/C)  ✅ DONE
**Goal:** turn the validated single-multitone-trans recipe (a LOCAL ngspice B-source harness) into
something usable on a real Cadence part and in the GUI. Built **A + B + C**; **deferred D** (the
multi-pole PSRR-phase fitter robustness — it is the only piece that touches a scored module, so it was
kept out to preserve the byte-identical contract; tracked as `DEFERRED_REFACTORS.md` R7). The emitted
`.va` is **actually compiled** (the hard "don't just write the .va" constraint) and proven end-to-end.

- **A — `trans_id.emit_stim_va(bands, outdir, ...)`** emits a parameterized Verilog-A multitone STIMULUS
  fixture (+ sidecar `plan.json` + README), ONE `.va` per band (the recipe is band-split; a combined
  `.tran` would blow up the point count). Flat-unrolled `V(vin)<+ vdd+Σva·sin(\`M_TWO_PI·f·$abstime)` and
  `I(vout)<+ -(Σib·sin(...))` — mirrors the proven `emit_va` spur idiom (no VA arrays/loops); the `-`
  injects current INTO vout, matching bench `Iac 0 vout` / `Biinj 0 vout`. Amplitudes stay settable
  params; only frequencies are baked. `$abstime` ⇒ `.tran`-only (noted; not PSS/HB-portable).
- **B — `harness/trans_import.py`** reads exported waveform(s) + `plan.json`, recovers Zout/PSRR per band
  via the new pure `trans_id.extract_zp_from_wave` (refactored out of `measure_band`, which is now
  behavior-identical), and writes `z_<c>.csv`/`p_<c>.csv` (+ nominal `_hf`) in the `freq,real,imag`
  layout `cadence/import_cadence.py` consumes. vin is reconstructed analytically when not exported.
- **C — GUI 5th tab "5 · Trans-ID"** + `ModelerCore.import_trans[_folder]`: convert one multitone trans
  → auto z/p, funneled through the EXISTING `import_data` (assemble + guardrails reused). Noise/DC still
  come from Tab 2. Headless `_selftest` extended (`_selftest_transid`): synth multitone → import_trans →
  asserts z/p/noise recovered (0.000 dB) + fittable. (Agent can't render the Qt window — the offscreen
  matplotlib render hangs in this dev venv as before; verified the LOGIC headless + the tab CONSTRUCTS
  offscreen: 5 tabs, widgets present. User runs the real window in a desktop terminal.)

- **Toolchain (NEW, under gitignored `tools/`):** Windows VA→OSDI now works — `tools/openvaf/openvaf.exe`
  (OpenVAF 23.5) + an MSVC-compatible `link.exe` (conda-forge `lld`'s lld-link, invoked as `link`) + the
  MSVC CRT/Win-SDK import libs splatted by `xwin` to `tools/xwin/splat` on `$LIB`. Wrapped by
  `harness/vatools.py` (`compile_va`, env-var overridable: `OPENVAF` / `OSDI_LINKER_DIR` / `OSDI_LIB`;
  Linux self-links). This also bootstraps the deferred Target-B `.va` HB-check.

- **End-to-end proof — `harness/validate_trans_va.py` (`results/trans_id/trans_va_e2e.md`):** per variant,
  emit + **COMPILE** the stimulus `.va` (OpenVAF) → run ONE `.tran`/corner/band in ngspice via **OSDI**
  driving the GT DUT → import → fit → score vs AC. **All 4 `.va` compiled + ran; max |d_path| = 0.04**
  (compiled-VA vs the B-source dev path — i.e. the productionized fixture reproduces the validated recipe
  to numerical noise), `d_AC` matches the dev path (base +0.06, v3 +2.14, v2 +2.60, v1 -0.68), and the
  importer CSVs are consumed by `import_cadence.assemble` on every variant (GUI-import OK).

**ADDITIVE / no regression (all re-verified this round):** A/B/C touch NO scored module
(`trans_id`/`trans_import`/`vatools` are leaves imported by nothing in the AC path; the GUI isn't
imported by the harness). `run_matrix --reuse` composite **byte-identical** (max|dComposite|=0.0 on the
14 baseline variants), `systest --all` **0 rows differ** vs `baseline_Bcover`, `crossval --all`
identifiability **PASS** for all. **Adversarial 4-reviewer workflow:** A + no-regression SOUND (sign,
identical tones, settable amps, compilation all confirmed; one reviewer rebuilt a band end-to-end);
importer/toolchain MINOR -> applied the real fixes (coherent-window coverage guard in
`extract_zp_from_wave`; vatools utf-8 decode + abs-path; README va/ib caveat). See
[[next-trans-id-productionize]] / [[finding-trans-va-pipeline]].

---

## ROUND R7 — coarse-grid PSRR/Zout fitter robustness  ✅ DONE — **NEGATIVE RESULT (investigated + reverted)**
**Goal (DEFERRED_REFACTORS R7, the trans-ID "D"):** close the trans-built composite gap on the
multi-pole parts (`v3_miller` +2.18 / `v2_capless` +2.60 vs AC-built) by making `fit_psrr` (and, per
user decision, `fit_zout`) robust to the coarse/irregular multitone grid. Built a flag-gated
(`regrid=False` default) deterministic complex-section **multi-start** + selectors, threaded only into
the trans-ID validators, fully gated through the 3-baseline contract. **Diagnosed, found not
production-robust, and REVERTED.** AC stayed byte-identical the whole time.

**The diagnosis (3 oracles; the value of this round):**
- **v3 = ill-conditioned, not robustly fixable.** Multi-start escapes the trapped single-start (B-source
  dev path 2.18 → **1.61**, and the oracle confirmed the chosen candidate is also closest to AC). BUT on
  the **compiled-VA production path the SAME recipe makes v3 WORSE** (2.14 → **2.37**, `d_path` 0.04 →
  **0.76**) — both deterministic. The coarse multi-pole PSRR fit has multiple near-degenerate optima; the
  discrete selector flips with the tiny B-source-vs-VA stimulus difference, and no selector may peek at AC
  to break the tie safely. (Same instability the "20 tones/dec → v3 +2.2→+5.4" note warned of.)
- **v2 = information limit, oracle-proven twice.** PSRR oracle: the min-phase **shelf is already
  AC-optimal** at every corner (complex candidates ~96° off); default already keeps the shelf. Zout
  oracle: **every** candidate (default + wide multi-start) returns the **identical** AC-zrms at every
  corner → the +2.60 is the sparse grid under-determining a near-invisible high-ESR cap (130 pF / 116 Ω)
  + 20 µA deep-PSRR low-SNR. **No fit-side change can touch it.**
- **`fit_zout` regrid** was harmful (can drop v3's needed 2nd RL branch) and useless (oracle matched
  default) → removed.

**Why reverted:** the merge gate required `d_composite` shrink on BOTH `validate_trans_id` AND
`validate_trans_va`; v3 shrank on the former but **regressed on the latter** (the production fixture),
and shipping would break the "VA reproduces B-source to numerical noise" property. No robust fitter-layer
win exists. **Conclusion: the gap is a coarse-grid information/conditioning limit; closing it needs a
RECIPE change (denser trans tones at the hard/light corners or a cheap AC anchor in `trans_id.py`), a
separate larger round — NOT another `fit_psrr` selector.** (User decision 2026-06-09: revert + record.)

**VERIFIED post-revert (working tree == stable baseline):** `run_matrix --reuse` composite
**byte-identical** to `matrix_baseline_r1.json` (Δ=0); `fit_model --selftest` PASS; `validate_trans_id
--all` back to baseline (v3 +2.18, v2 +2.60, base +0.06, v1 −0.68); `validate_trans_va --all` `d_path`≈0
restored; `crossval --all` identifiability PASS; `systest --all` == `baseline_Bcover`; no orphaned R7
symbols remain in `harness/`. Full evidence: `DEFERRED_REFACTORS.md` R7 (now "INVESTIGATED + REVERTED").

---

## WHAT'S STILL OPEN (the deep gap + the deliverable)
The **LOCO interp (bracketed) gap is UNCHANGED and still FAILs** — base Z/P 1.44/5.60, v3 1.31/11.11,
v4 1.49/4.87 — by design (the clamp deliberately doesn't touch the interior). This is the *fundamental
few-corner limit* (can't learn a load-curve from 3 points), not an interpolation bug. Closing it needs:
- **Deep LOCO fix:** more load corners in the characterization recipe, OR a physically-constrained
  per-corner parametrization that generalizes (smaller free DOF per corner). Also the PSRR/noise
  *regime* switches = model-order consistency (audit D4#7).

Audit D4 priority order for what remains (R1/R2 were audit D4 items 3+4; **the [B] system test is
BUILT — Round B; [B-fix] is DONE — proved the v4 "fail" was a test over-drive artifact, hardened the
TEST not the fit; and [B-cover] is DONE — Round B-cover above, the `*_hf` ceiling is now a
profile-driven recipe param and the system test verifies a GHz carrier in-envelope (`base_ghz` @
6 GHz)**):
1. **[Target B] the real Cadence LDO (~5.8 GHz)** — the real frontier. The recipe is now GHz-capable;
   extract a real part to a GHz `hf_stop`, run the SAME exploratory sweep to decide its ceiling AND
   detect whether it needs a series-ESL term (the lumped Zout flattens to ESR at HF — a real inductive
   GHz tail would need an added L element; high-ESR parts also stress the simple Cout/ESR extraction,
   see the B-cover sweep finding). Then hot-S sideband asymmetry under Xyce `.HB` / OpenVAF·VACASK `.va`.
2. **[C] VDD axis + `dc_linereg` anchor (R3, raised)** — `≥2` vin for PSRR/Zout + DC line-reg anchor;
   fixes R6#2 "rail droop"; the user runs high/nom/low VDD.
3. **Deep LOCO fix** (more corners / physically-constrained per-corner params); use-case-resolved
   scorecard + kernel-Zout phase pre-gate (D4#6); model-order consistency (D4#7).
4. **(deferred / likely-not-worth-it) v1_nmos Zout(f_c)** −2.22 dB carrier — a documented high-ESR-cap
   identifiability FLOOR (small-signal; joint-LS Cout tried+reverted, `finding-zout-passivity`). Within
   the 3 dB first-cut PASS and flagged; only revisit if a use case needs <2 dB |Zout| at high-ESR caps.

**R5 input-simplification — VALIDATED + PRODUCTIONIZED** (ROUNDS R5-transient-ID + R5-prod above): one
multitone `.tran` replaces the two AC sweeps, and it is now a compiled Verilog-A stimulus fixture
(`emit_stim_va`) + a Cadence waveform importer (`trans_import.py`) + a GUI tab (5 · Trans-ID), proven
end-to-end through OpenVAF→OSDI→ngspice. **The only remaining R5 piece is "D"** — the multi-pole
PSRR-phase fitter robustness (`DEFERRED_REFACTORS.md` R7, deferred by decision: it touches the scored
`fit_psrr` and needs a flag-gated, 3-baseline-proof round). Folds into Target B.

Recommended next: **Target B** (the real chip — the recipe is now GHz-ready and the single-trans
characterization is validated; bring up a real part end to end, ESL/HF-extraction checks per the B-cover
sweep, and the trans-ID Verilog-A fixture); OR **C** (VDD axis); OR the **deep LOCO fix**.

---

## HOW TO RUN / VERIFY (from repo root)
- Guardrails: `./.venv/Scripts/python.exe harness/crossval.py --variant base`  (or `--all`)
- Score + guardrails: `./.venv/Scripts/python.exe harness/score.py --variant base --crossval`
- System test: `./.venv/Scripts/python.exe harness/systest.py --variant base` (or `--all`;
  `--k 0` for the mixer A/B sanity; `--fc 600e6` to trip the frequency-envelope gate;
  `--variant v4_ffpsrr --vrip 10e-3` to trip the LARGE_SIGNAL gate). By default `vrip` AUTO-calibrates
  to small-signal (<=1% Vout ripple); pass `--vrip` to force a level. Also via
  `harness/score.py --variant base --systest`. Output: `results/systest/` (NOT `matrix.json`).
- GHz-carrier demo (B-cover): `gen_reference.py --variant base_ghz` then
  `systest.py --variant base_ghz` -> verifies a ~6 GHz carrier IN-ENVELOPE (PASS). To make any DUT
  GHz-capable, add `hf_stop=<Hz>` to its `variants.py` entry (default 500 MHz) and regen its ref; the
  system test's `f_c = 0.6 x ceiling` and validity-envelope then auto-scale, no code edits. Decide the
  ceiling from a 6-10 GHz exploratory Zout sweep (smooth ESR rolloff -> lumped model extrapolates;
  inductive/2nd-resonance rise -> add a series-L term first).
- R5 trans-ID validation (additive experiment): `./.venv/Scripts/python.exe harness/validate_trans_id.py
  --all` (then `--report`); single-band smoke vs AC on base/121u via `python harness/trans_id.py`.
  Output: `results/trans_id/trans_id_validation.md` (Level-1 per-freq recovery + Level-2 model
  equivalence + go/no-go). Leaf scripts imported by nothing in the AC path (zero regression by design).
- R5-prod compiled-VA end-to-end: `./.venv/Scripts/python.exe harness/validate_trans_va.py --all` (then
  `--report`; `--preflight` prints toolchain status). Emits + COMPILES the stimulus `.va` (OpenVAF) and
  runs it in ngspice via OSDI → import → fit → score. Output: `results/trans_id/trans_va_e2e.md`
  (max |d_path|=0.04 vs B-source; all `.va` compiled). Importer smoke: `python harness/trans_import.py
  --smoke`. Toolchain wrapper: `harness/vatools.py` (OpenVAF + conda `lld` link + `xwin` MSVC libs under
  `tools/`; env overrides `OPENVAF`/`OSDI_LINKER_DIR`/`OSDI_LIB`). GUI: `python gui/ldo_modeler.py`
  (Tab 5 · Trans-ID); headless logic check is `gui/ldo_modeler.py --selftest` (Qt render hangs offscreen
  in this dev venv — known; the LOGIC + tab construction are verified).
- **Zero-regression invariant** (must hold for any future "safe increment"):
  `./.venv/Scripts/python.exe harness/run_matrix.py --reuse` then diff `composite` in
  `results/generalization/matrix.json` vs `results/generalization/matrix_baseline_r1.json` (== 0).
- Baselines preserved: `results/generalization/matrix_baseline_r1.json` (composite; still current),
  `results/crossval/crossval_matrix_baseline_r1.md` (Round-1 schema — STALE for direct cell-diffs;
  identifiability is PASS post-Round-2; use the causal argument + documented numbers for crossval
  no-regression, not a cell-diff vs this file),
  `results/systest/systest_matrix_baseline_B.md` (Round-B reference: pre-calibration, v4 FAIL),
  `results/systest/systest_matrix_baseline_B2.md` (Round-B-fix reference: calibrated + linearity-gated,
  14 OK/PASS),
  `results/systest/systest_matrix_baseline_Bcover.md` (**current** reference: 15 rows = the 14 (identical
  to B2) + `base_ghz` in/OK/PASS @ 6 GHz — the next round must show NO regression on the 14 vs this).

## KEY INVARIANTS / GOTCHAS
- The model passes through every load corner **exactly** -> `score`/composite is evaluated only at the
  3 corners, so any change confined to BETWEEN/BEYOND-corner behavior is composite-safe. Use this to
  keep "safe increments" byte-identical.
- `score()` composite must stay byte-identical for additive work; `run_matrix` calls `score()` directly
  (never the CLI). New CLI flags go in `__main__` with lazy imports.
- Emitted model interpolates params quad-in-ln(iload) clamped to the corner envelope; outside
  `[LOADS[0],LOADS[-1]]` the load is clamped (no extrapolation — a hard validity boundary).
- `crossval` is single-DUT-per-pass (`zmodel` reads module `C/RC` set by `fit_variant`); `--all` runs
  variants sequentially (shared ngspice workdir; honors `$LDO_WORK`).
- Memory files: `finding-crossval-guardrails`, `finding-interp-hardening-r2`, `finding-methodology-audit`,
  `next-system-validation-and-quality`.
