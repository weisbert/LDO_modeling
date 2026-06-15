# Next task — one-corner PMU LDO modeling via the ALPS CLI path

**Goal (user, 2026-06-15):** At the company, the user points (via GUI) to where the **PMU
testbench (TB)** lives; we then drive **one corner’s LDO modeling end-to-end through the CLI**
(`dsub`+`alps` → PSF → npz → fit → model). First real exercise of the just-validated company
sim/submit path.

## DECISION (2026-06-15, confirmed with user) — Path B (pure-CLI) FIRST, then Path A
User chose: **pure CLI `dsub+alps` as the PRIMARY driver; get THIS working, THEN add Path A
(Mechanism-A Maestro drive).** So for this build:
- **Drive** = our own assembled `dsub … <wrapper>/bin/alps …` (engine-param {alps,spectre}),
  submitted async; **poll Donau `djob`/`dstat` → map to pending/running/done and surface it**
  (= deliverable #1 "GUI sees progress" on the CLI path). Spectre fallback kept (§8).
- **Maestro-open** (user's "other question") = classic PSF loads via ViVA `Results → Open
  Results` on the psf dir; it will NOT auto-populate a Maestro test row — that native
  behavior is Path A (Mechanism A inherits the Donau Job Setup + ALPS checkbox), DEFERRED.

## Runtime inputs the user hands us (from the company GUI)
- PMU TB `lib/cell/view` **+ which instance is the PMU/DUT** (so we can resolve its pins).
- 6 OUTPUT symbol-pin names — V: `VDD0P8_DIG`, `VDD0P8_PLL`, `VDD0P8_VCO`; I:
  `IBP_POLY_1P8U_VCO`, `IBP_POLY_500N_VCO_Fit`, `IBP_PTAT_TUNE_1P5U_VCO`.
- 1 INPUT symbol-pin name — `AVDD1P0` (**single** supply, 1.0 V).
- Target MODEL `lib/cell/path` for the emitted veriloga+symbol.
- ⚠️ These are **SYMBOL pin names** → must be resolved to TB nets first (component 1).

## Components to BUILD (this turn)
1. **symbol-pin→net resolver** (SKILL via skillbridge): locate the PMU/DUT instance in the TB,
   walk its instTerms, return `{pinName: netName}` (`inst~>instTerms` / `dbGetInstTermByName`).
2. **in-situ augment on the REAL TB**: generalize the `cadence/extract_pmu.py:8-17` contract to
   **3 V-out / 3 I-out / 1 supply (`AVDD1P0`)** applied at the resolved nets, AVDD1P0 AC
   idealized. Per V-out: `Zout`(1 A AC)+`PSRR`(1 V AC on AVDD1P0, complex)+`noise`. Per I-out:
   `admittance`(1 V AC on pin)+`current-PSRR`(1 V AC on AVDD1P0). + coupling among the 3 V-outs.
   Reuse `cadence/insitu/augment.il` + `manifest`.
   - **DC-bias / operating point (critical — model is linearized at the OP):** small-signal
     extraction must happen at each port's REAL operating point. **V-out**: LDO self-holds
     ~0.8 V but the **load CURRENT** must be set externally — the `1A AC` probe is `mag=1 dc=0`
     (probe only, no bias). No load → **silent fallback to no-load** OP (sim converges, but
     Zout/PSRR/noise are no-load values + possible no-load peaking artifact). **I-out**: the pin
     VOLTAGE is undefined without a DC reference — the `vsource mag=1` admittance probe doubles
     as the DC `compliance` bias (`dc=V_node`). Floating I-out → **DC non-convergence / railed
     garbage OP** (harder failure than V-out). Strategy: **true in-situ FIRST** (keep the TB's
     real loads / driven nodes → don't add idc/vdc, OP self-consistent); only if a port is
     floating, take a user-supplied `I_load`(V-out) / `V_node`(I-out) and add `isource dc=` /
     `vsource dc=`. **Safety net: run a DC OP first, print per-port `(V_dc, I_dc)`** for the user
     to sanity-check (V-out `I≈0` = missing load; I-out `V` railed = missing compliance); a
     floating I-out is REFUSED with a clear message, never dumped as a raw convergence error.
     Slots already exist in `extract_pmu.py` (`Ipll/Ivco isource dc=`, `Vb500/Vb1u vsource dc=`).
3. **CLI driver** (`cadence/cluster/` or `cadence/alps_cli.py`): assemble + submit the proven
   `dsub … <wrapper>/bin/alps input.scs -ade -format ps -o ../psf -I <pdk>/alps -ahdllibdir …
   -mt 8` (wrapper-not-raw, `-x all`, csh); **poll Donau → pending/running/done**; hand the psf
   dir to binpsf. Engine-parameterized {alps,spectre} (ALPS_DONAU_NOTES §8).
4. **storage convention**: `$WORK_ROOT/ldo_modeling/<Lib>__<Cell>/<corner>/{netlist,psf,npz,model}`;
   do NOT touch the designer's `$WORK_ROOT/simulation/<Lib>/<Cell>/` spine. Netlist by borrowing
   ADE's run (it pre-compiles the VA into `sharedData/input.ahdlSimDB`).
5. **downstream** (EXISTS): `binpsf`→`import_cadence`→npz→`fit_model`→`model/*.{lib,va}`.
6. **model cell emitter**: veriloga + symbol at the user's `lib/cell/path`, **compiled**. Interface
   = `AVDD1P0` LEFT, 6 outputs RIGHT, `VSS` BOTTOM. Reuse `cadence/skill/pmu_top_symbol.il`
   (`schCreatePin`+`schViewToView`; ADD a VSS-at-bottom row) + `ldo_cellview.il`/`skill_lib.py`.

## Where we are (validated this session — all pushed @ 445cf14)
The **ALPS-CLI-under-Donau path is proven end-to-end** (hand-assembled `dsub`+`alps`, no Maestro,
→ classic PSF byte-identical to ADE). Full intel: **`cadence/ALPS_DONAU_NOTES.md`** (single source
of truth) + memory **`alps-donau-cli-flow`**. Key carried facts:
- Invoke the **wrapper** `/software/empyrean/alps/2026.03.hf1/bin/alps` (NOT the raw binary — it
  sets `LD_LIBRARY_PATH`). `-x all` carries the FlexLM `LM_LICENSE_FILE` to the node.
- Output `-format ps` = **classic PSF** → `binpsf.py` reads it unchanged (npz firewall holds).
  Never `psfxl`. Model `-I <pdk>/alps` only (unambiguous `.alps` models; §1d).
- `-mt 8` ↔ Donau `cpu=8`. Standard LDO tuple: `-q short -A ug_rfic.rfSClass -R "cpu=8;mem=8000"`.
- Naming: **add `-ade`** to get ADE-style `ac.ac`/`noise.noise` + `.simDone` (matches binpsf &
  the local stand-in); without it, native names (`*.fd`/`*.td`) + read `logFile` as the index.
- Company host shell = **csh** (backticks, `|&`, `set`). Run dir layout: `<projectDir>/<lib>/<cell>/
  maestro/results/maestro/<TestSet>.<n>/<corner#>/<Test>/{netlist/input.scs, psf}`.

## Code status — what exists vs what to build (⚠️ the CLI path is GREENFIELD)
**There is NO code yet for the self-driven `dsub`+`alps` CLI path** — this session validated it by
hand (csh). All `alps`/`cluster`/`dsub` strings in the codebase are *comments*, not logic.
- **Reusable as-is:** `cadence/binpsf.py`, `cadence/insitu/importmp.py`, `cadence/import_cadence.py`
  (PSF→npz, engine/path-agnostic — already try `psf/`[ALPS] & `netlist/`[Spectre]); `harness/fit_model.py`
  + scoring (downstream, pure Python); `cadence/insitu/{manifest,augment,adestate}.py` (TB capture/augment logic).
- **Does NOT cover us:** Mechanism A (`cadence/insitu/run.py`) drives **ADE-XL via skillbridge** and
  **inherits the session’s Job Setup** — i.e. *ADE* assembles `dsub … alps …`, we neither build nor
  control that command. The local `spectre_run.py`/`bench_spectre.py` golden is local-only, no cluster.
- **To BUILD (new):**
  1. A **Donau/ALPS CLI driver** (e.g. `cadence/cluster/` or `cadence/alps_cli.py`): assemble the
     validated `dsub … <wrapper>/bin/alps … -x all -I …` command; submit (blocking `-I`, or async +
     poll `djob`/`.simDone`); hand the produced PSF dir to `binpsf`. Engine-parameterized {alps,spectre} (§8).
  2. A **storage / workarea convention**: where netlists + psf live and how the result PSF dir is
     resolved (this session borrowed ADE’s run dir and pointed at it by hand).
  3. Wire (1)+(2) into the existing PSF→npz→fit downstream.
  This IS the coding part of the task below.

## The plan (MVP = one corner, recommended path)
Build on what’s proven: let **ADE netlist** the corner (it also pre-compiles the VA into
`sharedData`), then **we CLI-run + downstream**.
1. **User provides** (from GUI): PMU TB `lib/cell/view`; the **LDO instance**; the contract nets
   — supply `<sup>` (1.05 V), `<sup2>` (1.8 V if used), output `<out>` (0.8 V); one **load corner**
   (nominal TT) + the LDO’s DC OP.
2. **In-situ extraction setup** (per `cadence/COMPANY_RUNBOOK.md` §1): keep the LDO wired in the
   PMU; apply contract stimuli at the LDO’s own pins, idealize only the supply’s AC:
   - `Zout`: ideal-DC the supply(s) (= AC gnd), inject **1 A AC into `<out>`**, `ac` 10 Hz–500 MHz → `Z=V(out)`.
   - `PSRR`: **1 V AC on `<sup>`** (others ideal-DC), `ac` → `H=V(out)/V(sup)` (store COMPLEX). 2nd path on `<sup2>` if multi-supply.
   - `noise`: out=`<out>`, supplies noiseless → output noise PSD (V/√Hz), sum the LDO instance’s contribution.
3. **Netlist one corner** in ADE → `input.scs` (+ `sharedData/.../input.ahdlSimDB`).
4. **CLI run** (the proven command; add `-ade` for ADE-style names):
   ```csh
   dsub -A ug_rfic.rfSClass -q short -R "cpu=8;mem=8000" -x all -EP <netlistdir> -I \
     /software/empyrean/alps/2026.03.hf1/bin/alps  input.scs -ade -format ps -o ../psf \
       -I <pdk>/c1x_plus_20251210/alps -ahdllibdir <run>/sharedData/CDS/ahdl/input.ahdlSimDB -mt 8
   # poll ../psf/.simDone (with -ade) or djob; then binpsf reads ../psf/{ac.ac,noise.noise}
   ```
5. **Downstream (pure Python, our box or theirs):** `binpsf.py` → `import_cadence.py` →
   `results/ref/<name>.npz` → `harness/fit_model.py --variant <name>` → `model/ldo_<name>.{lib,va}`.
6. **Confirm**: one real corner flows all the way to a model. (`score_spectre.py` to sanity-check.)

## Decisions to make with the user next session
- Reuse ADE netlisting (MVP above) vs full-CLI netlist export (defers to Stage 2 VA-compile).
- `-ade` (ADE names + sentinel, recommended) vs native names + `logFile` index.
- Single load point vs the 3 load corners (start with one; the contract wants 3 eventually).

## Deferred (not this task)
- Multi-corner / load sweep; the **Spectre fallback pipeline** (capture a real `spectre.out` with
  “Use ALPS” unchecked to mirror §8); **Stage 2** standalone (non-ADE) netlist + VA self-compile;
  cleanup of the `psf_selfdrive` validation dir.

## Hard constraints (carry over)
csh on the company box · SKILL via the **virtuoso-skill** protocol (grep index → read PDF → cite,
never from memory) · keep the **npz firewall** · don’t modify the designer’s spine (work on copies)
· wrapper-not-raw-binary + `-x all` for license · classic PSF only (no psfxl).
