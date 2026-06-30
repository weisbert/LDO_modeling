# LDO_modeling — start here

Fast, PSS/HB-robust **behavioral model** for an LDO / PMU (per-rail Zout + PSRR + Norton noise + spurs;
current sinks: I-V + admittance + PTAT + current-noise), fit from Spectre/ALPS ground truth and emitted as
`.lib` / `.va`. See `README.md` (build/run) and `PROJECT.md` (overview).

## Read order for every new session (prevents context loss)
1. **`STATUS.md`** — the single source of truth for *what's happening now and what's next*. Read it first.
2. **`docs/CONVENTIONS.md`** — how this project's handoff system works (state vs facts vs history). Follow it.
3. Durable knowledge, by need:
   - numbers / acceptance thresholds / golden values → **`docs/reference/DATA.md`**
   - Spectre / ngspice / ALPS / Cadence / Verilog-A env quirks → **`docs/reference/TOOL_FACTS.md`**
   - the modeling method + acceptance gates (incl. what was refuted) → **`docs/reference/METHODOLOGY.md`**
   - deferred / leftover items → **`docs/BACKLOG.md`**
   - superseded handoffs / old journal → `docs/archive/` (history; do not load by default)

## Invariants that must never be lost
- **Dev happens on the desk** (Spectre 18.1 local, ngspice, full `~/.claude` memory). The **red zone / box is
  TEST-ONLY**: code reaches it via `bash apply`; the box does NOT do development and does NOT have memory.
  → durable docs live in this repo (versioned), not only in memory.
- **Do not append-only.** When a thread closes: delete its STATUS block, `git mv` its `docs/threads/*` to
  `docs/archive/`, in the same commit. Never grow a 80KB journal again.
- Working mode, auto-push, and other personal preferences live in `~/.claude` memory (`MEMORY.md` index).
