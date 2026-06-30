# TOOL_FACTS — environment & tool pitfalls (with the exact fix)

> Durable archive. Each entry: the pitfall → the exact fix. Curate-only, no status.

## Spectre / Verilog-A

- **Compile VA only with `-64`.** `spectre -64` / `ahdlcmi -64`; else it compiles `gcc -m32` and dies on `gnu/stubs-32.h`. Local Spectre 18.1 binary `/home/yusheng/Program/eda/cadence/SPECTRE181/bin/spectre`, env in `cadence/spectre_run.py::_env()`, gate with `spectre_run.available()`. (Spectre 18.1.0.077 / SPECTRE181, Virtuoso IC618, skillbridge 1.8.0.)
- **Compile `.va` on COPIES in a scratch dir.** openvaf/lld drops an import-library `<name>.lib` beside its output → clobbers the emitted SPICE model if you compile in `model/`.
- **No `laplace_nd`** — synthesize passive RLC + controlled sources only (PSS/HB-robust); use native `white_noise`+`flicker_noise` for exact 1/f (valid in pnoise/hbnoise).
- **OpenVAF `$table_model` unsupported** (rc=65) → inline the dropout as closed-form sum-of-max PWL (== 1-D linear interp). `parameter string` table path crashes (rc=101 panic) → emit the absolute path literally, or inline. `$table_model` writes a BARE filename resolved against the run dir → embed the emit-time ABSOLUTE path.
- **VA `$temperature` is KELVIN** (328.15 = 55°C); `emit_pmu_model` uses `$temperature-328.15`, the `emit_isrc` ngspice twin uses degC `temper-55` — they MUST move in lockstep or crossval diverges.
- **VA numeric:** `(V/vk)^p` blows the OP Jacobian at Vo=0 when p<1 → sqrt-floor the base.
- **Emitted `.va` PSRR sign was inverted 180°:** `I(vout)<+X` removes current FROM vout but the `.lib` mirror `Gd 0 vout` injects INTO it → negate the PSRR `I(vout)<+` contributions in `emit_va` (spur tones were already negated; PSRR was missed). Only `ldo_model.va` was hand-fixed — regenerating the other `model/*.va` (esp. v4_ffpsrr) is an open TODO.
- **Emitted PMU module ground is VSS** — tie to 0 in any local TB/model or it floats to −100 MV.
- **Sink PSRR sign:** probe reads `i(vout) = −I_pin` → `gdd_eff = −gdd` (sink) / `+gdd` (source); source drives `I(supply,o)`, sink drives `I(o,gnd)`. importmp stores `pi = −I/Vsup`; emit fits gdd on `−PI`; report must negate to match the `.va`.

## ngspice

- **Built from source** at `~/.local/bin` (v46) — EPEL el8 has no package. `AC_PREREQ([2.69])`, `make LIBS=-lstdc++`. Found via `$NGSPICE` → bundled exe → PATH.
- **BSIM3 → Spectre:** ngspice `level=8` → Spectre `level=49` (8=generic mos8, rejects BSIM3); strip `{param}` braces → bare in subckt body; instance in SPICE lang (`xdut`), stimuli in spectre-lang.
- **`.param` names are CASE-INSENSITIVE** — noise g1/g2/g3 silently overwrote PSRR G1/G2/G3 (35× gain). Fix: rename gnw/gn1..gn6.
- **`ng.amps()` suffix parse:** `float(il.replace("u","e-6"))` crashed on mA corners → use the canonical p/n/u/m/k parser at all 6 sites.

## ALPS / Donau (cluster, red zone)

