# BACKLOG — deferred / leftover / future items

> One line per item: what + why + pointer. When picked up → promote to `docs/threads/<topic>.md` and remove here.
> Active work is NOT here — it's in STATUS.md / threads/. This is the "later" pile.

## Methodology / model gaps (the audit's standing items)
- **[BLOCKER] System-level test as the TOP score** — LDO+buffer drawing periodic carrier current on vout, tran+.HB, GT vs model carrier±Δ COMPLEX sideband (mag AND phase). `systest.py` exists (Round B); PSS/HB drop-in + making it the top scorer is open. The only test that measures the actual deliverable. (ref: METHODOLOGY §Validation)
- **[BLOCKER] Carrier-band validity envelope** — extend Zout/PSRR char to carrier±widest sideband at every corner; emit a HARD envelope that REFUSES to extrapolate above the ceiling (today it silently rolls to ~0). The 500 MHz nominal ceiling is ~12× below the 5.8 GHz carrier.
- **[MAJOR] Promote LOCO + a 4th off-grid load corner (49µ/174µ) into the scorecard** with an acceptance gate (held-out ≤ ~2× in-sample). The model fails it today; the LOCO interp gap is a fundamental few-corner limit (needs more corners or physically-constrained per-corner params).
- **[MAJOR] Identifiability gate** — cond(J)/σ per param per corner; freeze any param with σ<1e-3·σmax instead of interpolating noise; distinguish invisible params from harmful switches (R_pl ON/OFF).
- **[MAJOR] VDD axis (R3-L2)** — small-signal Zout/PSRR/noise at ≥2 vin + line-reg slope; wire the unused `dc_linereg` into the DC anchor so the rail tracks supply. Needs per-supply-corner exports.
- **[MAJOR] Use-case-resolved scorecard** + make kernel-Zout phase a pre-shell acceptance gate (not a low-weight term magnitude can outvote).
- **[ARCH] Series-L in the Cout branch (Cout-Resr-Lesl)** — closes v7_esl inductive tail, helps v8_dlc notch + v10_3lc. The one clean extension Round 2 pointed to.
- **[minor] One PSRR/Zout model order per variant** (or AIC/BIC); relabel the "decoupled" noise + score Sv vs GT end-to-end.

## Large-signal step / load (see thread: large-signal-recovery)
- ~~**PART2 compressive branch-A current-assist**~~ — **DONE** (PLL+VCO; `emit_pmu_model._iassist_*`, `fit_multiport._fit_iassist`, manifest override, `test_iassist_core.py`). Fixes the FF "trans negative" bug; AC bit-identical.
- ~~**B5 guarded transient fit on the VCO**~~ — **DONE** with PART2 (iaG=4.0 mA, iaV=0.22 V; rms 0.9 mV vs GT).
- **[MINOR] UNLOAD overshoot-above-supply** — a hard load-removal step (e.g. 12 mA→0.5 mA) makes the rail overshoot ABOVE the supply (branch-A fit-inductor kick: old +2.8 V, assist +2.0 V; present even in-envelope at ~1.1–1.4 V > 0.98 V supply). Bounded + settles to vreg (not a runaway), and the assist already halves it, but it's non-physical (an LDO output can't exceed its supply). The `floor` backstop is one-sided (low only) so doesn't touch it. Fix if hard-unload robustness matters: a high-side clamp at the supply, or a symmetric large-signal current cap on branch-A. Found by the 2026-06-30 VDD-sweep stress test (`78f5a7a`).
- **(optional) T55 z_pll re-export** — kills the ~×0.65 T-confound the assist gain currently absorbs (AC z=tt_25c vs step GT=tt_55c).
- **PART3 / 88 mV cold-start** — OUT OF LOCAL SCOPE; needs a box turn-on/EN characterization.
- **R1 de-hardcode large-signal step magnitudes** (trans_big=1mA/trans_slew=5mA) → profile-driven fractions of imax (2 places must agree: gen_reference GT + score re-sim).
- A same-temp 55°C z_pll re-export to rule out the 25/55°C confound on the dip.

