# Thread: WuR PMU red-zone deliverable — pll PSRR blocker — PARKED (awaiting box re-validate)

owner: weisbert · last-touched: 2026-06-30 · distilled from archived HANDOFF_REDZONE_LDO_FIXES

## Where it stands
First real PMU (WuR_PMU_TOP, 2 rails + 3 sinks). **4/5 ports USABLE** (VDD0P8_VCO + the 3 current sinks).
All four red-zone fix families (#1–#4) closed: Zout shelf-fit, PSRR 3-real polish, log-amplitude noise + |Y| pole-zero + I-V knee, cPSRR observability gate, current-noise wiring. Grades + before/after: DATA.md §5.

## The lone blocker
- **VDD0P8_PLL PSRR = REVIEW** (5.85 dB mag). Root cause: the AAA complex-section initializer FAILS on the sparse ~47-pt silicon AC sweep (complex resid 3.16 > shelf 1.06, phase RMS ~51°). It is NOT non-min-phase (Hilbert artifact). Needs a robust complex-section initializer for sparse real AC (`fit_model._bank_fit` / `_aaa_conj`).

## Next action
- [ ] `bash apply` on the box, then re-validate (the shelf/PSRR fixes land; expect pll Zout 1.84→1.69).
- [ ] If pll PSRR still REVIEW → build the robust sparse-AC complex-section initializer.
- [ ] 2nd-order Idc(T) needs a ≥5-temp box run to exercise curvature on silicon.
- [ ] Upgrade the report Zout grade to a SHAPE gate (peak-freq + per-decade, not broadband RMS).

Note: under minimal-emit (see large-signal-recovery thread) the old "pll Zout 1.84→1.69 awaiting bash apply" may be moot — re-check after deploy.
