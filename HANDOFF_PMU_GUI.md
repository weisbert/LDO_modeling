# Next task — the GUI front-end for one-corner PMU LDO modeling (the acceptance interface)

**Why this exists:** the user's acceptance was always *"open the GUI in the red zone, type the
inputs, get the modeling result."* The 2026-06-15 ultracode session built the whole **backend**
(pure-CLI Path-B: resolve → manifest → per-group sweep → import → fit → emit → cell) + a CLI entry
(`python -m insitu.pmu_corner`) and pushed it (`dd5f023`), **but did NOT wire it into the GUI** —
the GUI front-end was dropped. That front-end is THIS task.

## What's already DONE (backend, pushed @ dd5f023, 92 offline tests green)
The full Path-B backend + the model-cell pieces — all built, adversarially reviewed, tested:
- `cadence/cluster/` — Donau+ALPS CLI driver (engine-param, dsub submit + djob poll pending/
  running/done). `cadence/insitu/resolve.py`+`skill/resolve_nets.il` — symbol-pin→net.
  `cadence/insitu/build_manifest.py` (+`manifests/pmu_real.json`) — GUI inputs → manifest.
- `harness/emit_pmu_model.py` + `skill/pmu_top_symbol.il::pmuBuildModelCell` +
  `skill/ldo_cellview.il::ldoCompileVA` — ONE combined Verilog-A (1-in/6-out/VSS, ports = GUI
  pin names) + auto-symbol (VSS bottom) + compile.
- `cadence/insitu/pmu_corner.py::run_pmu_corner` — the 9-step orchestrator. step 5 is the
  **per-group SWEEP** (`run.groups(m)` → ~N jobs, each one `acm_*` one-hot + its analysis);
  `psf_map` keyed BY TAG → `importmp.from_psf_multiport` → `fit_multiport` → `emit` → `step_cell`.
- Full intel: `HANDOFF_PMU_CLI_CORNER.md`, `PMU_CORNER_RUNBOOK.md`, memory `next-pmu-cli-corner`.

## The GUI gap (THREE pieces to wire into `gui/ldo_modeler.py`)
The GUI already has a **"0 · Extract (in-situ)"** tab — `ExtractCore` (manifest→augment→run→npz→
fit via `insitu.cli.produce_npz(manifest, backend, session, ...)`), `_ExtractWorker` QThread
(progress/cancel), `_ManifestEditorDialog`, and `.va` emit on Tab 3. Missing for the acceptance:

1. **Simple INPUT FORM** (instead of hand-editing a manifest JSON): fields for TB lib/cell/view +
   DUT instance; the 3 voltage output pins (`VDD0P8_DIG/PLL/VCO`); the 3 current output pins
   (`IBP_POLY_1P8U_VCO / IBP_POLY_500N_VCO_Fit / IBP_PTAT_TUNE_1P5U_VCO`); the supply input pin
   (`AVDD1P0`); per-i_out compliance `vdc` (explain the V/I asymmetry); target model lib/cell/path.
   On "Resolve + Build": `resolve.resolve_nets` (skillbridge) → `build_manifest.build_manifest`
   → show the summary + surface `m['_warnings']` (missing i_out compliance) prominently.
2. **RUN with progress** (pending/running/done in the existing progress bar). DEFAULT engine =
   **Path A (`ade` / `run_ade`)** — it works end-to-end TODAY. Add **Path B (cluster /
   `run_pmu_corner`)** as a SELECTABLE engine, but it will error at the `insituNetlistTest` stub
   until that box-validation item is closed (RUNBOOK §8) — label it so. Reuse `_ExtractWorker`.
3. **MODEL-CELL BUILD** (deliverable 3): after fit, a "Create model cell" action taking the
   target lib/cell/path → `pmu_corner.step_cell` (= `ldoEnsureLib`+`ldoImportVA`+`ldoCompileVA`+
   `pmuBuildModelCell` over skillbridge), input LEFT / output RIGHT / VSS BOTTOM, compiled.
Then **repackage** via `deploy/package.py` → bundle for the red zone (`update.sh`/`run_gui`).

## Decisions to confirm at the START of the next session (blocking)
- **Red-zone environment:** does the box where the user opens the GUI have a **live Virtuoso/
  skillbridge session + `dsub` reachable**? The live flow (resolve/augment/netlist/run/cell-build)
  needs BOTH. If the red zone is instead the **airgapped standalone modeler box** (`deploy/`
  target, no Virtuoso/cluster), the live flow can't run there — we'd split it (extract on the
  Virtuoso host, model in the standalone GUI). **Get this answer first.**
- **GUI default engine:** Path A (works now) vs Path B (needs the netlister). Recommend Path A
  default + Path B selectable/flagged.
- **Also close the box-validation items?** (`insituNetlistTest` ADE netlist-only API + the live
  one-time ADE setup + real 3+3+1 PSF capture + async dsub+poll smoke) — needs a live box session;
  can run in parallel with the GUI build.

## Reusable (do NOT re-derive / reimplement)
- `gui/ldo_modeler.py`: tabs 0–5; `ExtractCore`; `_ExtractWorker` (QThread, progress/cancel);
  `_ManifestEditorDialog`; thin-shell-over-validated-harness pattern; `--selftest`.
- Backend entry points: `run_pmu_corner` + `step_cell` (Path B), `insitu.cli.produce_npz` (Path A,
  what the GUI drives today), `resolve_nets`, `build_manifest`, `fit_multiport`, `emit_pmu_va`.
- D model-cell SKILL: `pmuBuildModelCell`, `ldoEnsureLib`/`ldoImportVA`/`ldoCompileVA`.
- Testing: offscreen Qt (`QT_QPA_PLATFORM=offscreen`) + the GUI `--selftest`; the in-situ backend
  has 92 passing tests already. Deploy: `deploy/package.py` → `update.sh`/`bootstrap.sh`/`run_gui`;
  `GUI_DEPLOY_BUILD.md` / `GUI_DEPLOY_PLAN.md`.

## Hard constraints (carry over)
SKILL via the **virtuoso-skill** protocol (grep index → read PDF → cite; never from memory) ·
keep the **npz firewall** · never write the designer spine `$WORK_ROOT/simulation/<Lib>/<Cell>/`
(our workarea is `$WORK_ROOT/ldo_modeling/...`) · the GUI is a **thin shell** over the validated
harness — wire, don't reimplement · csh on the company box.
