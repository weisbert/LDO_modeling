# BUILD COMPLETE — GUI modeler + offline airgap deploy (2026-06-07)

Implements `GUI_DEPLOY_PLAN.md` (all 5 phases). The modeler is a **thin shell over the existing,
validated harness**; the offline pipeline ships it to an airgapped CentOS7/glibc-2.17 red zone.

## What was built

| Area | Files | What |
|---|---|---|
| Harness refactor | `harness/fit_model.py` | NEW `predict(P_il,f)` (analytic Zout/PSRR/noise), `FitResult` + `fit_variant()` in-process entry, de-hardcoded `121u`→`NOMINAL` (middle corner / profile override), `VREF` param (was 1.05), `_amps()` corner-key helper, `--selftest`. |
| Importer | `cadence/import_cadence.py` | Cadence/ADE CSV+PSF-ASCII → `results/ref/<name>.npz` (mirrors `CADENCE_EXTRACTION.md`); complex-format auto-detect (reim/mag·deg/mag·rad/dB·deg); `validate()` guardrails. |
| GUI | `gui/ldo_modeler.py` | PyQt5 4-tab shell (Profile / Import / Fit / Compare) over a Qt-free `ModelerCore`; matplotlib Qt5Agg; fit on a `QThread`; analytic `predict` overlay; measurement-guidance dialog; self-contained `--selftest`. |
| Deploy | `deploy/` | `audit_wheels.py` (glibc-2.17 gate), `package.py` (yellow-zone full/incremental bundler), `bootstrap.sh`/`update.sh` (red-zone install/update), `dryrun_manylinux2014.sh` (Docker rehearsal), `requirements-gui.txt`, `README.md`. |

## Validation evidence

**Phase 1 — zero numerical change (the hard gate):**
- 14-variant matrix re-run vs fresh baseline: **0.00 composite delta on every variant** (and all
  sub-metrics). Baseline composites: base 3.88, …, wp_big 7.75, v1_nmos 8.85.
- Emitted `.lib` files **byte-identical** (base/v4/v3/v1/v5). (`.va` differs only in cosmetic float
  formatting, e.g. `121e-6`→`1.210000e-04`, numerically identical; `.va` is not scored.)
- `predict()` self-test: matches the fitter's analytic zrms/prms/npsd to <1e-9 on 5 variants.

**Phase 2/3 — importer + GUI + guardrails:**
- Importer round-trip (export ref→CSV→re-import): **0.00 relative error**, spurs preserved, re-fit + selftest pass.
- Guardrails **fire** on all three documented silent-mismatch corruptions (PSRR-as-dB, noise-as-V²/Hz,
  Zout sign-flip) and stay **silent on clean data**.
- GUI offscreen Qt selftest passes (PyQt5 5.15.10 / Qt 5.15.2); Compare tab renders GT-vs-model overlay.

**Phase 4/5 — offline deploy:**
- Cross-downloaded the red-target wheels and **AUDIT PASS: 15/15 wheels ≤ glibc 2.17** (incl. scipy
  1.15.3, matplotlib 3.9.4, pillow 12.2.0, PyQt5-Qt5 5.15.2 — resolves the plan's open pin questions).
- Auditor **negative test**: rejects manylinux_2_28/_2_31, musllinux, bare-linux, wrong-arch (exit 1).
- Full (145.9 MB) + incremental (80 KB) bundles build with `requirements.lock` + `MANIFEST` + sha256.
- **Red-box smoke is self-contained**: running the bundled `app/` with no `results/ref` synthesizes an
  analytic reference and completes import→fit→predict→emit→Qt render. (`gui --selftest` needs no shipped data.)
- Container dry-run (`dryrun_manylinux2014.sh`, `--network none`) is written for where Docker exists
  (none on this box); the wheel audit already proves the offline install is glibc-2.17-valid.

## Adversarial review round (Phase 5) — 13 findings fixed

A 4-dimension multi-agent review (numerical / GUI / importer / deploy), each finding adversarially
verified against the code, surfaced **13 confirmed issues (3 refuted)**. All fixed + functionally
re-tested:

- **CRITICAL** — GUI Import button called `_apply_profile()` which rebuilt the grid and **wiped every
  file picker** → always "No files selected". Fixed: grid rebuilds only when corners change *and*
  preserves picked paths; selftest now asserts the button path (22 files survive apply).
- **HIGH** — `fit_all()` parsed corner keys with `float(il.replace("u","e-6"))`, **crashing on any
  non-µA corner** (e.g. a mA LDO). Fixed: one canonical `ng.amps()` (p/n/u/m/k suffixes) at all 6
  sites; **proven by fitting a `10m/50m/100m` LDO end-to-end**; matrix still **0.00 delta**.
- **HIGH** — incremental packager never enforced the req-hash guard. Fixed: stores `input_req_hash`,
  **aborts** when `requirements-gui.txt` changed (verified both directions).
- **HIGH** — `update.sh`'s `rm -rf app` wiped user outputs (app wrote `results/`,`model/` under `app/`).
  Fixed: persistent `$PREFIX/{results,model}` symlinked into `app/`, re-linked on every update.
- **MEDIUM** — emit DUT-desync (fixed: result invalidated on re-import; emit refuses a stale fit),
  `_detect_fmt` "rad" substring (→ token test), spur_F from first vs nominal corner (→ nominal).
- **LOW** — selftest screenshot now asserted; ambiguous complex-format raises; 2-col/PSF complex guard;
  noise V²/Hz two-tier heuristic; **MANIFEST sha256 integrity verified in `bootstrap.sh`** (catches
  corruption — verified).

## Run it

```
# GUI (dev):           python gui/ldo_modeler.py
# headless smoke:      QT_QPA_PLATFORM=offscreen python gui/ldo_modeler.py --selftest --require-qt
# build airgap bundle: python deploy/package.py full --out dist/
# red-zone install:    ./bootstrap.sh /opt/ldo_modeler     (then update.sh for code-only refreshes)
```
See `deploy/README.md` for the full two-zone runbook.
