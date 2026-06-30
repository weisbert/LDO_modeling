# Cadence auto-collection — brief for the VM-side agent

**Audience:** the Claude instance running on the user's PERSONAL VM, which has a real Cadence
environment (Virtuoso + Spectre). You are joining an ongoing project; this file is your context.
Written 2026-06-12 from the green-zone (Windows dev) session.

---

## 1. The project in one page

We build **fast behavioral LDO models** for RF system simulation (Cadence PSS/HB spur fidelity).
The model is a 3-pin subckt/Verilog-A (`vin/vout/gnd`, instance params `iload/slew_en/vdd/voutdc`)
composed of four swappable blocks, all fitted from characterization data:

- **Zout**: RLC branches (+ optional C_ft feedthrough cap for GHz tails)
- **PSRR**: VCCS G-bank (real poles + one signed complex-conjugate 2nd-order section)
- **Noise**: decoupled Norton @ vout (white + Lorentzians), or hybrid series form
- **Spurs**: deterministic vout tones (amp/phase)

Pipeline (all in `harness/`): reference data (`results/ref/<name>.npz`) → `fit_model.py
--variant <name>` → emitted `model/ldo_model.lib` + `.va` → `score.py` (composite metric +
crossval/passivity/structure-LOCO gates) → `report.py` (plain-text model-vs-GT diff).
A PyQt5 GUI (`gui/`) wraps the same calls: Import → Fit → Emit → Compare.

**Status:** method validated on 14 synthetic LDO architectures; the first REAL part (a 5.8GHz
capless LDO, company red zone, air-gapped, simulator = Empyrean ALPS) is modeled to composite
**1.81** — small-signal (Zout/PSRR/noise) closed. See `HANDOFF.md` for full history.

