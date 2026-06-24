# HANDOFF — Red-zone real-LDO (WuR_PMU) modeling fixes

**Date:** 2026-06-24 · **Branch:** main (6 commits, all pushed) · **Suite:** 369 green

## TL;DR
First real-silicon PMU (`wur_pmu_real` = WuR_PMU_TOP, 1 corner `tt_25c`: 2 V-rails VDD0P8_PLL/VCO +
3 bias current sinks IBP_POLY_500N_LPF / IBP_POLY_3P6U_VCO / IBP_PTAT_TUNE_1P5U_VCO) came back ALL
RED. An ultracode build (5-agent expert panel → fixes → 5-agent adversarial verify) took it to
**4 of 5 ports USABLE**. The expert panel CORRECTED two of my own earlier diagnoses (see below).
**Only pll PSRR (non-minimum-phase) remains** — deliberately deferred (deep, pll-only).

## Final scoreboard (local repro, post-fix)
| port | grade | scores |
|---|---|---|
| VDD0P8_VCO | **[OK]** | Zout 0.72 · PSRR 1.0 · noise 0.49 |
| IBP_POLY_500N_LPF | **[OK]** | IVrms 0.30% |
| IBP_POLY_3P6U_VCO | **[~]** | IVrms 1.18% · \|Y\| 4.27 (in-family) |
| IBP_PTAT_TUNE_1P5U_VCO | **[~]** | IVrms 0.02% · \|Y\| 6.94 (in-family) |
| VDD0P8_PLL | **[!!]** | Zout 1.6 (marg) · noise 2.75 (marg) · **PSRR 6.0 ← the ONLY blocker** |

## Reproduction loop (LOCAL, no npz — the report is self-contained)
The saved red-zone report carries the GT as a log-resampled `[MPD1]` digest, so everything
reproduces from text. Real-chip data lives in `results/redzone/` (LOCAL ONLY, gitignored):
```
python3 results/redzone/_score_real.py          # re-score the real chip (applies Zout sign-fix
                                                 # + hybrid; what the box produces after re-import)
results/redzone/wur_pmu_real_tt_25c.report.txt   # the saved self-contained report (GT digest)
results/redzone/_baseline_synthetic.txt          # the no-overfit yardstick (synthetic ceilings)
```
Rebuild an npz from any pasted self-contained report:
```python
import report_multiport as R, fit_multiport
ref = R.digest_to_npz(text, 'repro.npz'); m = R.parse_manifest(text)
print(R.debug_report(fit_multiport.fit_multiport(ref, m), ref, m))
```

## The 6 commits
1. `171c8ee` report self-contained ([MPD1] GT digest embed + parse_manifest/digest_to_npz)
2. `00434c7` **Zout SIGN BUG** — `importmp._passivity_sign` enforces Re(Zout(s→0))≥0
3. `fdb8137` data-driven I-V knee + cPSRR observability gate + |Y| bar calibration
4. `f2ba77f` current-noise wiring (req 1, opt-in `coverage.enable.inoise`)
5. `66f2431` adversarial hardening (I-V keep-best + cross-val bridge)
6. `ef91e0b` hybrid voltage-noise wired into multiport (vco → USABLE)

## What each fix is (and the 2 diagnoses I corrected)
- **Zout sign** (was: "high-Q/non-min-phase model-form gap" — WRONG). Real GT Zout was
  negative-real across the band → non-physical. The source-reuse refactor (d0a9cf9) switched Zout
  injection to the rail's LOAD source (draws from the node) → true Zout = −V; the z derive returned
  +V (y/pi were fixed, z/couple missed). Fix flips iff the DC-floor real part < 0. Keys on physics
  (stable regulator ⇒ positive-real); synthetic/insert GT already +real → untouched. couple left raw.
- **I-V knee** (was: "deep-triode under-resolved" per the auto-diagnosis — WRONG). Real refs are
  SINKS with a HIGH-Vo compliance ceiling (flat-then-rail-collapse), not a low-Vo knee.
  `fit_isrc._detect_knee` picks side {lo,hi,none}+fitted ceiling vhi from DATA; `_fit_iv` does
  KEEP-BEST vs 'none' (rejects spurious/partial knees); emit (emit_isrc + emit_pmu_model +
  current_crow_from_isrc_fit) follows the detected side.
- **cPSRR ~100dB** = metric artifact (flat-gdd model vs jw-rising GT). OBSERVABILITY GATE
  `|gdd|/g0 < 1e-3` (current_digest) skips grade+sign; HF rise reported as a coupling cap.
- **|Y| bar 3→8dB marginal** — g0+sCp inherently leaves ≤7.16dB on cascode/Wilson refs (validated
  library); real 4-7dB is in-family, NOT a bug.
