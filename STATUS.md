# STATUS — what's happening now (single source of truth)

> Read this first. Current state + next action only. NO history (that's `docs/archive/` + git).
> Rules: `docs/CONVENTIONS.md`. Durable facts: `docs/reference/`. Deferred work: `docs/BACKLOG.md`.
> Hard cap ~150 lines — if it grows past that, content is in the wrong layer.

## Current focus
**PART2 compressive current-assist — SHIPPED (`78f5a7a`), pending `bash apply` + box re-validate.** Fixes the FF "trans goes negative" bug: the deployed minimal model is a pure-LTI Zout that over-predicts a large load step LINEARLY (→ −0.85/−1.924 V); the real loop is class-AB SUB-linear. The assist `iaG·tanh(verr·|verr|/iaV²)` (ODD, f'(0)=0 EXACT → AC/PSRR/noise bit-identical, 0.004 dB) bends the dip to the silicon.
- **DERIVED PURE-PYTHON, NO simulator** (company forbids local Spectre): `harness/fit_iassist.predict_dip` solves the rail's branch-A-ladder+decap+assist as an ODE (LSODA, <1.6 mV vs Spectre both rails); `derive_iassist` fits iaG/iaV to the coverage.transient GT (`tr_*` already in the npz) — folded into `fit_multiport`, ≈5 s, runs box/GUI/CI. PLL ≈4.6 mA/0.44 V (held-out −1.2%), VCO ≈7.2 mA/0.31 V (0.0%). 2 params partly DEGENERATE → gate on dip-RMS+held-out, not the values. Manifest `iassist` = SEED only when no `tr_*` (legacy npz). (`78f5a7a` also fixed a Path bug: `gt_dips_from_npz`/`derive_iassist` only accepted `str`, so a `pathlib.Path` npz — the GUI run path + emit/fit CLIs — silently fell back to the seed; now `os.PathLike` via `_load_npz`, regression-tested.)
- SURVIVES minimal-emit (internal localparams, NO new CDF param — schematic still only `vreg_<rail>`). Backstop **`floor`=0.0 ON** (both rails) — a one-sided clamp that engages only below 0 V (zero value+slope above → invisible to DC/AC/the validated regime), carried onto the DERIVED assist (not just the seed). A far-beyond-iaG abuse step (>~10 mA, where the saturated assist can't hold) drops to ~0 (dropout) instead of a non-physical negative — verified 10/12 mA → −0.6/−1.3 mV.
- **VALIDATED vs real silicon**: my .va driven by the REAL idc=300µA iload (`cadence/wur_real_tb/`) tracks real silicon to **6.2 mV RMS** — BUT the replay REQUIRES the 20 pF decap (drive the bare current → 4× too-deep dip; see thread).
- **STRESS-TESTED** (local Spectre, full PMU .va, VDD setpoint 0.75–0.95 V, old/new/+floor): no runaway anywhere — every excursion SETTLES back to vreg (no "stuck 10 V/−2 V"). In-envelope (≤4 mA) the new model is healthy; at abuse ~12 mA (the bug regime) the PLL dip goes −1.1 V→−0.39 V (floor → ≥0). NEW FINDING: a hard UNLOAD overshoots ABOVE the supply (branch-A fit-inductor kick: old +2.8 V, assist +2.0 V; floor is one-sided so doesn't help) — present even in-envelope; logged in BACKLOG.
- Diagnosis: the trigger is the LOAD STEP, NOT V/T/process (supply 0.85–1.15 V & temp −40–125 °C move it 0 mV; higher vreg is SAFER). "VDD>0.8/FF" only correlates via the real load's current.
**Next: `bash apply` + box re-validate** (the box must regenerate the .va WITH the assist, not the stale minimal box.va). Optional: VCO real-iload replay; unload overshoot clamp; T55 z re-export for the ~×0.65 T-confound.
→ details: `docs/threads/large-signal-recovery.md`

## Active threads
| thread | status | next | file |
|---|---|---|---|
| large-signal-recovery | PART2 compressive assist committed (`78f5a7a`); FF-negative bug fixed; stress-tested; AC bit-identical | `bash apply` + box re-validate | `docs/threads/large-signal-recovery.md` |
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
