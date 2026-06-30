# PMU one-corner modeling — company-box runbook (pure-CLI ALPS path)

This is the exact, step-by-step procedure a designer follows on the company submit host to
model ONE process corner of an in-situ PMU LDO end-to-end via the **pure-CLI ALPS path**
(Path-B): resolve pins → build the manifest → augment the extraction TB → netlist (in ADE) →
run the corner on the cluster via `dsub`+`alps` → import PSF → fit → emit the combined
Verilog-A model cell → import + compile it.

The orchestrator is `cadence/insitu/pmu_corner.py` (`run_pmu_corner(...)`). Every external
system (Virtuoso, the cluster, dsub) is an injectable/skippable seam, so the same code that is
unit-tested offline runs for real here just by handing it a live session + the netlist handoff.

### One corner = N (~7) cluster jobs — the per-group SWEEP (read this first)

In-situ extraction identifies each transfer function by **AC superposition**: one simulation
**per measurement GROUP**, each setting **exactly ONE `acm_*` design variable to 1** (all others
0) and enabling **only that group's analysis**. So step 5 is NOT a single run — it is a SWEEP of
**N cluster jobs**, one per group:

- `run.groups(manifest)` collapses the measurement points into the minimal run set. AC points
  **merge** by `(analysis, one-hot stimulus)` — one run feeds every saved port at that one-hot.
  **NOISE is per-output** (a spectre noise analysis measures one oprobe), so each `v_out` noise
  is its own group. The stand-in 2-rail/2-sink PMU → **8 groups** for 14 measurements; the real
  3-rail/3-sink PMU → **10 groups** for 21 measurements (≈7 in the general "~7 runs" framing).
- Each group's netlist sets that group's `acm_*` one-hot + only its analysis, and its job writes
  to its **own** PSF dir `…/<corner>/psf/<group_tag>/`.
- The importer then maps **each group's PSF dir to its member measurement tags** — `psf_map` is
  keyed **by measurement TAG**, never by corner. `from_psf_multiport` reads each measurement
  from ITS group's run.

---

## 0. Prerequisites (company submit host)

- **Shell**: company shell is `csh`. Run the orchestrator under the project Python (the one
  with numpy/scipy that runs the harness). `python3 -m insitu.pmu_corner ...` (or call
  `run_pmu_corner` from a Python shell).
- **Virtuoso CIW + skillbridge server up.** In the CIW, load the three SKILL helpers ONCE per
  session (absolute paths):

  ```scheme
  load("/abs/.../LDO_modeling/cadence/skill/resolve_nets.il")    ; step 1 (insResolveNets)
  load("/abs/.../LDO_modeling/cadence/skill/pmu_top_symbol.il")  ; step 9 (pmuBuildModelCell)
  load("/abs/.../LDO_modeling/cadence/skill/ldo_cellview.il")    ; step 9 (ldoEnsureLib/ImportVA/CompileVA)
  ```

  Start the skillbridge server in the CIW so the Python side can attach (the same bridge the
  existing `insitu` flow uses). The orchestrator opens a `skillbridge.Workspace` for steps
  1 / 3 / 9; with no server those steps fail fast or — when you pass `session=None` /
  `--dry-run` — degrade to a printed PLAN.
- **ADE available to netlist.** Step 4 (netlist) is the documented handoff: ADE netlists the
  augmented `<tb>_extract` cellview and **pre-compiles its Verilog-A**, producing `input.scs`
  + a compiled `-ahdllibdir`. You hand the orchestrator those paths (see §4). The orchestrator
  does NOT shell out a netlister — that is a deliberate, clearly-marked future hook.
