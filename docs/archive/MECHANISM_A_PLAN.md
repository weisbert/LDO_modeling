# Mechanism A — ADE-native in-situ LDO extraction (BUILD PLAN)

**Status:** planned 2026-06-13, to be built in a fresh ultracode conversation.
**Owner handoff:** see the prompt at the bottom.

## Goal

Make the **validated in-situ multi-port extraction** (today only runnable via the
standalone `spectre -64` CLI in `cadence/extract_pmu.py`) run **through ADE-XL/Maestro**
instead — so it is (a) visible in Maestro, (b) rides the company's existing
ADE-XL **Job Setup → cluster** dispatch unchanged, and (c) exercises the real
**PSF → npz** production path. Everything downstream of PSF (the npz contract firewall,
the pure-Python fit, the Verilog-A emit) stays engine/launcher-agnostic.

This is **mechanism A** of the two agreed paths (A = ADE-native; B = netlist-append,
deferred). Run-trigger transfer to production is already confirmed: `axlRunAllTests`
is "equivalent to manual click" and inherits the session's simulator(ALPS)+cluster
Job Setup (see memory `simkit-mechanism-a-reference`).

## Acceptance criteria (from the user, verbatim intent)

1. **Programmatic end-to-end, no human/Claude intervention.** A single headless entry
   point runs the whole flow (augment → run-in-ADE → collect PSF → npz → fit) against a
   live Maestro session and produces the fitted model + report. No manual SKILL typing.
2. **A GUI to hand-test it.** A desktop GUI (same Qt binding simkit already runs on this
   box) lets the user click through the flow on the **experimental machine** to confirm it
   runs there; then re-run the *same* GUI at the **company on Monday** against the real PMU.

Both criteria are two views of the SAME core: a manifest-driven, engine-agnostic library;
the CLI (criterion 1) and the GUI (criterion 2) are thin layers over it.

## Integration posture (decided 2026-06-13)

LDO modeling stays **standalone** (NOT integrated into simkit) for now; the user will
consider folding it into simkit **later, once mature**. So copy simkit's proven pvtRunner
call sequence into our own `insitu_run.il` — do NOT import/depend on the simkit package.

**There is ALREADY an LDO-modeling GUI** on `main`: `gui/ldo_modeler.py` — a Qt-free
`ModelerCore` (import→fit→predict→emit, fully `--selftest`-able headless) under a thin
5-tab PyQt5 shell (Profile · Import · Fit · Compare · Trans-ID), plus a `deploy/` offline
airgap pipeline. Its architecture (Qt-free core + thin shell + selftest) is exactly the
seam we want — **mechanism A EXTENDS this GUI; it does NOT build a new one** (see P7). The
new in-situ Extract front-half produces the SAME npz the existing Import tab already
consumes, so the two converge at the npz firewall.

**Branch unified (2026-06-13):** all of this now lives on `main` (merge `10845f3` brought
the Cadence/Spectre bring-up + PMU stand-in onto main alongside the GUI/deploy line). Build
mechanism A on `main`. The old `target-b-cadence-bringup` branch is superseded.

## North-star invariant (why Monday is "just a new manifest")

Everything is driven by a **pin-role manifest** + the **designer's existing TB/corner
table**. We only ever *append* extraction stimuli + saves at manifest-tagged DUT-boundary
pins; we never reconstruct the OP, hunt/mutate the designer's sources, or hardcode pin
names / corner names / process sections. So porting from the stand-in (`PMU_top`, Spectre,
local) to the real PMU (ALPS, cluster) is: **swap the manifest + point at the real TB.**

## What we REUSE (do not rebuild)

