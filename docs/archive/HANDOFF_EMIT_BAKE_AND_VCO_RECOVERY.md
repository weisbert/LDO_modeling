# Build spec: FIT La+recovery from AC-Zout (real fix) → bake all params → reorg → GUI move

Plan rewritten 2026-06-29 after the owner's correct critique: hand-tuned `la_override`+`recovery` is a
crutch, not a solution. A real user cannot know La=120µH / Lreg=16µH — those came from MY hand-tuning
PLL against silicon on the local replay loop. They MUST be FITTED from data. Build in fresh ultracode.

Priority order: Part 0 (fit) → Part A (bake) → Part C (GUI move). Part 0 makes VCO automatic (the old
"export VCO real_V / seed" fork is DROPPED).

---

## Part 0 — THE REAL FIX: fit La + the recovery network from the EXISTING AC Zout

WHY this is possible: the recovery network is LINEAR — `Lreg||Rreg` in series with branch-A's Ra path,
plus a `Cs-Rs` snubber from vout→vrg. So in small-signal it is entirely part of `Zout(f)`:
  `Zout = [(La||Rpl) + (Lreg||Rreg) + Ra] || (Rs + 1/sCs) || (ESR + 1/sCout) || (Rb + sLb)`
(en_ls 0-vs-1 is AC-bit-identical → the slew is the ONLY thing NOT in AC; the recovery net IS in AC.)
The data is already measured and CLEAN (not GHz-contaminated): `z_<rail>_<corner>` AC sweep
10Hz–500MHz × 155 pts (see fit report "Zout AC : axis0 10..5e+08, 155pts").

WHAT is wrong today: the current Zout fit (3-branch A/B/C + `shelf gate`) captures the HF shelf but
MIS-READS the low-frequency loop pole → branch-A La comes out ~24µH (≈5× too small; the time-domain
face of the report's "Zout resonance mislocated" caveat). Re-tuning Lreg/Rreg cannot compensate (La is
the dominant recovery-shape lever).

THE FIX: extend the Zout model + fit so it captures the low-freq loop pole AND the recovery poles/zeros
from the 155-pt AC sweep → La, Lreg, Rreg, Cs, Rs all come out FITTED. Keep passivity. Keep the default
(no recovery in the data) byte-identical via the existing opt-in gate.

ACCEPTANCE — PLL is the oracle (we KNOW the answer from the replay-validated 2.47mV tune):
- Fitting PLL's recovery PURELY from its AC Zout must recover La ≈ 1.2e-4 H and a recovery net close to
  {Lreg 1.6e-5, Rreg 750, Cs 2.5e-11, Rs 2.0e3}, AND `cadence/wur_real_tb/replay_pll.py` must stay
  ~2.47mV RMS vs silicon.
- IF it recovers ~120µH from AC alone → AC Zout is sufficient; VCO gets recovery for FREE (no real_V,
  no hand values). Ship it.
- IF it still gives ~24µH → there is a genuine small-signal-vs-large-signal gap. Then the principled
  fallback is a CLEAN load-step characterization waveform (declared in the manifest, auto-fit from it) —
  NOT a hand guess. Document which path won.

DEMOTE the manifest knobs: `la_override` / `recovery` become rarely-used ESCAPE HATCHES only (override
the fit when present). Keep the same opt-in gate discipline; default (absent) = use the fitted values.

VCO: with Part 0 working, VCO recovery is just the fit running on VCO's AC Zout. No manifest edit, no
real_V export, no seed. (The earlier Part B is REMOVED.)

---

## Part A — bake ALL FITTED params → `localparam` (no CDF exposure), then reorg
After Part 0, ALL values to bake are FITTED (vreg, Zout incl La+recovery, slew, PSRR, noise, idc).
Convert every FITTED `parameter real` emission in emit_pmu_model.py to `localparam real` (values/
equations unchanged). Sites (current line nos):
- `216` `{pre}_vreg` · `257` `{pre}_Cft` · `273` `{pre}_SRa` · `339` `{pre}_en_ls`
- `344-356` `{pre}_Lreg,_Rreg,_Cs,_Rs,_Imax,_Vcl,_Gcl` · `647` `{pre}_idc55`
KEEP `parameter real` ONLY for `iload_{pre}` (206/525) — it is a runtime INPUT (the load), not a fit,
and it isn't emitted by the real single-OP model anyway; comment the exception.
Fix the baked-vreg probe at `emit_pmu_model.py:777` (`parameter real {o}_vreg` → `localparam real`),
and any sibling greps in report_multiport.
Result: the cell has NO CDF params → instance can't shadow the fit → re-emit always wins.
BOX STEP after re-emit: `ahdlUpdateViewInfo("<lib>" ?cell "WuR_PMU_TOP_model" ?view "veriloga")`
(skartistref.pdf p.600) to drop orphaned CDF params + stale instance overrides. Tune by editing the
`localparam ... = value;` lines in the veriloga directly.

REORG (pure formatting): group each rail's decls + initial_step under section headers in order:
DC setpoint / Zout branches A-B-C / large-signal slew (SRa,en_ls) / recovery network (Lreg,Rreg,Cs,Rs)
/ anti-windup clamp (Imax,Vcl,Gcl) / PSRR / output noise. Current biases: one labeled block per sink.

---

## Part C — GUI: move recovery+la_override OUT of the trans dialog → a v_out OVERRIDE group
Today they live buried in `_open_trans_editor` (gui/ldo_modeler.py:2236, the "Transient load steps
(slew)" dialog) and only render when the cell resolves to a rail — that's why the owner couldn't find
them, and it's the wrong place (rail-MODEL params inside a transient-TEST dialog).
Move `Slew override / La override / Recovery network {Lreg,Rreg,Cs,Rs}` into the v_out rail editor as a
clearly-labeled **"Large-signal overrides (blank = use fitted)"** group. Keep the lossless per-table
store discipline (`_slew/_la/_recov`), the all-four-or-off recovery rule, and collect()/load round-trip.
After Part 0 these are escape-hatches, so label them as overrides, default blank.

---

## Validation (local, Spectre 18.1 at desk)
1. Part 0 oracle: PLL recovery fitted from AC Zout → La≈120µH + replay ~2.47mV (the gate).
2. `pytest harness cadence/insitu -q` green (re-bless byte/param tests for the localparam change;
   keep semantic guards: off→on adds only recovery/slew lines, DC holds vreg, AC/PSRR/noise unchanged).
3. Local Spectre -64 compile of the new .va: DC 0.8V, transient stable+monotonic, AC bit-identical
   old(parameter)-vs-new(localparam).
4. Emitted cell has zero `parameter real` (except iload on the scheduled path).
5. GUI selftest PASS; recovery overrides round-trip from the new v_out group.
