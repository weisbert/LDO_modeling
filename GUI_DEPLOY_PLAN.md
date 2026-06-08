# Handoff — Company-side GUI modeler + offline (airgap) deployment

**Status: PLANNING COMPLETE, build is the next step (open in ultracode).** This is the
**manual-TB → modeler** path: the engineer hand-builds the testbench in ADE (red zone, no LDO
auto-extraction yet), exports data, imports into a PyQt5 GUI, generates the `.va`, and compares
before/after — all OFFLINE in an airgapped red zone. Complements the Cadence auto-extraction
bring-up on branch `target-b-cadence-bringup`.

This doc is self-contained and executable. Decisions below are LOCKED (the user chose them) — do
not re-litigate; build to them.

---

## 0. LOCKED decisions (user-confirmed this session)

| Decision | Value | Consequence |
|---|---|---|
| **Red zone OS** | Linux, CentOS7-class, **glibc 2.17** (= `manylinux2014`/`manylinux_2_17` baseline) | wheels MUST be ≤ glibc 2.17; see §4 (the #1 risk) |
| **Red zone Python** | **already has Python 3.11** | don't bundle an interpreter; `python3.11 -m venv .venv` |
| **Yellow zone** | Windows, has network | cross-downloads Linux wheels for the red target |
| **Qt binding** | **PyQt5** (5.15.x) | matplotlib Qt5Agg backend; no pyqtgraph |
| **Before/after compare** | **analytic only** | GUI NEVER calls a simulator; `predict(P,f)` from fitted params vs imported GT. Spectre validation stays in CLI (`cadence/score_spectre.py`), out of the GUI |
| **Generalization** | parameterize the characterization recipe | band/corners/vin/token → user "profile"; emit a validity-envelope (see §6, memory `tool-generalization-intent`) |

---

## 1. What the GUI wraps (thin shell over EXISTING code — no new algorithms)

- **Import** → `cadence/import_cadence.py`: `from_csv` / `from_psf` / `assemble` → `results/ref/<name>.npz`.
  CSV layout is fixed in that module's docstring; npz schema source of truth = `CADENCE_EXTRACTION.md`.
- **Fit** → `harness/fit_model.py:fit_all()` returns params `P`; `emit` / `emit_va` write
  `model/<name>.{lib,va}` + `<name>_dropout.tbl`. `fit_cout_esr` auto-extracts Cout/ESR from the
  Zout HF tail.
- **Compare (NEW)** → factor a `predict(P, f) -> {Zout, PSRR, noise}` out of the existing analytic
  transfer functions inside `fit_zout` (`zmodel`), the PSRR bank, and `fit_noise_bank`. This is
  exactly what the fitter optimizes, so the overlay = fit quality, pure numpy, no simulator.

## 2. GUI design — 4 tabs = the engineer's workflow

| Tab | Action | Wraps | Output |
|---|---|---|---|
| **① Profile** | name, nominal vin, 3 load-corner keys, Cout, ESR, **characterization band** | `import_cadence.assemble` args + contract's "3 things to report" | in-memory profile |
| **② Import** | per-corner/per-quantity file pickers (CSV or PSF), "import + preview" | `from_csv`/`from_psf` → npz | npz + raw-data preview plots |
| **③ Fit** | "Fit": per-block residuals (Zout RMS / PSRR band / noise) + params | `fit_all()` → `emit`/`emit_va` | `.va`/`.lib`/`.tbl` + P |
| **④ Compare** | per-corner GT vs model overlay (Zout mag/phase, PSRR dB/phase, noise PSD) | `predict(P,f)` analytic | overlay + per-curve error |

**Import guardrails (§ "silent mismatches" in CADENCE_EXTRACTION.md) — build into Tab ②:**
plot raw imported data immediately + warn on:
1. PSRR stored as dB instead of complex H (|H| in −40..−80 range → warn).
2. Noise given as V²/Hz instead of V/√Hz (offer a "sqrt it" checkbox).
3. Zout sign/direction (nominal corner should peak near the output resonance).

**Tech:** matplotlib `FigureCanvasQTAgg` (matplotlib already a dep; `score.py` uses `Agg` for
headless — GUI uses Qt5Agg). Fit/import run in a `QThread` (seconds-scale, keep UI responsive).
New runtime dep = **PyQt5 only** (+ `PyQt5-Qt5`, `PyQt5-sip`).

## 3. Refactor (zero numerical change — only move CLI shells off importable cores)

- factor `predict(P, f)` (Tab ④).
- make `fit_all()` / import callable in-process (CLI becomes a thin `main()`).
- **generalize the hardcoded `121u` token** → `<nominal>` (`fit_cout_esr` reads literal
  `ref["z_121u_hf"]`; `import_cadence` reads literal `z_121u_hf.csv`). GUI knows the nominal corner
  and writes the hf array under whatever name the fitter expects.
- parameterize band / corners / vin / Vref from the profile (today: `bench.AC="ac dec 40 10 100meg"`,
  `AC_HF="...500meg"`, `NOISE="...dec 20 10 100meg"`, `LOADS=["20u","121u","250u"]`, Vrf=1.05).

---

## 4. ⚠️ #1 RISK — glibc 2.17 wheel audit (this is what breaks the airgap install)

Modern wheels increasingly target `manylinux_2_28` (glibc 2.28). On the **Windows** yellow zone
`pip download` will succeed anyway, but on **CentOS7 (glibc 2.17)** the import dies
`GLIBC_2.28 not found`. The packaging script MUST audit and reject.

**Cross-download (on Windows, for the Linux red target):**
```
pip download -r requirements.lock --dest wheels/ \
    --only-binary=:all: \
    --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 \
    --python-version 3.11 --implementation cp --abi cp311
```
**Then audit every .whl tag** — anything `manylinux_2_28/_2_31/_2_34` ⇒ FAIL the build (forces a
downpin). Pure-python (`py3-none-any`) always OK.

**Pin anchors (versions known to still ship glibc-2.17 wheels; VERIFY on yellow zone day 1):**

| pkg | pin | note |
|---|---|---|
| numpy | **1.26.4** | definite `manylinux_2_17`; compatible with scipy 1.15 |
| scipy | **1.15.x** | x86_64 still `manylinux_2_17` *as of writing — VERIFY*; need ≥1.15 for `AAA` |
| matplotlib | **3.8.x / 3.9.x** | + transitive (pillow/kiwisolver/contourpy/fonttools/cycler/dateutil/pyparsing/packaging) all 2_17 |
| PyQt5 | **5.15.9 / 5.15.10** | |
| PyQt5-Qt5 | **5.15.2** | KEY: this version is `manylinux2014`; 5.15.11+ may need glibc 2.28 |
| PyQt5-sip | matching | |
| scikit-rf | *optional* | only `score.py`'s passivity gate uses it; the analytic-only GUI path does NOT need it (drops pandas etc.). Include only if you keep the passivity diagnostic. |

**Runtime:** PyQt5-Qt5 needs xcb/fontconfig/libGL system libs — the red box runs Virtuoso (a Qt GUI
app) so they're present. Smoke test with `QT_QPA_PLATFORM=offscreen` import first; if xcb plugin
fails, ship 1–2 `.so` or set `QT_QPA_PLATFORM_PLUGIN_PATH`.

---

## 5. Deployment pipeline (two modes)

**Red-zone layout (.venv and code decoupled — this is what makes incremental possible):**
```
/opt/ldo_modeler/
  .venv/            # built ONCE (full deploy), never rebuilt on incremental
  wheels/           # kept for venv rebuild
  app/              # repo source, refreshed each incremental
  results/          # user npz + outputs, persists, never overwritten
  bootstrap.sh      # full: venv + offline pip + smoke
  update.sh         # incremental: overwrite app/ only
  MANIFEST.json     # git SHA, versions, requirements-hash, mode, checksums
```

**Yellow-zone packaging (Windows, has net):**
- FULL: `git archive HEAD`→`app/`; cross-download wheels (§4) + **tag audit**; gen `requirements.lock`;
  bundle `app/ + wheels/ + bootstrap.sh + MANIFEST.json` → tar.gz + checksum.
- INCREMENTAL: `app/` only (or diff vs last-deployed SHA) + `update.sh` + MANIFEST. **No wheels.**
  GUARD: if `requirements.txt` hash ≠ last full build → refuse, demand a FULL deploy.

**Red-zone deploy (one-click):**
- `bootstrap.sh`: unzip → `python3.11 -m venv .venv` → `pip install --no-index --find-links=wheels/
  -r requirements.lock` (`--no-index` = the airgap flag) → smoke (offscreen import + `gui --selftest`)
  → write MANIFEST.
- `update.sh`: unzip → overwrite `app/` (keep `.venv/ wheels/ results/`) → verify req-hash unchanged
  vs deployed MANIFEST else abort. No pip, no venv.

---

## 6. Generalization (memory `tool-generalization-intent`)

The **model** is already frequency/disturbance-agnostic (continuous LTI 2-port; 8/16/24 MHz tones
were validation-only, never baked in). What was specialized = **hardcoded recipe defaults** → make
them a user profile (§3). Plus: **emit each model with a validity-envelope report** — fitted band,
OP/load range, linearity floor (dBc), whether ESL/2nd-resonance needed. Tool should DETECT + REPORT
limits, not assume: existing probes = linearity gate `spur_500u`, slew/dropout tanh clamp, passivity
gate. Honest limits: small-signal/OP bound (linear only ~50µA @121µA OP; mA swing → gm
compression/slew); nonlinear upconversion floor (LTI can't, SpectreRF 2f square-law).

---

## 7. Measurement / TB guidance — surface as inline GUI hints (Tab ② ②)

- **Sweep density:** Zout/PSRR `dec 40` 10 Hz→100 MHz; noise `dec 20`; densify (60–100/dec or local
  linear) only if the Zout peak / PSRR notch looks under-resolved (≳10 pts across the −3 dB width).
  Noise start freq BELOW the flicker corner (try 1 Hz). The accuracy knob that matters = a CLEAN DC
  OP (`errpreset=conservative`, tight `reltol`), not AC reltol.
- **Decap / loading:** load corner = ideal **DC current source** (sets OP, AC-open, doesn't pollute
  Zout); LDO's **intrinsic** output cap = INCLUDE; **external board/system decap + other loads on
  vout = EXCLUDE** (the system testbench already has them → double-count otherwise). `meta_cout/esr`
  = the LDO design values. (defect-6 boundary.)
- **`*_hf` arrays:** same TB as `z`/`p`, **nominal corner only, swept to 500 MHz**. Two uses:
  (a) bracket the RF carrier; (b) feed Cout/ESR auto-extraction (capacitive tail). One-shot option:
  sweep all corners to 500 MHz, nominal doubles as hf (fit_cout_esr falls back to `z_<nom>` if no hf).
- **Bandwidth for THIS real chip (5.8 GHz):** memory `modeling-bandwidth-three-frequencies`. 3-layer
  freq: LDO UGB (~MHz) / disturbance tone / supplied-circuit carrier (5.8 GHz). Modeling band ≠
  system max freq. 500 MHz was for Target A's 304 MHz carrier — NOT universal. **Run one exploratory
  nominal Zout sweep to 6–10 GHz**: smooth passive cap/ESR(/ESL) tail ⇒ lumped model extrapolates,
  500 M–1 G fine; structure/inductive rise ⇒ sweep past carrier AND add a series-ESL element (current
  `zmodel` flattens to ESR at HF, no L term).

---

## 8. Suggested build order (ultracode workflow phases)

1. **harness callable-ization** — factor `predict(P,f)`; make fit/import in-process; de-hardcode
   `121u` token; parameterize band/corners/vin. (zero numerical change; re-run the variant matrix to
   prove no regression.)
2. **GUI skeleton** — QTabWidget 4 tabs + matplotlib Qt5Agg canvas + QThread; wire import→fit→compare.
3. **Import guardrails + raw preview** (§2).
4. **Packaging pipeline** — yellow two-mode + wheel tag audit; red bootstrap/update + MANIFEST + req-hash guard.
5. **Offline dry-run** — rehearse the red install inside a `quay.io/pypa/manylinux2014_x86_64`
   container at home (kills risks §4 before the airgap).

## 9. Open items / verify

- scipy 1.15 x86_64 still `manylinux_2_17`? — verify on yellow zone day 1; if it moved to 2_28,
  downpin to the last 2_17 build that still has `AAA`.
- PyQt5 xcb plugin libs on the actual red box (Virtuoso implies present, but confirm).
- Does the 0.8 V LDO also take the 1.8 V supply? → 2nd PSRR path in `fit_model` (carried from the
  Cadence handoff; no-input change).
- 5.8 GHz chip: run the §7 exploratory 6–10 GHz sweep → set hf cutoff + decide if ESL element needed.

## 10. Memory pointers

`project-goal-ldo-rf-model`, `modeling-bandwidth-three-frequencies`, `tool-generalization-intent`,
`next-zout-psrr-phase`, `finding-spur-band-is-linear`, `reference-github-repo`,
`ldo-toolchain-ngspice-subprocess`. The Cadence-side bring-up: branch `target-b-cadence-bringup`
HANDOFF.md (2026-06-07c).