**Three environments:**
| zone | what | data path |
|---|---|---|
| green (this repo's dev box) | harness/fitter dev, synthetic GT via bundled ngspice | direct |
| red (company, air-gapped) | the real 5.8G part, ALPS simulator | text digest pasted across the gap |
| **VM (you)** | real Cadence/Spectre, user's own designs | **direct — no air gap. Repo cloned from github.com/weisbert/LDO_modeling** |

## 2. Your mission: automate the characterization exports

Today each LDO costs ~22–30 hand-exported simulations (the user: "每次自己仿真LDO数据建模还是
太麻烦了"). Your job is a **reproducible script** that, pointed at an LDO testbench, runs the
whole analysis matrix and writes files `cadence/import_cadence.py` ingests unattended — so
"characterize a new LDO" becomes one command.

**Deliverable shape (recommended):**
1. An **Ocean script** (`.ocn`) — or skillbridge/Python driving ADE, but plain Ocean ports best —
   parameterized by: design/testbench, the 3 load corners, nominal vin (vref), hf_stop, output dir.
2. It writes **one CSV per (quantity, corner)** in the layout `import_cadence.py` already parses
   (header row + numeric columns; comma or whitespace; see its module docstring lines 11–33).
3. A small manifest (JSON or naming convention) mapping `(kind, corner) → file` so
   `import_cadence.assemble(prof, files)` runs without human file-picking.
4. End-to-end check on the VM: assemble → fit → score → report (section 6).

Keep the **analysis-launching layer thin and swappable**: the same script must later port to the
red zone, whose simulator is Empyrean ALPS (ADE-compatible but NOT Spectre — analysis names and
ocnPrint quirks may differ).

## 3. The existing data contract (v1 — collect exactly this first)

Source of truth: **`CADENCE_EXTRACTION.md`** (array semantics, units, conventions) and
**`cadence/import_cadence.py`** (its executable mirror; CSV formats auto-detected from headers).
The file matrix per LDO (3 load corners bracketing the operating load, e.g. 20u/121u/250u):

| group | quantities | count |
|---|---|---|
| per corner ×3 | `z` (Zout AC), `p` (PSRR transfer), `noise`, `trans_lin`, `spurs_raw` (raw tran, auto-FFT'd) | 15 |
| nominal only | `z_hf`, `p_hf` (to hf_stop), `trans_big` (1mA step), `trans_slew` (5mA step) | 4 |
| global | `dc_loadreg`, `dc_linereg`, `dc_dropout` | 3 |
| optional | `spur_500u` linearity gate, `ibp` transimpedance | — |

Mind the silent-mismatch conventions (CADENCE_EXTRACTION.md §"Conventions"): PSRR = COMPLEX
transfer vout/vin (not dB), Zout = driving-point V/I with vin ideal, noise in V/√Hz, spurs
intrinsic-only. Grids: ≥10 pts/decade is safe (the digest sufficiency gate warns under 4).
`hf_stop` is a profile parameter (default 500MHz); for GHz parts run a 6–10GHz exploratory
sweep first and set the ceiling accordingly (ideal caps are smooth up there; real parts with
package L have structure — see memory `finding-systest-bcover`).

Transient step sizes (0.3·bias / 1mA / 5mA) are currently hardcoded harness-side (deferred
refactor R1) — make them script PARAMETERS now so the script doesn't inherit the hardcode.

## 4. New scope (v2 extensions — collect these too; harness support follows)

These are PROPOSED array names. The fitter/emitter does NOT consume them yet — green-zone work
will follow once first data exists. Collect into separate clearly-named CSVs; do not block on
harness support.

### 4a. Bias-current output pins (NEW requirement, 2026-06-12)
The real LDO's output rail also serves bias outputs (`IBP_POLY_500N`, `IBP_POLY_1P8U`,
`IPTAT_1P5U`) feeding **VCO/PLL** — so their noise/ripple becomes phase noise/spurs downstream.
Fidelity decision: **L0+L1+L2 all needed** (DC + supply coupling/output impedance + noise).
Per bias pin, at the NOMINAL load corner (bias outputs barely depend on LDO load), pin held at
its nominal DC operating voltage:

| proposed array | shape | analysis |
|---|---|---|
| `biasdc_<pin>` | `[T,2]` temp[°C], i[A] | dc temp sweep (captures PTAT slope) |
| `biasvdd_<pin>` | `[V,2]` vdd[V], i[A] | dc vdd sweep at nominal temp |
| `biasxfer_<pin>` | `[N,3]` f, Re, Im | ac: 1V on vin → i(pin)/V(vin) [A/V] supply→bias coupling |
| `biasz_<pin>` | `[N,3]` f, Re, Im | ac: driving-point impedance at the pin [Ω] |
| `biasnoise_<pin>` | `[M,2]` f, Si[A/√Hz] | noise, output = pin current |

`<pin>` = lowercase tag, e.g. `ibp500n`, `ibp1p8u`, `iptat1p5u`. Their µA-level DC draw on the
LDO output should simply be INCLUDED in the load-corner currents (the model is iload-parameterized
— no separate mechanism needed for the loading effect).

### 4b. PVT (route A: one model per PVT cell, .lib section selection)
Decision made green-side: NO cross-PVT interpolation (avoids a new overfitting surface); instead
characterize each PVT cell separately → fit separately → emit `.lib` sections (`tt_25c_1v05`,
`ss_125c_0v95`, …), Cadence corner setup picks the section. Implication for YOUR script: the
whole §3+§4a matrix loops over **process corner × temp × vdd** — this is exactly why automation
is a prerequisite. Layout: `export/<pvt_tag>/<files>`, one npz/profile per tag, profile `name`
carrying the tag. Start with the user's shortlist of cells (ask — don't enumerate the full cube).
Exception: `biasdc_*` is already a temp sweep — collect it once per process×vdd, not per temp.

## 5. Pending decision: trans-ID may shrink the AC matrix

A validated alternative ("trans-ID", `harness/trans_id.py` + GUI tab 5) recovers Zout+PSRR+DC
from **one interleaved-multitone transient per corner** — proven on synthetics, an experiment on
the real part is queued (HANDOFF.md top). If it lands GO, the script's AC portion shrinks to
the `*_hf` tails + noise + spurs, and the tran becomes the backbone. **Structure the script so
the AC block and a future tran block are interchangeable**; don't wait for the decision to start.

## 6. DUT strategy: the VM has NO LDO design (only the tsmc18rf PDK) — that's fine

Nobody hand-builds an LDO for this. The script's correctness is about UNITS/CONVENTIONS/FORMATS,
not about how good the DUT is. Three tiers, in order:

- **Tier 0 (primary): round-trip our own emitted `.va` — known ground truth.** Spectre simulates
  Verilog-A natively (`ahdl_include "ldo_v3_miller.va"`; repo `model/` has 14 of them, 3-pin,
  with built-in noise sources so even `pnoise` is exercised). The loop:
  `collect(va_model) → import_cadence.assemble → fit → score against the ORIGINAL ref npz in
  results/ref/` — the composite should land near that variant's known baseline (see
  `results/matrix*`). Any unit/format/convention bug in your script shows up as a huge composite.
  Zero PDK, zero schematic, fully quantitative. **Prove the script here first.**
