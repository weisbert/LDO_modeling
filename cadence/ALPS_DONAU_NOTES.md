# ALPS (CLI sim) + Donau (cluster submit) — research log

Living intel log for running the real-PMU sims on the **company box**: ALPS as the
simulator (CLI), Donau (`dsub`) as the cluster scheduler. Companion to
`COMPANY_RUNBOOK.md` (§0 setup / §1 extraction recipe).

**Box topology (confirmed 2026-06-15)**
- **Local stand-in** (this repo's dev box): Cadence IC618 + Spectre 18.1 + Calibre only.
  **No ALPS, no Donau, no module system.** Only place we can run Spectre-CLI goldens.
- **Company submit host** (remote desktop): ALPS + Donau (`dsub`) live here. All ALPS/Donau
  facts below are gathered *by the user on that host* and pasted back — marked accordingly.
- ✅ **Company host login shell = `csh`/`tcsh`** (not bash). Probe commands & any shell wrapper
  for the cluster must be csh-syntax: command substitution = backticks `` `…` `` (no `$(…)`);
  merge stderr into a pipe = `|&` (no `2>&1`); set vars with `set x = …`; `time cmd` is a csh
  builtin. (Our Python tooling is shell-agnostic — the shell only bites in interactive probes
  and any hand-written run wrapper.)

Legend:  ✅ VERIFIED (from tool output / paste)   🔶 INFERRED (LSF/Spectre analogy, unverified)
         ❓ OPEN (needs a probe on the company box — see “Open questions” at bottom)

---

## 0. ⚠️ Two simulation engines on the company box (don’t conflate)
- **Empyrean ALPS** — the `alps` CLI. ✅ `alps -ver` = **"Empyrean Software" build 2026.03.hf1**
  (rev a29e413, gcc 9.3.0). A **third-party** FastSPICE simulator. The PDK ships an ALPS model
  section (`…/models/c1x_plus_20251210/alps`). This is the “CLI run ALPS” the user asked about.
- **Cadence Spectre + APS turbo** — `simulator('spectre)` with `option(?categ 'turboOpts
  'uniMode "APS")`, PDK section `…/spectre`. ✅ This is what the user’s exported Maestro test
  (`sim_NDIV_Interference`) actually uses. APS = Cadence’s own Accelerated Parallel Simulator —
  *not* Empyrean ALPS, despite the similar “parallel” naming.
- Both engines read **Spectre-syntax netlists**; the PDK provides a model section for each.
- ✅ **Engine is user-selectable** via an ADE **“Use ALPS” checkbox** (right-click a test →
  Environment Options). Checked ⇒ ADE runs the Empyrean `alps` binary on the Spectre netlist;
  unchecked ⇒ Cadence `spectre` (+APS). **The checkbox does NOT serialize into the assembler
  OCN** — both exports are byte-near-identical (`simulator('spectre)` + `uniMode "APS"` either
  way; only a stray `check_simulator t` differed). It lives in the test’s ADE env state.
  → **For our CLI golden we don’t need the checkbox**: we call `alps` directly on ADE’s
  `input.scs` using the exact command template in §1c.

## 1. Empyrean ALPS — the CLI simulator (`alps`)
Reads a **Spectre-syntax netlist**. ✅ `alps -help` captured 2026-06-15 (193 lines, full;
annotated below). Vendor: Empyrean (see §0).

### 1a. Options that matter to us (✅ from `alps -help`)
| flag | meaning | note for us |
|---|---|---|
| `-c <file>` | config file | ❓ is this the netlist deck, or a separate options file? |
| `-o <path>` | **output directory** for simulation | where results land |
| `-log <filename>` | log file name | |
| `-mode <basic\|turbo\|pro>` | accuracy/speed (ALPS only) | `pro`=most accurate (small-signal AC/noise → use accurate end) |
| `-prolvl <1..5>` | post-sim level, `mode=pro` only | 1 liberal … 5 conservative (=moderate(3)/conservative(5)) |
| `-mt <num>` (alias `-p`) | max sim threads (1–256) | **must match Donau `cpu=`** |
| `-mtlimit <normal\|lsf\|ccs\|sge>` | where thread count comes from | **no `donau` value** → under Donau use `normal`+explicit `-mt`, OR Donau exports a var ALPS reads (❓) |
| `-mts <no\|yes>` | turn on MTS (multi-thread solver) | |
| `-gt <1..64>` / `-gt_speed <gold\|acc\|accplus\|perf\|perfplus\|fast>` | GPU accel (GPU pkg only) | likely N/A on RF cluster |
| `-ckttype <analog\|memory\|sram_timing>` | circuit type | ours = `analog` |
| `-precfg <name>` | preload option set `$ALPS_ROOT/tools/alps/etc/<name>.cfg` | company may ship a standard `.cfg` |
| `-inc <file>` / `+inc+<f>` | include netlist | |
| `-I <path>` / `+I+<p>` | include search path | model/lib paths |
| `-ahdllibdir <path>` / `-vamodellibdir <path>` | AHDL/VA lib dirs | where compiled VA goes |
| `-rec` | auto-recover analysis state on rerun | |
| `-d` | display sim process | |
| `-reportclock <min>` / `-reportinterval <pct>` | tran progress annotation | |
| `-saveclock <hour>` | checkpoint interval | |
| `-topckt <name>` | set top subckt on cmdline | |
| `-ver` | version | |

### 1b. Output format — RESOLVED from the real runtime command (✅ `spectre.out`)
- ✅ **Format flag IS `-format ps`** (in the actual `alps` cmdline). **`-format` is hidden** —
  it does NOT appear in `alps -help` (the 193-line help omits it). Output dir = **`-o ../psf`**
  (relative to the `netlist` Pwd → sibling `…/Test/psf`).
- ✅✅ **CONFIRMED classic PSF** (inspected a real `…/psf` dir): binary results
  (`pss.fd.pss`, `pss.td.pss`, `dcOp.dc`, `*.info`) start with the classic **BINPSF** header
  `…PSFversion…1.1…BINPSF creation time…PSF style…PSF sweeps…PSF sweep points…` — the exact
  big-endian layout `binpsf.py` already reads; ASCII metadata (`logFile`/`runObjFile`/
  `simRunData`/`variables_file`) is classic ASCII PSF (`HEADER "PSFversion" "1.00"`).
  ADE’s `simOutputFormat "psfxl"` is downgraded to `-format ps` for ALPS (Empyrean ALPS doesn’t
  do Cadence psfxl). **Empyrean ALPS deliberately impersonates Spectre in the PSF** (`"simulator"
  "spectre"`, `"version" "14.1.0.138"`) → our reader works unchanged. **npz firewall holds on the
  company ALPS-CLI path with NO new reader.** (psfxl only bites the *spectre* engine — avoid it
  there too.)
- ✅ **Completion sentinel**: a zero-byte `.simDone` appears in `…/psf` when the run finishes
  (also `.trynfssync`). Cleaner to poll than relying on dsub blocking. PSS produced
  `pss.fd.pss` (freq-domain) + `pss.td.pss` (time-domain); our ac/noise runs yield
  `ac.ac` / `noise.noise` in the same binary PSF.
- **Netlist passing = POSITIONAL**: `alps input.scs …` (first positional arg; not `-c`). ✅
- **`-64`**: absent → ALPS is 64-bit-only (no flag); Spectre needed `-64` only for VA compile.
- ❓ other `-format` values (`psfascii`/`psfbin`?): `alps -help-option |& grep -iE 'format|psf'`.

### 1c. ★ Exact ALPS command Cadence assembles (✅ from `spectre.out`) — our CLI template
Run from the per-point `…/<Test>/netlist/` dir (cwd matters; `-o` is relative):
```
alps input.scs -format ps -o ../psf \
     -I <pdk>/c1x_plus_20251210/spectre -I <pdk>/c1x_plus_20251210/alps \
     -ahdllibdir <run>/sharedData/CDS/ahdl/input.ahdlSimDB \
     -mt 8 -lqtimeout 900 -ade -adap_aff 0 -applog
```
| token | meaning |
|---|---|
| `input.scs` (positional) | the Spectre-syntax netlist ADE generated |
| `-format ps` | output format = classic PSF (hidden flag; ADE’s psfxl→ps for ALPS) |
| `-o ../psf` | output dir, relative to cwd (`netlist/`) → `…/Test/psf` |
| `-I <pdk>/spectre  -I <pdk>/alps` | model search paths (BOTH PDK sections) |
| `-ahdllibdir …/input.ahdlSimDB` | compiled AHDL/VA model DB |
| `-mt 8` | **8 threads — ADE passes Donau `cpu=8` straight through** (so `-mtlimit` is moot) |
| `-lqtimeout 900` | license-queue wait timeout (s) |
| `-ade -adap_aff 0 -applog` | ADE-integration flags (hidden): ADE mode / adaptive-affinity off / app log |

**Scheduler reality (✅ from log):** `Note: Simulation is launched by CCS with JOBID 37230513`,
node `sincs79-hs` (36 cores, 503 GB), threads bounded to 8 by `-mt`. → **Donau ≡ CCS** to ALPS
(the `ccs` value in `-mtlimit {normal,lsf,ccs,sge}`). dsub = user-facing submit; CCS = the job
backend ALPS detects. Submit host `sinct03-hs` ≠ compute node `sincs79-hs`.

**Run-dir layout (✅):**
`<projectDir>/<lib>/<cell>/maestro/results/maestro/<TestSet>.<n>/<corner#>/<Test>/{netlist,psf}`
e.g. `…/Test_model_LDO_second_same_GND.1/1/Test/{netlist/input.scs, psf/…}`.

### 1d. ✅ Model-file selection per engine — RESOLVED (correctness-critical)
The PDK ships **two** model trees `…/c1x_plus_20251210/{alps,spectre}`, each with its own
`toplevel.scs` **selector wrapper**. Both have **identical section names** (`TOP_TT_RFTYP`,
`pre_Sim`, `Noise_Worst`, FF/SS…); they differ **only** in the `.lib` device-file extension:
```
/alps/toplevel.scs:     .lib 'CF710_Plus_0d8_logic_1d0a_rev1_usage.alps'  TT_MOS_MOSCAP   …
/spectre/toplevel.scs:  .lib 'CF710_Plus_0d8_logic_1d0a_rev1_usage.scs'   TT_MOS_MOSCAP   …
```
`input.scs` uses a **bare** `include "toplevel.scs" section=…` → resolved by the **`-I` search
path**. The captured alps cmd `-I …/spectre -I …/alps` loaded **`/alps`** (per `psf.warn`) ⇒
**ALPS gives the LAST `-I` precedence** (matches OCN `path(alps spectre)` = alps highest, emitted
reversed). So the “Use ALPS” engine choice = which model tree wins the search-path race; the
company script just sets that ordering. `.alps` vs `.scs` are vendor-specific device cards.

**Our CLI control (unambiguous):** pass **only `-I <pdk>/alps`** → `include "toplevel.scs"` can
only resolve to the alps selector ⇒ guaranteed `.alps` models; anything missing errors LOUDLY
(no silent wrong-model). Alternative: replicate ADE’s exact `-I …/spectre -I …/alps` (verified to
load `/alps`). Either way: ⚠️ **never let the spectre tree win on an ALPS run.** Confirm which
`toplevel.scs` ALPS opened by grepping the run log if ever in doubt.
*(Aside: `input.scs` also `ahdl_include`s absolute user VA: `…/Test_NDIV_mpw/veriloga/veriloga.va`
and `…/LDO_model_2/veriloga/veriloga.va` — they already carry an `LDO_model_2` behavioral model.)*

---

## 2. Donau — the cluster scheduler (`dsub`)

Company-internal scheduler; submission cmd `dsub`. **LSF-`bsub`-flavored** but a company fork,
so syntax must be verified not assumed.

### 2a. The one command we have (✅ from Cadence Job Setup → Distribution method = Command)
```
dsub -A ug_rfic.rfSClass -q short -R "cpu=8;mem=8000"
```
| token | ✅/🔶 | reading |
|---|---|---|
| `dsub` | ✅ | Donau submit (≈ LSF `bsub`) |
| `-A ug_rfic.rfSClass` | 🔶 | account / project / app-class. `ug_rfic`=project/allocation group; `.rfSClass`=sub-class (RF-Spectre class?) |
| `-q short` | ✅ flag, 🔶 set | queue named `short` (other queues ❓) |
| `-R "cpu=8;mem=8000"` | ✅ | resource req: **8 CPUs, 8000 MB RAM**; `;`-separated `key=value` (Donau syntax, NOT LSF `rusage[]`) |

### 2a′. Donau command family (✅ verified — `ls` of `dsub`’s bin dir on `sinct03-hs`)
`dsub` submit · `djob` status(≈`bjobs`) · `dkill` cancel(≈`bkill`) · `dpeek` tail running
job(≈`bpeek`) · `dmod`/`djctl` modify/control · `dacct` accounting+exit code(≈`bacct`) ·
`dqueue` queues(≈`bqueues`) · `dnode`/`dlstat`/`dcluster`/`dtopo` node/load/cluster/topology ·
`dattach`/`dlogin` attach/login to node · `dversion` version · `dacct dadmin dconf dconfig
dlicense dlimit drespool duser` admin/config/licensing.

⚠️ **Help flag is NOT `-help`** — `dsub -help` → `Error: invalid option or param. Unexpected
argument "help".` (parser strips the dash, then `help` is unexpected). Try `dsub --help` /
`dsub -h` / bare `dsub` instead. ❓ confirm which prints usage.

**Company host/workarea (✅, for locating real run dirs):** host `sinct03-hs`, user `w84368867`,
project `Hi1108V100_Pilot_C1Xplus`, workarea `/data/RFIC3/Hi1108V100_Pilot_C1Xplus/w84368867/workarea`.

### 2a″. `dsub` flags that matter to us (✅ from `dsub --help`)
| flag | meaning | use |
|---|---|---|
| `-R,--resource "cpu=N;mem=M"` | per-task resources; **mem default unit = MB**; default `cpu=1;mem=128MB` | `-R "cpu=8;mem=8000"` = 8 cores / 8 GB |
| `-A,--account` | resource account (e.g. `root.balong1`) | `-A ug_rfic.rfSClass` |
| `-q,--queue` | work queue (e.g. `root.default`) | `-q short` |
| **`-I,--interactive`** | block, attach status+logs to session; **session end ⇒ job killed** (≈`bsub -I`) | foreground/synchronous submit |
| **`-Kc,--blockcontinue`** | block (status only); **job survives session disconnect** | |
| **`-Kco,--blockcontinue_output`** | block + stream logs; survives disconnect | safest blocking for our script |
| `-o/-oo,--stdlog[_override]` · `-e/-eo` | stdout / stderr to log file (append / override) | capture sim log |
| `-J,--json` | JSON output | parse job id when async |
| `-EP,--execPath` | task working dir on node | cwd for relative output |
| `-x,--env` | env propagation: `none\|all;VAR=val;…` | ⚠️ maybe need `-x all` so ALPS/license env reaches node |
| `-T,--job_timeout` | job wallclock limit (s), default 0 = none | guard runaway |
| `-D,--dependency` | e.g. `1874=SUCCEEDED` | chain jobs |
| `-N,--replica` / `-t,-tr` | task copies (array) + per-task timeout/retry | could map corner sweep → tasks |
| `-p,--priority` 1–9999 · `-l,--labels` · `-aff numa[…]` · `-ex` exclusive | priority / node labels / NUMA / exclusive | tuning |
| `-tpn,-nn,--mpi,--job_type` | tasks-per-node / nnodes / MPI type | **N/A for ALPS** (shared-mem `-mt`, single task) |

**Block-vs-async (❓ via 1D):** the working Cadence Command has none of `-I/-Kc/-Kco`, so either
`dsub` blocks by default or Cadence wraps it. For OUR own CLI golden, submit blocking with `-I`
(dies with script) or `-Kco` (survives), one task, `-R "cpu=8;mem=8000"`, and run `alps -mt 8`.

### 2b. How Cadence “Command” distribution wires it (🔶 standard ADE behavior)
ADE/Maestro **prepends** the Command string to each per-point simulation invocation:
```
dsub -A ug_rfic.rfSClass -q short -R "cpu=8;mem=8000"  <the alps/spectre run cmd …>
```
→ **one Donau job per sim point**, each landing on a node with 8 cores / 8 GB.
**Implication for `-mt`**: the wrapped ALPS should run `-mt 8` to use the 8 requested cores
(unless Donau exports a var ALPS auto-reads). ❓ verify.

### 2c. ❓ Open on Donau
- Does `dsub` **block** until the wrapped command finishes (required for Cadence Command method —
  else ADE thinks the sim ended instantly)? Or is it async + a wait/`-I` flag? 🔶 likely blocks.
- Donau command **family** for status/kill/queues/hosts (≈ LSF `bjobs/bkill/bqueues/bhosts`):
  names unknown — guess `dstat`/`djob`/`dkill`/`dquery`/`dhist`/`dnode`. Need the real set.
- Valid `-R` keys beyond `cpu`/`mem` (gpu? tmp? span/host? walltime?).
- What `-A` accepts; list of queues; default queue.

---

## 3. The target flow (what we’re building toward)
Mirror the local Spectre-CLI golden, but on the cluster:
```
emit Spectre netlist (input.scs)  →  dsub -A … -q short -R "cpu=8;mem=8000"  alps … input.scs -o <out>
                                  →  (wait)  →  read PSF (binpsf.py)  →  npz  →  fit
```
Unknowns to close before this runs: ALPS output format/flags (§1b) + dsub block/status (§2c).

---

## 4. Maestro → Donau, as configured (✅ from an OCN export of `sim_NDIV_Interference`)
Reference test: lib `sim_1108_yusheng`, cell `sim_NDIV_Interference`, view `config`, sim `spectre`
(+APS turbo). Project sim dir: **`ocnxlProjectDir = /tmpdata/RFIC/rfic_share/w84368867/simulation`**.
PDK model path (both engines):
`…/pdk/Ver_Plus_1.0c/CF710_Plus_RFPDK_0818_1P10M_7X1Z1U_V1.0c/models/c1x_plus_20251210/{alps,spectre}`,
sections e.g. `TOP_TT_RFTYP`, `Pre_Sim`, `Noise_Typical`.

`ocnxlJobSetup` (✅ verbatim fields):
```
distributionmethod = Command
jobsubmitcommand   = dsub -A ug_rfic.rfSClass -q short -R "cpu=8;mem=8000"
name               = Short_rfSClass        jobruntype = ICRP
maxjobs = 20   runpointsvalue = 5   usesameprocess = 1
defaultcpuvalue = 1   defaultmemoryvalue = 1000   providecpuandmemorydata = 1
scaleestimatedbycpu = 100   scaleestimatedbymemory = 100
configuretimeout = 300   lingertimeout = 300   runtimeout = -1   starttimeout = -1
```
- `-R "cpu=8;mem=8000"` is **hardcoded** in the submit string → fixed 8c/8GB; the
  `defaultcpu/mem=1/1000` are only fallback estimates. `providecpuandmemorydata=1` lets ADE pass
  cpu/mem to the resource manager but the explicit `-R` wins.
- `usesameprocess=1` + `lingertimeout=300` → ADE reuses a lingering process across points.
- Output format: `saveOption(?simOutputFormat "psfxl")` (see §1b gotcha).

## 5. How to see the EXACT command Maestro runs on submit
The OCN is the *setup*, not the runtime command. To capture what ADE actually assembles
(`dsub … spectre/alps … input.scs …`), inspect the run dir after a submit. Each point gets a
`netlist/` dir (with `input.scs`, `runSimulation`/`.runObj`, and a sim log that echoes the full
command line). Probes (csh, on the company box):
```csh
# the project sim tree from the OCN:
set P = /tmpdata/RFIC/rfic_share/w84368867/simulation
ls -dt $P/*/ |& head
# find where the dsub wrapper + sim command are recorded for the last run:
grep -rinE 'dsub |spectre |alps |simOutputFormat|psfxl' $P |& head -40
# a per-point netlist dir usually holds the runnable command + log:
find $P -maxdepth 6 -type d -name netlist |& head
# then: ls one of them, and head the runSimulation / *.out / CDS.log that echoes the cmdline
```
Other ways: ADE-XL **Job Monitor** shows the submitted command per job; `djob`/`dpeek` show the
live Donau job + its stdout once submitted.

## 6. Status — what’s closed vs still open
**✅ Closed (feasibility proven):**
- Engine is user-selectable (“Use ALPS” checkbox, not in OCN) → for CLI we call `alps` directly (§0).
- Exact assembled per-point command captured → our CLI template (§1c).
- Output format = classic PSF, `binpsf.py` reads it unchanged; npz firewall holds (§1b).
- Donau command family, `dsub` flags, blocking modes, Job Setup, Donau≡CCS (§2, §4).

**❓ Still open (small, non-blocking):**
- `dsub` default block-vs-async (1D): `time dsub -A ug_rfic.rfSClass -q short -R "cpu=1;mem=100" sleep 5`.
- Env propagation to compute nodes (need `-x all`? do nodes auto-source the csh profile / EDA env?).
- For a *standalone* CLI golden (netlist NOT generated by ADE): does `alps` need a separate
  VA/AHDL compile step, or is `-ahdllibdir <prebuilt>` enough? (ADE pre-compiles into `sharedData`.)
- Other `-format` values ALPS accepts: `alps -help-option |& grep -iE 'format|psf|output'`.

## 7. Synthesized end-to-end (our CLI golden, mirroring the local spectre_cli path)
From a netlist dir holding `input.scs` (+ a compiled `-ahdllibdir`), submit one blocking Donau job:
```csh
dsub -A ug_rfic.rfSClass -q short -R "cpu=8;mem=8000" -I \
  alps input.scs -format ps -o ../psf \
       -I <pdk>/c1x_plus_20251210/alps \   # alps tree ONLY → unambiguous .alps models (§1d)
       -ahdllibdir <run>/sharedData/CDS/ahdl/input.ahdlSimDB -mt 8
# then: poll ../psf/.simDone  →  binpsf.py reads ../psf/{ac.ac,noise.noise}  →  npz  →  fit
```
- `-I` (dsub) blocks until the sim ends (or drop it and poll `../psf/.simDone`). `-R cpu=8` ↔ `-mt 8`.
- **Models (§1d):** pass **only the `/alps` `-I`** so the bare `include "toplevel.scs"` can only
  resolve to the alps selector. (To match production byte-for-byte instead, replicate ADE’s
  `-I …/spectre -I …/alps` — ALPS last-wins picks `/alps` anyway.) Never psfxl; keep `-format ps`.

## 8. Requirement — keep BOTH engines (ALPS *and* Spectre)
**User requirement (2026-06-15):** mostly ALPS, but **retain the Spectre pipeline** — ALPS can
have bugs and they occasionally switch back to Spectre. So any CLI wrapper we build must be
**engine-parameterized** `engine ∈ {alps, spectre}`, mirroring the ADE “Use ALPS” checkbox:

| | **ALPS** (Empyrean) | **Spectre** (Cadence, +APS) |
|---|---|---|
| binary | `alps` | `spectre` |
| netlist | `input.scs` (positional) | `input.scs` (positional) |
| model `-I` | `<pdk>/c1x_plus_20251210/**alps**` | `<pdk>/c1x_plus_20251210/**spectre**` |
| output dir | `-o ../psf` | `-raw ../psf` |
| **format → classic PSF** | `-format ps` ✅ | `-format psfascii` (or `psfbin`); **NOT psfxl** |
| threads | `-mt 8` | `+mt=8` |
| turbo | (native FastSPICE) | `+aps` / `uniMode APS` |
| `-64` | n/a (64-bit only) | keep for VA compile if `gnu/stubs-32.h` (see [[spectre-va-compile-64bit]]) |

- The **engine choice drives the model `-I`** (§1d) — alps tree for alps, spectre tree for spectre.
- Both wrapped identically by `dsub -A … -q short -R "cpu=8;mem=8000"`; both produce **classic PSF**
  `binpsf.py` reads (force classic on the *spectre* side — its ADE default is psfxl, unreadable).
- We **already have a working Spectre-CLI golden locally** (`cadence/spectre_run.py`,
  `score_spectre.py`, `bench_spectre.py`); the company Spectre path = that + company model paths +
  the dsub wrapper. ❓ capture a real company `spectre.out` (uncheck “Use ALPS”, run) to nail
  Spectre’s exact flags/raw-dir under this PDK — analogous to §1c for ALPS.
