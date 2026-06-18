# HANDOFF — complete the pure-CLI Donau+ALPS workflow (Path B, full sweep)

> **✅ GUI-WIRED 2026-06-18 (commit `052cf5b`, main).** The full sweep now RUNS from the GUI
> (no CLI). Extract tab: Run-on=cluster + **Build & Run** → `_ClusterSweepWorker` (QThread) →
> `ExtractCore.run_cluster_sweep` (offline factory → `step_run` real Donau → `step_import` →
> multi-port fit; sets `self.result` so **Create model cell** works after). LIVE status = a
> **per-group table** (#, group, analysis, state with colour: pending→running→done/failed) +
> progress bar; a **"Preview only (dry-run)"** checkbox lists the per-group dsub commands without
> submitting. `pmu_corner.step_run` gained structured `group_status(i,n,group,state)` + a
> cooperative `cancel` (between groups). 130 backend tests + GUI `--selftest` PASS.
>
> **DATA LANDS AT** (`pmu_corner.corner_dir`): `$WORK_ROOT/ldo_modeling/<tb_lib>__<tb_cell>/
> <corner>/{netlist/<g>/input.scs, psf/<g>/, npz/<name>_<corner>.npz, model/<cell>.va}`.
> `$WORK_ROOT` = env `WORK_ROOT` else `~/ldo_workarea`. NEVER the designer spine
> `$WORK_ROOT/simulation/<Lib>/<Cell>/`.
>
> **NEXT = BOX VALIDATION (designer-driven, the only thing untested off-box) + deploy:**
> (a) deploy: `package.ps1 -Mode incremental` → red zone `bash apply` (ships 297cb1f + 052cf5b;
>     no dep change — code-only);
> (b) in the GUI: get a RESOLVED manifest (Mode A resolves pins→manifest, or load a resolved one
>     — wur_pmu_top.json ships `<net:...>` placeholders the guard refuses); Mode B → point
>     **Netlist dir** at the maestro base `input.scs` (Create Netlist, no run); PDK=$MODEL_ROOT;
>     engine=ALPS; Run-on=cluster; **untick Preview** → Build & Run;
> (c) watch the 8-group status table walk pending→running→done; confirm the npz lands + the fit
>     report renders + **Create model cell** lights up;
> (d) box-only unknowns to watch: real ALPS PSF for the wur **2 v_out + 3 i_out** topology read
>     back by `importmp`; analysis-strip / supply auto-detect on the REAL maestro netlist (other
>     maestro idioms beyond the handled `\` continuations); supply `tb_src` if auto-detect is
>     ambiguous (set `supplies.avdd1p0.tb_src`).
>
> **OPTIONAL GUI NICETY (offered, not yet built):** an "Open results dir / show full npz path"
> button on the Extract tab (today only the npz FILENAME is shown in the status bar). Small.
>
> ---
>
> **✅ BUILT 2026-06-18 (commit `297cb1f`, main).** The gap below is CLOSED. The offline
> (no-ADE) per-group netlister is `cadence/cluster/netlist_augment.py`
> (`make_offline_group_netlister`); `run_pmu_corner` gained a `manifest=` injection; the
> entrypoint is `python -m cluster run-sweep --manifest <name> --base-netlist <dir> --pdk
> $MODEL_ROOT --engine alps [--dry-run]`. 128 tests pass offline. Backslash line-continuations
> (maestro formatting) are handled. **ONLY REMAINING = box validation + deploy:**
> (1) `package.ps1 -Mode incremental` → red zone `bash apply`;
> (2) RESOLVE the manifest nets first (wur_pmu_top.json ships `<net:...>` placeholders — the
>     netlister's guard refuses them; run Mode A in Virtuoso or hand-edit `net` fields);
> (3) supply PSRR source: `avdd1p0` has no `tb_src` → auto-detect finds the lone vsource on the
>     net, or set `supplies.avdd1p0.tb_src` if ambiguous;
> (4) `--dry-run` should print 8 per-group dsub commands; a real run lands the npz → fit gate.
> The historical brief below is kept for context.

Date: 2026-06-18. Branch `main`. HEAD at handoff: `dd19aab`.
**Goal of the next session:** make the full measurement sweep run end-to-end on the cluster
via **pure dsub+ALPS (no ADE / no skillbridge)** — manifest → per-group one-hot netlists →
submit/poll all groups → collect PSF → npz → fit. Today a SINGLE-job smoke runs and the whole
orchestrator exists, but the **per-group netlist for the pure-CLI path** is the remaining seam.

**Working mode:** build in a fresh **ultracode** conversation, on explicit go. Box-validation
(real dsub/alps) only happens on the red zone — the dev box has no cluster, so build to be
box-runnable + dry-run-testable, and the designer smokes it on the box.

---

## What ALREADY EXISTS (do NOT rebuild — this is the surprise: most of it is done)

The full one-corner orchestrator is `cadence/insitu/pmu_corner.py` (STEPS line 66 = resolve,
manifest, augment, netlist, run, import, fit, emit, cell). Verified this session:

- **Per-group SWEEP is built:** `pmu_corner.step_run` (line 251) loops `run.groups(m)` (8 groups),
  calls `cluster.run_corner` per group, and builds **`psf_map` keyed BY MEASUREMENT TAG** (every
  member point of a group → that group's PSF dir). Has `dry_run` (assembles all per-group dsub
  cmds without executing). 14→8 decomposition = `insitu/run.py` `groups(m)` (line 31).
- **The per-group netlist is an INJECTABLE SEAM:** `step_run(..., group_netlister=…)` —
  `group_netlister(group) -> netlistdir` (each holding ONE acm one-hot's input.scs). THIS is the
  one plug point that matters.
- **Single-job driver + CLI smoke (dd19aab):** `cluster.run_corner` + `python -m cluster
  --netlistdir … --out … --pdk $MODEL_ROOT --engine alps [--dry-run]`. Lets the designer prove
  one real dsub+alps job on the box NOW.
- **Read/fit side READY:** `importmp` takes the `psf_map={tag: dir}` and derives every contract
  array in Python (npz firewall); steps import→fit→emit→cell are wired in pmu_corner.
- **Submit/poll + engine cmd:** `cluster/donau.py`, `cluster/alps_cli.py`. 48+ tests green offline
  (injected fake runner). Reference log: `cadence/ALPS_DONAU_NOTES.md` + `PMU_CORNER_RUNBOOK.md`.

## THE GAP — one real piece + an entrypoint

**1. An OFFLINE (no-ADE) `group_netlister` = a netlist-text augmenter.** Today the only real
   netlister is **ADE-based**: `ade_group_netlist` / `_live_group_netlister` (pmu_corner.py:188 /
   227) reuse the run_ade wiring to set the one-hot + enable-only the analysis, then **netlist via
   the SKILL helper `insituNetlistTest`** (a documented STUB) and copy `input.scs` per group — that
   needs skillbridge. For PURE CLI, build a `group_netlister` that needs **no Virtuoso**: given ONE
   base `input.scs` + the manifest, emit per-group one-hot `input.scs` by text:
     - declare `parameters acm_*=0` and APPEND the acm sources/probes at the manifest nets — the
       Spectre-netlist equivalent of `augment.build_plan(m)` (augment.py:43): isource at each
       v_out net, named probe vsource at each i_out net (`dc` + acm), supply-acm at each supply's
       `tb_src`;
     - set THIS group's hot acm var = 1 (`manifest.acm_var`), all others 0;
     - REPLACE the base analysis with the group's `ac`/`noise` line (`m['analysis']`) — the base
       maestro netlist is a `.tran` TB, so strip/replace, don't append;
     - emit the group's targeted SAVE set (union of `group['members'][].save`; noise → the group
       `oprobe`).
   New module e.g. `cadence/cluster/netlist_augment.py`; it plugs straight into the `group_netlister`
   seam — the orchestrator/import/fit downstream are untouched.

**2. Entrypoint that drives it with a REAL Donau runner.** A `python -m cluster run-sweep
   --manifest … --netlistdir <base> --pdk $MODEL_ROOT --out <workarea>` (extend the new CLI) that
   calls `pmu_corner.run_pmu_corner` / `step_run` with the offline netlister (#1) + a real
   `donau.SubprocessRunner`, prints per-group pending→running→done, and lands the npz. And/or wire
   the GUI Tab-0 **Build & Run** cluster branch (`gui/ldo_modeler.py` `_x_run` 1962 /
   `_x_cluster_preview` 1994) to call this instead of previewing, progress via run_corner `on_status`.

## Gotchas / decisions for the next session
- **Base netlist:** pure-CLI → designer hands the maestro `input.scs` (a `.tran` TB). Decide
  analysis-swap robustness (regex vs a small Spectre-stmt parser). Confirm acm_* can be appended at
  top level (nets are global in the flattened TB).
- **Supply PSRR needs `supplies.<k>.tb_src`** (the TB supply vsource to AC-modulate). `wur_pmu_top.json`
  has none → blocks `g_supply_avdd1p0`. Capture it (add `tb_src` to the manifest, or locate the
  vsource on the supply net in the netlist) EARLY.
- **PDK arg = DIR** `$MODEL_ROOT` (→ `-I $MODEL_ROOT/alps`); the `toplevel.scs` FILE is wrong
  (→ `-I …/toplevel.scs/alps`). Confirmed via the run_corner dry-run.
- **Storage contract:** write `$WORK_ROOT/ldo_modeling/<Lib>__<Cell>/<corner>/{netlist,psf,npz,model}`
  (pmu_corner.corner_dir, line 82). NEVER the designer spine `$WORK_ROOT/simulation/<Lib>/<Cell>/`
  — only READ the base input.scs from there.
- **Concurrency:** step_run currently submits+polls per group in the loop; for 8 groups consider
  concurrent submit then poll-all (donau.poll is per-job) if queue latency hurts.
- **ADE-netlister alternative:** if a little ADE-just-to-netlist were acceptable, finishing the
  `insituNetlistTest` stub (box research) reuses `ade_group_netlist` and skips #1. The designer
  chose pure-CLI, so #1 (offline netlister) is the deliverable; keep ADE as the documented fallback.

## Verify
- Dev: `cd cadence && python3 -m pytest cluster/ insitu/ -q` green; add unit tests for the offline
  netlister (golden per-group input.scs) and a step_run path that injects it (asserts 8 dsub cmds +
  the psf_map). `python3 -m cluster … --dry-run` prints correct per-group commands.
- Box: `python -m cluster run-sweep …` → 8 jobs pending→running→done → npz → fit gate PASS; read
  back one real PSF to confirm importmp parses the live ALPS output.
