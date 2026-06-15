# Next task — one-corner PMU LDO modeling via the ALPS CLI path

**Goal (user, 2026-06-15):** At the company, the user points (via GUI) to where the **PMU
testbench (TB)** lives; we then drive **one corner’s LDO modeling end-to-end through the CLI**
(`dsub`+`alps` → PSF → npz → fit → model). First real exercise of the just-validated company
sim/submit path.

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