- **current noise (req 1)** — opt-in `coverage.inoise` → probe-form `.noise` (`nz oprobe=<probe>`,
  BOX-VALIDATE-PENDING) → importmp `noise_i` derive → `_fit_noise` → emit → report panel (kept OUT
  of grade until cross-validated). Form white+1/f reuses the locked air-gap path.
- **hybrid voltage-noise** — after the Zout fix the correct high-Q |Zout| exposed that the Norton
  noise bank can't hold In=Sv/|Zout| for a loop-shaped rail (pll/vco ~11dB). fit_model HAS the
  'hybrid' series-voltage mode (fit_all engages it); multiport forced 'norton'. Wired the gated
  keep-best into `_fit_voltage_output` + threaded nmode/nfkv through report_multiport. Synthetic
  stays norton (never stalls >4dB).

## Generalization guardrails (req 2 — adversarially verified, all held)
- Synthetic 8-variant ceilings the fixes MUST hold: IVrms ≤3.82% · |Y| ≤7.16dB · cPSRR ≤25.91dB ·
  Nrms ≤9.40dB · ivR2 ≥0.93 (see `results/redzone/_baseline_synthetic.txt`).
- Full suite 369 green is the hard gate.
- Adversarial verify (5 agents) cleared Zout-sign / cPSRR-gate / current-noise as generalizing;
  the two I-V breaks it found (flat-ref flap, partial-collapse) were fixed by keep-best (`66f2431`).

## THE ONE REMAINING RESIDUAL — pll PSRR (non-minimum-phase)
pll PSRR = 6.0dB; phase RMS ~51°, worst ~−89°. The NPC=1 complex 2nd-order section EXISTS
(`fit_model.psrr_model` / `_bank_fit` / `_aaa_conj` / `_pair_section`) but loses to the real shelf
because `_bank_fit`'s AAA initializer fails to converge on the sparse ~47-pt silicon AC sweep
(complex resid 3.16 > shelf 1.06; the shelf early-return gate correctly does NOT fire: e_shelf=0.65,
shelf_ph=37°). **Fix = a robust complex-section initializer for sparse real AC data** (deep; pll-only).
Look at `harness/fit_model.py:_bank_fit` (~439) and `_aaa_conj` (~411); `analyze_psrr_phase.py` exists.

## NEXT STEPS (in order)
1. **BOX RE-VALIDATE** (the natural next gate). The Zout sign-fix + hybrid noise take effect on
   RE-IMPORT (they're derive/fit-side). So: `bash apply` → re-import (or re-run extraction) → the
   real npz then carries the corrected-sign Zout and the fit goes hybrid → re-paste the new
   self-contained report. Confirm pll/vco Zout/noise improve on the REAL re-run (should match the
   local 0.72/0.49 etc.). To get current noise, set `coverage.enable.inoise` (opt-in; the
   `oprobe=<probe>` netlist is box-validate-pending — confirm the box writes the noise output key).
2. **pll PSRR** complex-section initializer (after re-validate; the only thing keeping pll at REVIEW).

## Key files touched
- `cadence/insitu/importmp.py` — `_passivity_sign`, `_derive` (z/couple/noise_i), `_read_noise_out`
- `cadence/insitu/manifest.py` — i_out noise allowlist, `inoise` coverage item, measurements() ni_ point
- `cadence/insitu/run.py` — `_CARRY_KEYS += oprobe_src`
- `cadence/cluster/netlist_augment.py` — `_analysis_line` current-noise oprobe form
- `harness/fit_isrc.py` — `gate`/`_detect_knee`/`_cross_from_top`/`_fit_iv` (keep-best)/`predict_iv`
- `harness/fit_multiport.py` — `_fit_current_largesignal` (knee_side/vhi + noise), `_fit_voltage_output` (hybrid keep-best), `_noise_i_for_sink`
- `harness/current_digest.py` — cPSRR observability gate (`CPSRR_OBS_GATE`, diff_metrics, _diagnose)
- `harness/report_multiport.py` — grade_port cPSRR skip, |Y| bar, current-noise panel, nmode/nfkv threading
- `harness/emit_isrc.py` / `harness/emit_pmu_model.py` — knee side/vhi emit + `current_crow_from_isrc_fit` bridge

## Transcripts (this session, for detail)
- Expert panel (5 agents): the 4 domain evals + Zout-sign code archaeology
- Adversarial verify (5 agents): 4 fix-breakers + completeness critic (it named the hybrid-noise
  next step + the pll-PSRR root cause)
Both under the session's `subagents/workflows/` transcript dir.

See also memory: `redzone-real-ldo-debug-and-selfcontained-report.md` (the persistent index).
