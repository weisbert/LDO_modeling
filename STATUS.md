# STATUS — what's happening now (single source of truth)

> Read this first. Current state + next action only. NO history (that's `docs/archive/` + git).
> Rules: `docs/CONVENTIONS.md`. Durable facts: `docs/reference/`. Deferred work: `docs/BACKLOG.md`.
> Hard cap ~150 lines — if it grows past that, content is in the wrong layer.

## Current focus
**Large-signal load transient / recovery on the WuR PLL rail** — modeled from the standard flow (higher-order LTI Zout). The startup-drop is RESOLVED (TB-side 600→300µA step). **BLOCKED on user decisions** before the next build:
1. Build PART2 (compressive branch-A current-assist) for the GT sub-linear stiffening dip?
2. Is the 88 mV cold-start in scope, or excludable?
3. Confirm the temps of real_V / coverage / z (the 25/55°C confound).
→ details: `docs/threads/large-signal-recovery.md`

## Active threads
| thread | status | next | file |
|---|---|---|---|
| large-signal-recovery | ladder STEP2 + minimal-emit shipped; startup-drop closed | user answers Qs 1–3 | `docs/threads/large-signal-recovery.md` |
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
