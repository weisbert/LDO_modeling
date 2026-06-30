# LDO_modeling

A **fast, PSS/HB-robust behavioral model** for a low-dropout regulator (LDO), built to replace a
transistor-level LDO in Cadence Spectre periodic analyses while preserving RF spur / sideband
fidelity around a ~304 MHz carrier. The model is all-linear/passive **R/L/C + controlled sources**
(no `laplace_nd`), operating-point-parameterized by load current.

The LDO is treated as a 2-port supply element: its **Zout(f)** and **PSRR(f)** shape the spur
spectrum. The model is fit per-block (Zout, PSRR, decoupled Norton noise, discrete spurs) from a
ground-truth reference and emitted as both a SPICE `.lib` and a Verilog-A `.va`.

See **`CLAUDE.md`** (start here), **`STATUS.md`** (current status / next action), **`PROJECT.md`**
(overview), and **`docs/`** (`reference/` durable facts · `CONVENTIONS.md` the handoff system ·
`archive/` history) for the full story.

## Layout
| path | what |
|---|---|
| `harness/` | the toolchain: `bench.py` (DUT-generic stimuli), `gen_reference.py` (extract ground truth), `fit_model.py` (fit + emit `.lib`/`.va`), `score.py` (grade vs GT), `run_matrix.py` (multi-variant), `variants.py` (DUT registry), `analyze_*.py` (analysis helpers) |
| `ground_truth/` | transistor-level GT LDO netlists (the DUTs) |
| `models/` | device model cards (`nmos_lv.mod`, `pmos_lv.mod`) |
| `model/` | emitted behavioral models (`*.lib`, `*.va`, `*_dropout.tbl`) |
| `results/ref/` | extracted ground-truth references (`*.npz`, reusable across runs) |
| `results/generalization/` | `matrix.md` / `matrix.json` — the per-variant scorecard |
| `research/` | modeling-method + open-source-tooling surveys |

> Not in the repo (see `.gitignore`): the Python `.venv/`, the Windows ngspice binaries
> (`tools/`), and ngspice run-scratch dirs (`work*`). Install ngspice on your OS (below).

## Setup (Linux / macOS)
```bash
# 1. ngspice (the SPICE engine the harness drives as a subprocess)
sudo apt install ngspice          # Debian/Ubuntu   (or: brew install ngspice)

# 2. Python env
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```
`harness/ng.py` finds ngspice via `$NGSPICE` → bundled Windows exe → `ngspice` on `PATH`.
If your ngspice is elsewhere: `export NGSPICE=/path/to/ngspice`.

## Quick start
```bash
cd harness
# score the emitted model for a variant against its stored reference:
python score.py --variant base
# re-fit + emit a variant from its reference, then score:
python fit_model.py --variant v4_ffpsrr && python score.py --variant v4_ffpsrr
# full generalization matrix (reuses the stored references):
python run_matrix.py --reuse
# regenerate a reference from the GT netlist (needs ngspice):
python gen_reference.py --variant base
```
Lower composite score = better. The matrix lands in `results/generalization/matrix.md`.