- **Validated run:** `dsub -A ug_rfic.rfSClass -q short -R "cpu=8;mem=8000" -x all -EP <netdir> -J /software/empyrean/alps/2026.03.hf1/bin/alps input.scs -format ps -o <psf>/<tag> -I <pdk>/alps -ahdllibdir <ahd> -mt 8 -ade`.
- **Call the WRAPPER `.../bin/alps`, not the raw binary** (raw fails `libsvadv.so`; the wrapper sets LD_LIBRARY_PATH).
- **`-format ps` = classic PSF** (hidden flag; ADE's psfxl downgraded to ps for ALPS) → binpsf reads it unchanged. Never `psfxl`.
- **`-ade`** = ADE output names (ac.ac / noise.noise) + the 0-byte `.simDone` completion sentinel; without it, native `.fd/.td` + logFile index.
- **`-mt 8` MUST equal Donau `cpu=8`.** `-x all` propagates the submit-shell env (FlexLM `LM_LICENSE_FILE`) to the node — required for CLI licensing.
- **PDK `-I` is a DIRECTORY** (`$MODEL_ROOT` → `-I $MODEL_ROOT/alps`); a `toplevel.scs` FILE is wrong (→ `-I .../toplevel.scs/alps`). Consumer = `cadence/cluster/alps_cli.py`. Pass only `-I <pdk>/alps` so `include "toplevel.scs"` resolves to the `.alps` selector (never let the spectre tree win on an ALPS run).
- **`dsub --json`** returns `{"data":{"jobId":"…"}}` (numeric-only; ignore requestId).
- **ALPS-keyword caveat:** deck statements (`options temp=`, `noise oprobe=`) are Spectre-18.1-validated but ALPS-keyword-UNVERIFIED — if ALPS silently no-ops, the panel comes up wrong with NO error. After a real run, sanity-check PTAT Idc(T) slopes + non-blank current-noise; the fix lives in `netlist_augment`, not the manifest. (The `nz oprobe=V10 noise...` token-order bug was exactly this: param before the `noise` type → parse error. Correct = `nz {noise} oprobe={probe}`.)

## ADE / skillbridge

- **`axlGetRunStatus` returns `(completed,total)` POINTS, not an idle code.** A finished run RESTS at `(N,N)`, not `[0,0]` — the famous "run hangs" was a misread status. Poll PER-HISTORY (`insituHistStatus`), not the session aggregate (it poisons after renames). Locate PSF via `axlGetResultsLocation`.
- **ADE ASSEMBLER-1610/1707** (missing per-test vars + disabled analyses): design vars are per-test not globals → inherit the OP via the `axlGetToolSession → asiGetSession` bridge (`asiGetDesignVarList`/`asiAddDesignVarList`), then `asiSetAnalysisFieldVal` + `asiEnableAnalysis`.
- **ADE field names:** ac uses `start/stop/dec` (not from/to). Noise needs `outType='voltage'` + `p=net,n=gnd!` + clear `oprobe` + `inType='none'`; the default `outType='probe'` emits `oprobe=<net>` → SFE-1997.
- **ADE session degrades** after ~6 runs + history renames (runs slow 3 s → 197 s, rename-collision modals) → don't rename histories; **Session → Reset** fully recovers.
- **Fresh-session `axlGetCurrentHistory` returns 0**, which is TRUTHY in SKILL → guard in `insituCurHist`/`_cur_hist()`.
- **skillbridge is live-Virtuoso-only** — import it LAZILY inside live functions (eager import crashed the airgapped deploy). A modal Virtuoso dialog WEDGES the whole channel (even `plus(2,3)` times out); killing the client doesn't abort the in-Virtuoso call → close the dialog or restart Cadence. Agents can't pop X11/Qt/CIW — the USER must launch Virtuoso + load the SKILL helpers per session (resolve_nets.il, pmu_top_symbol.il, ldo_cellview.il).
- **Currents need explicit save** (`probe:p`), not `allpub`.
- **OCEAN standalone binary is OS-broken on this box** (`sysname` → "unknown" on RHEL8) → run analyses in the live ADE session, feed PSF/CSV through `import_cadence.py`. Tight CLI loop (no GUI): `spectre tb.scs +escchars =log run.log -format psfascii -raw ./psf`; headless OCEAN `ocean -nograph -replay run.ocn`.

## PSF / binpsf

- **ADE/cluster write BINARY PSF**; `cadence/psf.py` was ASCII-only → standalone big-endian `cadence/binpsf.py`, `psf.read_psf` auto-dispatches on bytes; per-instance STRUCT noise traces handled. 5 sections, big-endian header `…PSFversion…BINPSF…`.
- **PSF axis names (confirmed local Spectre):** dc-sweep axis = `'dc'`, transient axis = `'time'`. `-format psfbin` (binary) / `-format psfascii` (ascii).
- **Windowed transient PSF** (`PSF window size != 0`) is signal-major buffered with a NaN-padded last window — reverse-engineered reader in binpsf.
- **Grouped PSF (groups=1)** is NOT an error wall — it just means every device noise contribution was saved; the VALUE section is the same flat per-point layout, `out` is the last decl (scalar real). Read it directly by constant stride; don't crawl the whole file. (See DATA.md §15 for the exact byte layout.)

## Deploy / install / shell

- **Red box is tcsh:** `VAR=val` errors → use `$PWD`; backticks/`|&`/`set`. `/opt` is unwritable on the shared box → install self-contained under one user folder.
- **Install PREFIX** = `/data/RFIC3/Hi1108V100_Pilot_C1Xplus/w84368867/workarea/LDO_modeling`; update via `bash apply` (auto-detects incremental/full). `~/.ldo_modeler/` is the GUI config dir (distinct). skillbridge==1.8.0 in `.venv`.
- **glibc-2.17 wheel audit:** Windows `pip download` succeeds but CentOS7 import dies `GLIBC_2.28 not found` → cross-download `--platform manylinux2014/_2_17`, REJECT any `_2_28/_2_31/_2_34`. `PyQt5-Qt5` must be `5.15.2` (5.15.11+ needs glibc 2.28).
- **Qt ↔ Cadence conflict:** Virtuoso puts a conflicting `libQt5Core.so.5` on `$LD_LIBRARY_PATH` (`/software/public/qt/5.15.3_xcb/lib`) → PyQt5 dies `symbol _ZdaPvm, version Qt_5`. Fix = prepend the wheel's `PyQt5/Qt5/lib`; launch the GUI from a CLEAN shell (separate process over the skillbridge socket).
- **`deploy/apply` must be LF** (`.gitattributes`) — Windows CRLF gave `set -euo pipefail\r` → "invalid option name". Install launchers atomically (temp + `mv`) so the running script doesn't self-overwrite (`syntax error near '('` after the work succeeded).
- **PowerShell 5.1 zh-CN traps:** save scripts UTF-8 BOM; PS strips embedded `"` to native exes (use a quote-free version probe); text artifacts must be LF (`newline="\n"`) or `sha256sum -c` fails on `\r`; MANIFEST keys via `.as_posix()` (a WindowsPath str = backslash → every file reads "missing" on Linux).
- **`LDO_NOISE_FAST`** env caps the noise fit budget (nfev) + skips ladder/admittance escalation, deploy-smoke only; UNSET on the real box = full budget, byte-identical for converging fits.

## Manifest / coverage

- **`coverage.temps` is the ONLY temperature run axis** (`manifest.temps()`); it MUST be an explicit number list — `manifest._validate_coverage` RAISES on a string. The `start:step:stop` / comma-mixed expansion happens at the GUI/build boundary, not in `temps()`. Tier T4 only selects machinery.
- **Transient label keys:** real npz `tr_<o>_<label>_<load>` (e.g. `tr_pll_2m_tt_25c`); load currents must come from manifest `coverage.transient.steps`, NOT parsed from the opaque key. `_settled_step` must search the edge in `[15%,98%]` of span, else a t=0 startup drop hijacks argmax onto startup (0 settled pts). The digest DROPS transient arrays → fit the DC/vreg layer from the FULL run npz, not the digest.
- **Self-fulfilling-test trap:** lock tests that hand-fabricate the input shape the code wants (vreg key-format, Idc(T) temps) PASS while the real manifest path fails — always drive the REAL manifest.

## Misc python / numeric

- **`np.trapz` is GONE** in this numpy — use a manual trapezoid.
- **Headless Qt screenshots:** `QT_QPA_PLATFORM=offscreen` + monkeypatch `QDialog.exec_` to `grab().save()`.