- **Donau (`dsub`/`djob`/`dpeek`/`dkill`) on PATH**, and `alps` reachable via the wrapper
  `/software/empyrean/alps/2026.03.hf1/bin/alps` (the bash wrapper sets `LD_LIBRARY_FILE`/
  `LD_LIBRARY_PATH` then exec's the raw binary; the raw binary fails `libsvadv.so` on the node).

---

## 1. The GUI inputs to hand us

These are the fixed real-PMU inputs (the **6 outputs + 1 supply** are SYMBOL PIN NAMES on the
DUT instance; the resolver turns them into TB nets in step 1):

| GUI field | value (real PMU) |
|---|---|
| `tb_lib` / `tb_cell` / `tb_view` | the PMU testbench cellview (e.g. `PMU_TOP_TB` / `pmu_tb` / `schematic`) |
| `dut_inst` | the DUT instance name in that TB (e.g. `I0`) |
| `dut_lib` / `dut_cell` | the PMU DUT (e.g. `PMU_TOP` / `pmu_top`) |
| `supply` | `{pin: AVDD1P0, dc: 1.0, tb_src: <the TB source instance driving AVDD1P0>}` |
| `v_outs` | `[VDD0P8_DIG, VDD0P8_PLL, VDD0P8_VCO]` — the 3 voltage rails |
| `i_outs` | `[IBP_POLY_1P8U_VCO, IBP_POLY_500N_VCO_Fit, IBP_PTAT_TUNE_1P5U_VCO]` — the 3 bias currents |
| `ground` | `VSS` |
| `corner` | the process-corner label (e.g. `tt_25c`) |
| `vdc` | **per-i_out compliance** map `{<i_out pin>: <node operating voltage>}` (see below) |
| target model | `model_lib` / `model_cell` / `model_path` — where the combined model cell lands |

### Per-i_out compliance `vdc` — why current outputs need it and voltage outputs don't

The two port types **linearize differently**, so the bias policy is asymmetric:

- **Voltage rails (`v_outs`)** are characterized **truly in-situ**: the Zout probe the augment
  adds is an AC-only current source with `dc=0`, so it never moves the operating point — the
  TB's own load biases the rail. No DC input is required (an optional `iload` is pure metadata).
- **Current outputs (`i_outs`)** are **port-isolated**: the augment owns a probe *voltage*
  source at the pin that BOTH applies a compliance DC **and** injects the AC — it *replaces*
  the node's DC driver. So each i_out needs `vdc` = the pin's **real operating node voltage**.
  If you omit it, `manifest.validate()` defaults `dc=0.0` (clamps the pin to 0 V, almost never
  the true OP) and the orchestrator prints a prominent `_warnings` line listing every i_out
  missing compliance. **Supply `vdc` for each of the 3 bias currents** for a meaningful fit.

Provide the GUI as a JSON file, e.g. `gui.json`:

```json
{
  "tb_lib": "PMU_TOP_TB", "tb_cell": "pmu_tb", "tb_view": "schematic", "dut_inst": "I0",
  "dut_lib": "PMU_TOP", "dut_cell": "pmu_top",
  "supply": {"pin": "AVDD1P0", "dc": 1.0, "tb_src": "V_AVDD"},
  "v_outs": ["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"],
  "i_outs": ["IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit", "IBP_PTAT_TUNE_1P5U_VCO"],
  "ground": "VSS", "corner": "tt_25c",
  "vdc": {"IBP_POLY_1P8U_VCO": 0.45, "IBP_POLY_500N_VCO_Fit": 0.45, "IBP_PTAT_TUNE_1P5U_VCO": 0.50}
}
```

---

## 2. Where artifacts land (the workarea tree)

`$WORK_ROOT` is resolved from the env var `WORK_ROOT` (fall back: `~/ldo_workarea`). The
orchestrator OWNS this tree and **never** writes into the designer's spine
`$WORK_ROOT/simulation/<Lib>/<Cell>/`:

```
$WORK_ROOT/ldo_modeling/<tb_lib>__<tb_cell>/<corner>/
    <tb_cell>_<corner>.manifest.json   # step 2 — the pin-role manifest
    netlist/<group_tag>/input.scs      # step 5 — per-GROUP one-hot netlist (default seam, box)
    psf/<group_tag>/                   # step 5 — per-GROUP classic-PSF output (one job per group)
    npz/<name>_<corner>.npz            # step 6 — the generalized multi-port npz (contract firewall)
    model/<model_cell>.va              # step 8 — the ONE combined Verilog-A module
    model/cds/<model_lib>/             # step 9 — the imported veriloga cellview (default model_path)
```

Note `psf/` and `netlist/` now hold **one subdir per measurement group** (`<group_tag>/`) — the
SWEEP writes one job's output per group.

Set `WORK_ROOT` before running, e.g. `setenv WORK_ROOT /scratch/$user/ldo` (csh).

---

## 3. The command sequence

```csh
# from the repo's cadence/ dir (so the `insitu` package + reused siblings resolve)
cd /abs/.../LDO_modeling/cadence
setenv WORK_ROOT /scratch/$user/ldo

# (A) full real run — resolve (live), augment (live), netlist handoff, run on cluster, fit,
#     emit, build the model cell (live). The skillbridge server must be up.
python3 -m insitu.pmu_corner \
    --gui /abs/path/gui.json \
    --corner tt_25c --engine alps \
    --netlistdir  /abs/.../maestro/.../Test/netlist \
    --ahdllibdir  /abs/.../input.ahdlSimDB \
    --pdk-model-dir /abs/.../models/c1x_plus_20251210 \
    --model-lib LDO_model_lab --model-cell pmu_top_model
```

Or call it directly (gives you the live `session=` + `on_status=` seams):

```python
from skillbridge import Workspace
from insitu.pmu_corner import run_pmu_corner
ws = Workspace.open()                                  # the live CIW bridge
res = run_pmu_corner(gui, work_root="/scratch/me/ldo", corner="tt_25c", engine="alps",
                     session=ws,                       # steps 1/3/9 drive Cadence
                     netlistdir=NETDIR, ahdllibdir=AHDL, pdk_model_dir=PDK,
                     model_lib="LDO_model_lab", model_cell="pmu_top_model",
                     on_status=lambda st, raw: print("Donau:", st.upper()))
```

### The 9 steps and how each is skippable / seamed

1. **resolve** — `resolve_nets` over skillbridge → `{pin: net}`. Skip by passing `netmap=`
   (or `--netmap netmap.json`). With no live session and no netmap you get an **actionable**
   error: *supply netmap= or run on the box*.
2. **manifest** — `build_manifest` → writes `<tb_cell>_<corner>.manifest.json`. The
   missing-compliance `_warnings` are surfaced **prominently before the run**.
3. **augment** — `augment.build` builds `<tb>_extract` over skillbridge. **Auto-skipped when a
   `netlistdir` is provided** (the extract TB was already built + netlisted in ADE). With no
   session / `--dry-run`, prints the augment PLAN (`augment.build_plan`).
4. **netlist (HANDOFF)** — accepts `netlistdir` + `ahdllibdir` + `pdk-model-dir`. Missing any →
   a clear error telling you to netlist `<tb>_extract` in ADE. The `ahdllibdir` (compiled VA DB)
   and `pdk-model-dir` are **shared by every group**; the **per-group `input.scs`** is produced
   by the step-5 group-netlister (below). *[future hook: ADE-triggered netlisting from the
   augmented TB.]*
5. **run — the SWEEP** — `grps = run.groups(manifest)`; for each group the orchestrator (a) gets
   that group's netlist dir from the **`group_netlister(group)` seam**, (b) submits one
   `dsub`+`alps` job via `cluster.run_corner`, polls `djob`, verifies the PSF + `.simDone`
   sentinel, and (c) maps **`psf_map[member_tag] = that group's PSF dir`** for every member.
   `on_status` surfaces the Donau transitions, prefixed `group i/N <tag>`. Returns the **BY-TAG**
   `psf_map` + the **per-group** `dsub_cmds` list (`res["dsub_cmds"]`; `res["dsub_cmd"]` is the
   first group's, for back-compat).
   - **The `group_netlister` seam.** The DEFAULT (live session) is `ade_group_netlist`: it reuses
     the proven `run.run_ade` wiring — `enable_only_analysis(group.analysis)` (+ `set_noise_output`
     for a noise group), set the `acm_*` **one-hot** via `insituPutVar` (group's hot vars → '1',
     all others → '0'), then **netlist the ADE test (no run)** and copy the produced `input.scs`.
     The netlist-only trigger is the SKILL helper **`insituNetlistTest`** in
     `cadence/skill/insitu_run.il` — currently a **documented box-research stub** (the netlist-only
     export API must be confirmed live; see §8). Tests inject a fake `group_netlister`.
6. **import** — `importmp.from_psf_multiport` reads each measurement from **its group's** PSF dir
   (the **BY-TAG** `psf_map` from step 5) → the generalized npz, written into the **workarea**
   `npz/` (step 6 deliberately does NOT use `assemble_multiport`, which writes to `results/ref/`
   and would escape the workarea). The read-side is the dual of the augment stimulus side. Bypass
   with `npz_in=<ready npz>`.
7. **fit** — `fit_multiport.fit_multiport` + `report` (voltage ports + a SEPARATE current-port
   table). Pure-Python.
8. **emit** — `emit_pmu_va` → the ONE combined `.va`: input `AVDD1P0` (left), the 6 outputs
   (right), `VSS` (bottom). The module ports are the **GUI symbol pin names**.
9. **cell** — `ldoEnsureLib` + `ldoImportVA` + `ldoCompileVA` + `pmuBuildModelCell` over
   skillbridge at your target lib/cell/path. With no session / `--dry-run`, prints the SKILL
   CALL PLAN instead of touching Cadence.

A `steps=` allow-list (Python) selects a subset; injecting a step's output skips it.

---

## 4. What the status output looks like (per group: pending → running → done)

Step 5 threads `on_status(state, raw)` through each group's `run_corner` → `donau.poll`, which
fires ONCE per real transition. The SWEEP submits one job per group, so you watch every group
progress through Donau in turn (`group i/N <tag>`):

```
[pmu_corner] run      | group 1/10 g_v_out_dig: submitting
[pmu_corner] run      | group 1/10 g_v_out_dig: Donau job PENDING     # dsub queued (State PENDING)
[pmu_corner] run      | group 1/10 g_v_out_dig: Donau job RUNNING     # node picked it up
[pmu_corner] run      | group 1/10 g_v_out_dig: Donau job DONE        # Exit 0 + .simDone landed
[pmu_corner] run      | group 1/10 g_v_out_dig: PSF landed: …/tt_25c/psf/g_v_out_dig
[pmu_corner] run      | group 2/10 g_n_dig: submitting
…                                                                     # … through all N groups
```

On a FAILED job the orchestrator raises with the `dpeek` tail of the sim log (the usual cause:
a netlist/model error, or the raw-binary `libsvadv.so` lib-env miss if the wrapper was bypassed).

---

## 5. How to open the classic-PSF results in Maestro / ViVA

The CLI run writes a **classic BINPSF** `psf/` dir (the exact layout `binpsf.py` reads:
`pss.*`/`ac.ac`/`noise.noise` with the big-endian `…PSFversion…BINPSF…` header). To inspect it
in the Cadence GUI:

> In Virtuoso/Maestro: **Results → Open Results…** and point the browser at the CLI `psf` dir
> (`$WORK_ROOT/ldo_modeling/<Lib>__<Cell>/<corner>/psf`). ViVA opens the waveforms directly.

**It will NOT auto-populate a Maestro test row.** A CLI `dsub`+`alps` run is not a Maestro-driven
simulation, so there is no test/history row tied to it — you browse the PSF as a standalone
results dir. (Auto-populating a Maestro test row is the deferred **Path-A** behavior: drive the
run through ADE-XL `axlRunAllTests` so Maestro owns the results tree. Path-A reuses this same
PSF→npz→fit→emit tail; only the launcher differs.)

---

## 6. Reference — the proven `dsub`+`alps` command (one PER GROUP)

The orchestrator assembles exactly this **for each group** (the full list is returned in
`res["dsub_cmds"]`; `res["dsub_cmd"]` is the first group's — both available on `--dry-run`).
Validated in `cadence/ALPS_DONAU_NOTES.md` (§1c/§2a/§9):

```
dsub -A ug_rfic.rfSClass -q short -R "cpu=8;mem=8000" -x all -EP <group_netlistdir> -J \
    /software/empyrean/alps/2026.03.hf1/bin/alps input.scs \
        -format ps -o <psf_dir>/<group_tag> \
        -I <pdk>/c1x_plus_20251210/alps \
        -ahdllibdir <ahdldir> -mt 8 -ade
```

Only **`-EP <group_netlistdir>`** (this group's one-hot `input.scs`) and **`-o …/<group_tag>`**
(this group's PSF subdir) change per group; the `-ahdllibdir` (compiled VA DB) and `-I <pdk>/alps`
are shared by all groups.

Key points (all from the notes):
- **wrapper, not the raw binary** — `…/bin/alps` sets the lib env then exec's; the raw binary
  fails `libsvadv.so` on the node.
- **`-format ps`** = classic PSF (a hidden flag; ADE's `psfxl` is downgraded to `ps` for ALPS).
- **`-o`** = output dir, this group's `psf/<group_tag>`. ADE uses `-o ../psf` when `psf/` is a
  *sibling* of the netlist Pwd; when the netlist dir and our workarea `psf/<tag>` are in
  different trees, the orchestrator passes the **absolute** path (the node resolves it the same).
- **`-ade`** = ADE-style output names (`ac.ac`/`noise.noise`) + the 0-byte `.simDone` completion
  sentinel `run_corner` polls for.
- **`-mt 8` MUST equal Donau `cpu=8`** (the orchestrator matches `-mt` to `DonauCfg.cpu`).
- **`-x all`** propagates the submit shell's env (FlexLM `LM_LICENSE_FILE`, EDA env) to the
  node — required for licensing on the CLI path.
- **`-I <pdk>/alps` only** — so `include "toplevel.scs"` can only resolve to the `.alps`
  selector (unambiguous models); never let the `spectre` tree win on an ALPS run.

`DonauCfg(account, queue, resource)` (`cadence/cluster/donau.py`) tunes `-A`/`-q`/`-R`; the
default is the validated LDO tuple above.

---

## 7. Offline dry-run (no Cadence, no cluster) — for validation

```csh
cd /abs/.../LDO_modeling/cadence
python3 -m insitu.pmu_corner --gui gui.json --netmap netmap.json \
    --netlistdir /tmp/netlist --ahdllibdir /tmp/ahdl --pdk-model-dir /tmp/pdk \
    --npz-in ../results/ref/pmu_standin.npz --dry-run
```

Writes the manifest + the augment/SKILL plans, assembles (and prints) **one `dsub` command per
group** (`res["dsub_cmds"]`), and — because a ready npz is injected — still fits + emits the
combined `.va`. Executes nothing. The offline `--dry-run` CLI has **no live session**, so it
cannot run the per-group `ade_group_netlist` (which needs ADE to netlist each one-hot); it falls
back to reusing the single handoff `--netlistdir` as every group's `-EP` so the commands still
assemble. On the box (live session) each group gets its OWN one-hot netlist dir.
`cadence/insitu/test_pmu_corner.py` exercises the real SWEEP with a fake `group_netlister` + a
fake cluster runner, and a SEPARATE test drives the ACTUAL `importmp` reader over a real BY-TAG
`psf_map` (`cadence/work_pmu`) → fit → emit.

---

## 8. Box-validation item (FLAGGED) — the per-group netlist export

The default `group_netlister` (`ade_group_netlist`) reuses the proven `run.run_ade` ADE-state
wiring to set each group's `acm_*` one-hot + analysis, then must **netlist the ADE test without
running it**. That netlist-only trigger is the SKILL helper **`insituNetlistTest(session, test)`**
in `cadence/skill/insitu_run.il`, currently a **documented box-research stub** that errors with
exactly what to wire. The grounded pieces (virtuoso index + skartistref/adexl refs):

- the per-test ADE-L session bridge: `sev = axlGetToolSession(sess test)` (adexl p.36) →
  `ts = asiGetSession(sev)` (skart p.648) — same bridge `adestate.py` already uses;
- `asiGetNetlistDir(ts)` (skart p.644) — **locates** (and creates) the netlist dir, but does
  NOT trigger netlisting;
- `asiNetlist(ts)` (skart p.421) — **runs** the netlister, BUT the reference says it is "called
  by the environment, you should not call it directly" (an overridable Analog-class method).

**Because no clearly-PUBLIC "netlist this ADE-XL test now, no run" entry point is in the
virtuoso index, the signature was NOT guessed.** To close this item on the box: confirm the
sanctioned netlist-only call (the ADE-XL *Netlist* menu's underlying fn, or `asiNetlist` /
`sevNetlistFile` via the `sev` above), wire it into `insituNetlistTest`, then return
`asiGetNetlistDir(ts)`; and confirm the produced per-group `input.scs` carries the intended
one-hot + single analysis. Until then the **default `group_netlister` raises on the box** — the
offline tests never reach it (they inject a fake), and they fully exercise the SWEEP + the
BY-TAG `psf_map` + the real importer.

**Box-validation checklist (all of the live per-group netlist path needs one box trip):**
1. **Wire `insituNetlistTest`** — the sanctioned ADE netlist-only trigger (above), returning the
   netlist dir.
2. **One-time ADE test setup** — `_live_group_netlister` (in `pmu_corner.py`) already does, ONCE
   before the first group, exactly what `run.run_ade` does: `insituEnsureTest` (create the bare
   extract test) → `adestate.inherit_state` (backfill the designer OP vars — fixes ASSEMBLER-1610
   — and configure the ac+noise analyses) → `insituEnableOnly`. Confirm the `session_name`
   argument (`run_pmu_corner(..., session_name=...)`, default `"fnxSession0"`) matches your live
   ADE-XL session, and that `enable_only_analysis` / `insituPutVar` then act on the configured test.
3. **Confirm per-group netlist content** — each group's `input.scs` has its one `acm_*=1`
   (others 0) and only that group's analysis (noise groups: the oprobe on that group's output).
4. **Async submit + poll smoke** — the orchestrator submits `dsub -J` (no `-I`) and polls `djob`;
   the §9-validated manual run used `-I`. Smoke-test the async JOBID-parse + `djob` state words
   live once before relying on the unattended poll.
