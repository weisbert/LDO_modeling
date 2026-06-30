# Handoff system — conventions (read this once, then follow it)

> This file is the SPEC for how this project records state, knowledge, and history.
> It is stable; it does not carry status. If you change the system, change it here.

## The one rule that prevents context loss
There are **three kinds of writing**, and they must never be mixed into one append-only pile
(that is what rotted the old `HANDOFF.md`/`MEMORY.md`):

| Kind | Question it answers | Mutability | Lives in |
|---|---|---|---|
| **STATE** | What's true *now* / what's next? | mutable, pruned, capped | `STATUS.md` (+ `docs/threads/*.md`) |
| **FACTS** | Durable knowledge: numbers, methods, tool quirks, *why* | append/curate-only, atomic, **no status** | `docs/reference/*.md` |
| **HISTORY** | The changelog / superseded threads | frozen, out of hot path | `docs/archive/*` + git |

Corollary rules:
- **STATUS never carries history.** When a thread closes, **delete** its block from STATUS and
  `git mv` its thread file to `docs/archive/`. Do this in the *same commit* that closes the work.
- **reference/ never carries status.** No "as of", no "next session", no commit-hash changelogs.
- **No supersede chains in the live set.** A superseded doc is *moved to archive*, not annotated in place.
- Every live doc's first line says **what it is + whether it's live**.

## File map
```
CLAUDE.md                 # auto-loaded anchor. Points here + to STATUS.md. Keep tiny.
STATUS.md                 # ★ single entry point. Current focus + next action + active-thread index
                          #   + "to find X, read Y" pointer table. HARD CAP ~150 lines. No history.
docs/
  CONVENTIONS.md          # this file (the rules)
  BACKLOG.md              # deferred / leftover / future items (one line each + pointer)
  reference/              # ★ durable knowledge — curate-only, no status
    DATA.md               #   measured numbers, acceptance thresholds, golden values (tables)
    TOOL_FACTS.md         #   Spectre/ngspice/ALPS/Cadence/Verilog-A env quirks + exact fixes
    METHODOLOGY.md        #   the modeling method + acceptance gates; what works AND what was refuted
  threads/               # one file per ACTIVE thread (the unit of concurrent work)
    <topic>.md           #   working surface for that thread; checklist of its TODOs
  archive/               # superseded handoffs / old journals — never loaded by default
```

## STATUS.md shape (keep it this short)
1. **Current focus** — the one active thread + its single next action.
2. **Active threads** — one line each: `topic — one-line status — → docs/threads/<topic>.md`.
3. **Pointer table** — "to find X, read Y" (numbers→DATA.md, tool quirks→TOOL_FACTS.md, method→METHODOLOGY.md, deferred→BACKLOG.md).
Nothing else. If it grows past ~150 lines, content is in the wrong layer.

## Concurrent terminals (multiple sessions at once)
- **The unit of concurrent work is a thread file** `docs/threads/<topic>.md`. One terminal owns one
  thread file → no write conflict.
- **STATUS.md is a thin index**: each active thread is ONE line. Rule: *edit only your own thread's line*.
  Different lines merge cleanly in git.
- Before starting work, **claim the thread**: add/own its line in STATUS and its thread file. Two
  terminals on the *same* thread is a human-coordination problem — claim first.
- Each thread file header carries `owner:` and `last-touched:` (fill with `date`) so it's obvious who's on it.
- Discipline: `git pull --rebase` before editing; commit small.

## TODO / leftover items
- **Inside the current thread** → checklist in that thread's file.
- **Cross-thread / "later"** → one line in `docs/BACKLOG.md` (with context + a pointer to the source).
- Picked up → promote to a thread file, remove from BACKLOG.

## Memory (`~/.claude/.../memory/`) — desk-local, NOT versioned, NOT on the box
- Memory holds **durable cross-session facts only** (tool quirks, user preferences, validated method).
  Current-task status does NOT go in memory — it goes in `STATUS.md`.
- One fact per file; the `MEMORY.md` index is **one line per file** (it is truncated on load if over its
  size cap → keep it small, or every session starts blind).
- A single memory `project-pointer` says: "current state lives in repo `STATUS.md`; rules in `docs/CONVENTIONS.md`."

## Size caps (run `docs/check_handoff.sh` — fails loud if exceeded)
- `STATUS.md` ≤ ~150 lines
- `MEMORY.md` index ≤ 24 KB (Claude load cap)
- any single memory file ≤ ~8 KB (bigger = it's a handoff, not a fact → split or move to STATUS)

## When in doubt
Ask: is this STATE (now), a FACT (durable), or HISTORY (past)? Put it in that layer. Never the pile.
