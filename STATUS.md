# STATUS — what's happening now (single source of truth)

> Read this first. Current state + next action only. NO history (that's `docs/archive/` + git).
> Rules: `docs/CONVENTIONS.md`. Durable facts: `docs/reference/`. Deferred work: `docs/BACKLOG.md`.
> Hard cap ~150 lines — if it grows past that, content is in the wrong layer.

## Current focus
**PART2 compressive current-assist — BUILT + VALIDATED locally, UNCOMMITTED.** Fixes the FF "trans goes negative" bug: the deployed minimal model is a pure-LTI Zout that over-predicts a large load step LINEARLY (→ −0.85/−1.924 V); the real loop is class-AB SUB-linear. The assist `iaG·tanh(verr·|verr|/iaV²)` (ODD, f'(0)=0 EXACT → AC/PSRR/noise bit-identical, 0.004 dB) bends the dip to the silicon.
- **DERIVED PURE-PYTHON, NO simulator** (company forbids local Spectre): `harness/fit_iassist.predict_dip` solves the rail's branch-A-ladder+decap+assist as an ODE (LSODA, <1.6 mV vs Spectre both rails); `derive_iassist` fits iaG/iaV to the coverage.transient GT (`tr_*` already in the npz) — folded into `fit_multiport`, ≈5 s, runs box/GUI/CI. PLL ≈4.6 mA/0.44 V (held-out −1.2%), VCO ≈7.2 mA/0.31 V (0.0%). 2 params partly DEGENERATE → gate on dip-RMS+held-out, not the values. Manifest `iassist` = SEED only when no `tr_*` (legacy npz).
- SURVIVES minimal-emit (internal localparams, NO new CDF param — schematic still only `vreg_<rail>`). Backstop `floor` OFF by default (one manifest field to re-enable).
- **VALIDATED vs real silicon**: my .va driven by the REAL idc=300µA iload (`cadence/wur_real_tb/`) tracks real silicon to **6.2 mV RMS** — BUT the replay REQUIRES the 20 pF decap (drive the bare current → 4× too-deep dip; see thread).
- Diagnosis: the trigger is the LOAD STEP, NOT V/T/process (supply 0.85–1.15 V & temp −40–125 °C move it 0 mV; higher vreg is SAFER). "VDD>0.8/FF" only correlates via the real load's current.
**Next: commit + push + `bash apply` + box re-validate** (nothing committed yet). Optional: VCO real-iload replay; T55 z re-export for the ~×0.65 T-confound.
→ details: `docs/threads/large-signal-recovery.md`

## Active threads
| thread | status | next | file |
|---|---|---|---|
| large-signal-recovery | PART2 compressive assist shipped (PLL+VCO); FF-negative bug fixed; AC bit-identical | commit + `bash apply` + box re-validate | `docs/threads/large-signal-recovery.md` |
| wur-pmu-pll-psrr | 4/5 ports USABLE; pll PSRR = lone REVIEW blocker | `bash apply` + box re-validate | `docs/threads/wur-pmu-pll-psrr.md` |

## To find X, read Y
- measured numbers / acceptance thresholds / golden values → `docs/reference/DATA.md`
- Spectre / ngspice / ALPS / Cadence / Verilog-A env quirks + fixes → `docs/reference/TOOL_FACTS.md`
- the modeling method + acceptance gates (incl. what was refuted) → `docs/reference/METHODOLOGY.md`
- deferred / leftover items → `docs/BACKLOG.md`
- how this handoff system works → `docs/CONVENTIONS.md`
- superseded handoffs / old journal (history) → `docs/archive/` (don't load by default)
- personal workflow / preferences → `~/.claude` memory (`MEMORY.md` index)

## Standing reminders
- **Dev on the desk; the box is TEST-ONLY** (`bash apply` deploys; the box has no memory).
- **Auto-push to origin/main after every commit** (direct-to-main; the box pulls).
- **Plan in a normal session; build in a fresh ultracode conversation after an explicit go.**
- When a thread closes: delete its block here + `git mv docs/threads/<it>.md docs/archive/`, in the same commit. Run `bash docs/check_handoff.sh` to catch drift.