| Asset | Role in mechanism A |
|---|---|
| `cadence/extract_pmu.py` | The **proven analysis recipe** (8-point matrix: Zout/PSRR×2sup/noise/coupling/admittance/current-PSRR). Port the *stimulus+read logic*, re-route from CLI to ADE. |
| `cadence/psf.py` | PSF reader — unchanged. |
| `cadence/skill_lib.py` | The skillbridge `Workspace.open()` pattern — run-drive uses the same `ws['axl...']()` calls. |
| `cadence/import_cadence.py` (main's 457-line GUI version: `assemble(profile,files)` / `from_psf(path,kind,fmt)` / `validate` / `match_dir`) | The npz contract writer the GUI uses — **add multi-port functions alongside**, don't break the GUI's existing single-port API. |
| `gui/ldo_modeler.py` (`ModelerCore` Qt-free core + 5-tab shell + `--selftest`) + `deploy/` | **The GUI to EXTEND** (criterion 2): add an Extract tab + `ExtractCore`; generalize `ModelerCore` import/fit/compare to multi-port. Keep the Qt-free-core architecture + airgap pipeline. |
| `harness/fit_model.py` | The per-output fitters (zmodel/psrr/noise) — **reuse as building blocks** in a multi-port loop. |
| `simkit/skill/pvtRunner.il` | The **run-drive reference**: `axlRunAllTests` Submit → poll `axlGetRunStatus` → `axlSetHistoryName`; test/corner enable via `axlGetTests`/`axlGetCorners`/`axlSetEnabled`. Borrow the proven sequence (don't depend on the whole package). |

## The 8-point measurement matrix (from extract_pmu, mapped to ADE)

One shared OP; each ADE point sets exactly one `acm_*` design variable = 1 (AC
superposition, DC untouched). Read multiple probes per run.

| ADE point | hot var | analysis | read | contract arrays |
|---|---|---|---|---|
| sup_1p0 | acm_vd0 | ac | V(out_pll),V(out_vco),I(probe_i500n:p),I(probe_i1u:p) | p_pll_1p0, p_vco_1p0, pi_*_1p0 |
| sup_1p8 | acm_vd8 | ac | same | p_pll_1p8, p_vco_1p8 |
| inj_pll | acm_iinj_pll | ac | V(out_pll),V(out_vco) | z_pll, couple_pll_vco |
| inj_vco | acm_iinj_vco | ac | V(out_vco),V(out_pll) | z_vco, couple_vco_pll |
| noise_pll | — | noise@out_pll | out | noise_pll |
| noise_vco | — | noise@out_vco | out | noise_vco |
| y_500 | acm_b500 | ac | I(probe_i500n:p) | y_i500n |
| y_1u | acm_b1u | ac | I(probe_i1u:p) | y_i1u |

Stimulus/read rules (the manifest dual): v_out → inject 1 A AC isource + save node V;
supply → series-insert 1 V AC vsource + save out nodes / probe currents; i_out → insert a
**named** probe source + save `probe:p` (currents need **explicit** save, not `allpub`).

---

## Phases

### P0 — Scaffold + confirm environment (small)
- New package `cadence/insitu/` (manifest, augment, run, import, cli, gui) + SKILL under
  `cadence/skill/insitu_*.il`.
- Confirm: skillbridge live to `fnxSession0`; **PyQt5 importable** (the GUI's binding —
  check on the experimental box; this dev VM may lack it, and the `--selftest` path stays
  Qt-free regardless); **`python gui/ldo_modeler.py --selftest` passes** (post-merge
  integrity check that merge `10845f3` didn't break the GUI's `ModelerCore`); `Test_PMU` opens.
- **Deliverable:** `insitu/__init__.py`, package importable; a `insitu doctor` CLI that
  prints session + Qt + DUT availability.

### P1 — Pin-role manifest (the contract for "designer tells us roles")
- `insitu/manifest.py`: JSON schema + loader/validator. Per DUT pin →
  `{role: supply|v_out|i_out|bias|leave_alone, stim: {kind: inject|series|probe, dc: ...},
  net: <name>}`; plus a `corners: {pull_from_session: true}` reference (capture, don't
  construct) and `dut: {lib, cell, tb_lib, tb_cell, inst}`.
- Write the **stand-in manifest** `insitu/manifests/pmu_top.json` (we know PMU_top's roles
  → this *is* the "designer-supplied" stand-in).
- (Phase-2 nice-to-have, stub now) `propose_manifest()`: DC/AC probe → draft roles for the
  designer to confirm.
- **Deliverable:** load+validate pmu_top.json; round-trips; unit tests.

### P2 — ADE augmentation engine (SKILL via skillbridge)  ← the heart
- `cadence/skill/insitu_augment.il` + `insitu/augment.py`:
  - Build/refresh an **extraction TB schematic** `<tb_lib>/<tb_cell>_extract` = copy of the
    designer's TB + our sources/probes with `acm_*` AC-mag **design variables** (default 0):
    inject isources at v_out, series vsources at supplies, named probe sources at i_out.
    (SKILL: `schCreateInst`/`schCreateWire`/`schCreateWireLabel` — cite via virtuoso-skill.)
  - Set up ADE-XL: add `ac` + `noise` analyses, declare the `acm_*` variables, define the
    8 points (as ADE points/corners over the `acm_*` set), set **targeted saves** (manifest
    pins + probe `:p`, NOT allpub). (SKILL: `asiSetSimOptionVal`, analysis setup, save APIs
    — cite via virtuoso-skill.)
- **Deliverable:** running `insitu augment --manifest pmu_top.json` leaves `Test_PMU_extract`
  set up and visible in Maestro with 8 points + saves; idempotent re-run.

### P3 — Run-drive (reuse simkit pattern)
- `cadence/skill/insitu_run.il` + `insitu/run.py`: `axlRunAllTests` Submit → poll
  `axlGetRunStatus` to idle → `axlSetHistoryName`; then resolve the results dir, trying
  **both** `<corner>/<test>/psf/` (ALPS) and `.../netlist/` (Spectre) subdirs.
- On this box: runs local Spectre via Maestro (visible in history). On the company box: the
  *same call* inherits Job Setup → cluster+ALPS.
- **Deliverable:** `insitu run-only` triggers + waits + returns the PSF tree path; a Maestro
  history appears.

### P4 — PSF → multi-port npz (generalize the firewall)
- Extend `cadence/import_cadence.py`: add `assemble_multiport(...)` + `from_psf_multiport(
  root, manifest)` emitting the generalized schema (`z_<o>_<load>`, `p_<o>_<s>_<load>`,
  `noise_<o>_<load>`, `y_<c>_<load>`, `pi_<c>_<s>_<load>`, `couple_<a>_<b>_<load>`) — the
  SAME schema `extract_pmu.py` writes. Handle the corner/`<load>` axis from the pulled table.
- **ACCEPTANCE GATE (criterion-1 core):** the ADE-path npz must match the validated CLI
  `results/ref/pmu_standin.npz` within tolerance (same DUT, same physics, different launcher).
  This proves mechanism A reproduces the trusted result via the real PSF path.
- **Deliverable:** `results/ref/pmu_standin_ade.npz` ≈ `pmu_standin.npz` (assert in a test).

### P5 — Multi-port fit + report (current error separate)
- `harness/fit_multiport.py`: loop the existing `fit_model` per-output fitters over each
  v_out (Zout, PSRR×supplies, noise); add a current-port fit (admittance `y_<c>`,
  current-PSRR `pi_<c>_<s>`). Emit per-output Verilog-A (reuse the emit path) + a **report
  that breaks out current-port error separately** from voltage-port error.
- **Deliverable:** `fit_multiport --variant pmu_standin_ade` produces models + a report
  table (voltage ports vs current ports, error per metric).

### P6 — End-to-end CLI  ← **acceptance criterion 1**
- `insitu/cli.py`: `python -m insitu run --manifest pmu_top.json --session fnxSession0`
  chains P2→P5 headless, no intervention, exits non-zero on any gate failure. Prints a
  one-screen summary (points run, npz path, fit error table, pass/fail vs CLI baseline).
- **Deliverable:** one command, cold→done, on the experimental box.

### P7 — EXTEND the existing GUI  ← **acceptance criterion 2**
**Do NOT build a new GUI — extend `gui/ldo_modeler.py`.** It already has a Qt-free
`ModelerCore` + a 5-tab shell (Profile · Import · Fit · Compare · Trans-ID) + `--selftest`
+ a `deploy/` airgap pipeline. Two additions:
- **New `ExtractCore` (Qt-free)** wrapping P1–P4 (manifest → augment → run-in-ADE → PSF →
  npz) via skillbridge — same headless-testable discipline as `ModelerCore`. It PRODUCES the
  npz that the existing **Import** tab already consumes (the two paths converge at the npz).
- **New tab "0 · Extract (in-situ)"**: load/edit the pin-role manifest (tag pins, stim kind);
  "Build & Run" → augment+run via skillbridge with live status (poll `axlGetRunStatus`) +
  Maestro history link; on success hand the npz to the Import→Fit→Compare flow.
- **Generalize `ModelerCore` + Fit/Compare tabs to multi-port** (P5): per-output Zout/PSRR×sup/
  noise + current-port admittance/current-PSRR; overlay + fit-error table break out current
  ports separately. Keep the Qt-free-core + `--selftest` discipline (add selftest coverage).
- Runs offline on the experimental box (PyQt5 is on the red box; this dev VM may lack it — the
  `--selftest` path must stay Qt-free). Monday: same GUI, load a different manifest.
- **Deliverable:** launch `gui/ldo_modeler.py`, click Extract→Import→Fit→Compare end-to-end on
  PMU_top, see multi-port results; `--selftest` green.

---

## Experimental box  vs  Monday (company)

| | Experimental box (now) | Company (Monday) |
|---|---|---|
| DUT | `sim_yusheng/PMU_top` (behavioral) | real PMU top |
| manifest | `pmu_top.json` (we author = stand-in) | real PMU manifest (designer-tagged) |
| simulator | Spectre, local via Maestro | ALPS, cluster via Job Setup |
| run trigger | `axlRunAllTests` | **same call** (inherits Job Setup) |
| code | all of insitu/ | **unchanged** — only the manifest + DUT differ |

## Monday unknowns to confirm at the company (don't block the build)
1. ALPS result dir layout (`psf/` vs `netlist/`) — P3 already tries both.
2. ALPS analysis syntax for `ac`/`noise` (and later `pss`/`pac`/`pnoise`).
3. ALPS Verilog-A compile (else `.lib` fallback) — only matters when the model goes back in.
4. The designer's real corner table shape (pull via `axlGetCorners`).

## Risks / notes
- **fit_multiport scope:** reuse fit_model's fitters as-is in a loop; do NOT rewrite the
  fitter. Current-port fit is new but simple (1-pole admittance + flat current-PSRR).
- **Schematic-edit fragility:** prefer adding sources to a *copy* TB (`_extract`) so the
  designer's spine is never mutated. All SKILL cites virtuoso-skill index (mandatory).
- **Saves:** explicit current saves; targeted not allpub (PSF bloat on a real PMU).
- **Stand-in is LTI** → single nominal corner only on this box; the corner-axis plumbing is
  built and exercised but degenerate until the real (transistor) PMU on Monday.