- **Tier 1 (PDK realism): port one GT netlist to tsmc18rf yourself** — `ground_truth/ldo_gt.lib`
  etc. are transistor-level ngspice subckts with generic MOS; porting one to a Spectre `.scs`
  with tsmc18 devices (model names/sections from the PDK's model `.scs` files) is pure TEXT work
  + CLI iteration — agent time, not user weekend time. It only needs to REGULATE, not be good.
  This is the tier that tests what tier 0 can't: PDK corner `section=` statements (PVT loop!),
  real device flicker noise, temperature. Add a 2-mirror PTAT/bias branch to exercise §4a.
- **Tier 2 (loop mechanics): any trivial DUT.** The PVT × corner × file-matrix orchestration is
  DUT-agnostic — bring it up on an RC divider if tier 1 lags. Never block on the LDO.

## 7. Agent feedback loop in the Cadence environment

Ranked by iteration speed for an agent:

1. **`spectre` CLI on raw netlists (the tight loop — use this for bring-up).**
   `spectre tb.scs +escchars =log run.log -format psfascii -raw ./psf` — you write the netlist,
   run, parse `./psf` (PSF-ASCII; `import_cadence` already best-effort reads it, or use the
   `psf_utils` pip package), iterate. No GUI, no Maestro, no ADE license — simulator license only.
   **This mirrors the repo's own architecture** (`bench.py` generates decks, `ng.py` runs them
   via subprocess): the natural deliverable is a *spectre backend* beside `ng.py`, with the file
   matrix as backend-agnostic Python that EMITS decks — which is also exactly what makes the
   later ALPS (red-zone) port a one-layer swap.
2. **Headless OCEAN for the Maestro layer.** `ocean -nograph -replay run.ocn -log ocean.log`
   runs unattended; for an ADE Assembler/maestro view use OCEAN XL (`ocnxlBegin()` …
   `ocnxlRun()` … results API). The loop: agent edits `.ocn` → headless run → read log/results
   → iterate. Add this as a THIN adapter only after the matrix works via tier-0/CLI — it's what
   lets the user trigger collection from their interactive maestro setup later.
3. **skillbridge (live session, optional).** `pip install skillbridge`, load its SKILL server
   into a Virtuoso session **the user starts** (agents can't pop the GUI — known gotcha), then
   Python drives the live session. Best for poking at an existing maestro cellview; worst
   portability. Don't make the deliverable depend on it.

## 8. Validating your collection end-to-end (on the VM)

```bash
python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
# ngspice needed for scoring (model is simulated): apt/yum install ngspice, or set $NGSPICE
cd harness
python -c "import sys; sys.path.insert(0,'../cadence'); import import_cadence ..."  # assemble(prof, files)
python fit_model.py --variant <name>
python score.py --variant <name>          # composite + gates; --crossval for the full net
python report.py --variant <name>         # plain-text model-vs-GT diff
```
A healthy real-part fit looks like composite ~2–4 with no gate FAIL (the red-zone part scored
1.81). If Zout shows a huge ghost capacitance, suspect a broken `z_hf` export (this exact bug
happened once — the guardrail now catches it, but read the warnings).

## 9. Gotchas inherited from earlier rounds

- **GUI**: launch `./run_gui` from a desktop terminal yourself? No — the USER must launch it
  (agent processes can't pop X11/Qt windows reliably); prefer the CLI path above for automation.
- `LDO_WORK` env var gives each parallel process its own ngspice scratch dir.
- Compile `.va` files only on COPIES in a scratch dir — openvaf/lld drops an import-library
  `<name>.lib` beside its output and once clobbered the emitted SPICE model.
- Spurs: export the RAW vout transient (`spurs_raw`), no calculator FFT — import side does a
  coherent-window FFT with fundamental classification.
- Do NOT re-attempt a fitter fix for multi-pole PSRR grid sensitivity (R7 closed as negative);
  if a fit looks PSRR-limited, the fix is recipe-side (tone/grid placement), not the fitter.

## 10. Out of scope for the VM session

- Re-tuning fit thresholds (e.g. SHELF_PH_TRIG) or the composite weights — green-zone work with
  the full 19-variant regression matrix behind it.
- Harness consumption of the §4 arrays (bias-pin blocks, PVT section emitter) — collect first,
  green zone builds the fitter/emitter support against real data.
- The red-zone part itself — it never leaves the company box; your scripts get ported there later.