## Current-output model (see DATA §7, METHODOLOGY §Current-output)
- Wire fit_multiport to produce large-signal fields; consume iv_sweep/temps; thread supply_dc/tnom_c into meta.
- G9 coupling verify-first (n² cross-admittance only if confirmed); G6 corner-family .lib (round-2); populate iport_<pin> real refs.
- N-supply-input model cell: emit PSRR vs ALL supplies (today only the first, loud-warned).
- EXT-1: `isrc_vbias_noise` 5th current-path gate (flicker corner walks with Vo).
- EXT-2: productionize the 5 observational gates INTO the verdict — BLOCKED on the v8_wilson 139 mV B4 decision (extend the model with a T-dep knee DOF, or accept as a known limit).
- FIX-1: a clean-blind B1 (collinear CTAT+PTAT) — documented fallback, optional retry. §E alt DUTs: ldo_ghostcap, ldo_rhpz, isrc_softknee_subthreshold.

## Orchestration / cluster
- **DONAU_ALPS box validation** — set the supply tb_src (grep `vsource` on AVDD1P0), real run, try Max-parallel=4; resolve the pending "manifest change requirement"; harden `_detect_supply_src` if AVDD1P0 isn't the first node.
- RESUME: skip groups whose PSF is already on disk (don't re-submit completed cluster jobs).
- Per-group netlists for a real Mode-B cluster sweep; wire `insituNetlistTest` (asiNetlist/sevNetlistFile via the axlGetToolSession→asiGetSession bridge).
- Targeted saves on the real PMU (ADE-L `sevSaveOptions`); the ALPS `sev` field name for `values=[...]`.
- `vin_sweep`/line-reg DC coverage kind (none exists); make the bias ports actually hold a DC (currently inert).

## Red-zone deliverable (see thread: wur-pmu-pll-psrr)
- **pll PSRR** — robust complex-section initializer for sparse silicon AC (the lone REVIEW blocker; 4/5 ports already USABLE).
- 2nd-order Idc(T) needs a ≥5-temp box run to exercise curvature on silicon.
- Upgrade the report Zout grade to a SHAPE gate (peak-freq + per-decade, not broadband RMS).
- `v3_miller` synthetic hot-corner fix (regulation breaks at 125°C) before its 125°C noise ref is usable.

## GUI / housekeeping
- Regenerate the other `model/*.va` (esp. v4_ffpsrr) from the PSRR-sign-fixed `emit_va`.
- Build a Spectre intrinsic-spur characterizer (PSS / transient-FFT) → enables v5/v6 + the real 304 MHz.
- Rip out the dormant slew/la/recovery store plumbing; option to skip smoke screenshots (~3–4 s).
- GUI: "Open results dir / show full npz path" button on the Extract tab.
- Multi-supply 2nd PSRR path if the 0.8 V LDO also takes the 1.8 V supply.

## Shipped — pending `bash apply` + box re-validate
These are DONE locally; they just need a deploy + a real-box confirmation (no new build):
coverage-modeling · current-model (TURN 1–10) · ade-backend (1610/1707) · manifest-editor (3-block) · parallel-sweep-scheduler · status-tree per-cell · in-situ Report tab · GUI-UX/PSF batch · split-ground export · unified-source-reuse · pmu-cli-corner · manifest-edit-ux · deploy-smoke fast-noise · Zout ladder STEP2 (ab21b83) · minimal-emit (6833c61).

## Refuted — do NOT re-attempt
See METHODOLOGY §"Refuted — do NOT re-attempt" (two-notch PSRR, neg-Re Zout, B1 clean blind-spot, fitter-layer multi-pole PSRR fix, joint-LS Cout/ESR, branch-A slew, fitting to real_V).
