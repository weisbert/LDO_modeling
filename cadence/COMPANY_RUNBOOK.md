# Company runbook — modeling the real PMU 0.8 V LDO (Phase 4, in-situ)

The real PMU lives in the company Cadence environment and can't leave it, so **the
extraction runs there, by you.** This repo's tooling is built so only the *extraction*
layer touches Cadence; fitting is pure Python and runs anywhere.

```
 [Cadence/ADE on the PMU top]            [pure Python: numpy/scipy]        [Spectre]
   extract contract arrays    -- npz -->   fit_model -> .lib + .va   -->   score / PSS-HB drop-in
   (THIS is company-only)                  (runs anywhere)                 (company machine)
```

The npz file (`results/ref/<name>.npz`, schema in `CADENCE_EXTRACTION.md`) is the firewall:
if your ADE export produces those arrays, the rest is `fit_model.py` + `score`.

---

## 0. Setup on the company machine
1. `git clone` (or copy) this repo; checkout branch `target-b-cadence-bringup`.
2. Python deps: `pip install -r requirements.txt` (numpy, scipy≥1.15, matplotlib, scikit-rf).
3. **Adapt `cadence/env.sh`** to the company paths/versions: `CDSHOME`, `SPECTRE_HOME`,
   `CDS_LIC_FILE`. (Same idea, different absolute paths.) Same for the hardcoded paths in
   `cadence/spectre_run.py` (`CDSHOME`/`SPECTRE_HOME`/`LICENSE`) if you use the CLI path.
4. **`spectre -64`**: needed *only* to compile the emitted Verilog-A model. If your Spectre
   compiles VA fine without it, drop it; if VA compile dies on `gnu/stubs-32.h`, keep `-64`.
5. **The `spice_dut` BSIM3 `level=49` remap + `{}`-strip are for THIS repo's toy ngspice
   GT only.** Your PMU uses your PDK (already Spectre-native, instanced in your schematic) —
   you will NOT use `spice_dut`; your DUT is the PMU cellview itself.

---

## 1. Extraction recipe (in-situ, in YOUR ADE on the PMU top)
**Reconciliation "in-situ OP + pin-level decoupled extraction":** keep the LDO fully wired in
the PMU (bias / VREF / IBP / enables all real). Apply the contract stimuli at the LDO's OWN
pins. Idealize ONLY the supply pin's AC.

Let the LDO's pins be `<sup>` (1.05 V supply), `<sup2>` (1.8 V supply, if used), `<out>` (0.8 V).

**Step 1 — one DC operating point on the PMU top.** Record: DC level at `<sup>`/`<sup2>`, DC at
`<out>`, and the current the LDO sources into its load. Choose **3 load corners** bracketing it.

**Step 2 — per corner, these analyses on the PMU top** (all small-signal at the OP):

| contract array | how to drive it in ADE | read |
|---|---|---|
| `z_<il>` (Zout) | force `<sup>`(+`<sup2>`) with an ideal vsource = its OP level (= AC ground); inject **1 A AC into `<out>`**; `ac` 10 Hz–500 MHz | `Z = V(<out>)` (1 A ⇒ V=Z) |
| `p_<il>` (PSRR, 1.05 V) | **1 V AC on `<sup>`**, other supplies ideal-DC; `ac` | `H = V(<out>)/V(<sup>)` — store **COMPLEX, not dB** |
| `p2_<il>` (PSRR, 1.8 V) | **1 V AC on `<sup2>`**, others ideal-DC; `ac` | second PSRR path (multi-supply) |
| `noise_<il>` | `pnoise`/`noise`, out=`<out>`, supplies ideal (noiseless) | output noise PSD in **V/√Hz** — attribute = **sum the LDO INSTANCE's noise contribution only** (Spectre per-instance noise summary) = intrinsic LDO noise |
| `z_121u_hf`,`p_121u_hf` | same Zout/PSRR for the nominal corner, extend to **500 MHz** | bounds the RF carrier + drives Cout/ESR auto-extract |
| (optional) `dc_loadreg`,`dc_linereg`,`dc_dropout`,`trans_lin_<il>` | DC sweeps / load-step transient | regulation + transient |

Notes that cause silent errors (see `CADENCE_EXTRACTION.md`):
- **Boundary (defect-6):** decide whether the on-die output decap belongs to the LDO model
  (folded into `Zout`) or stays external in the system TB — don't double-count. Pass the design
  `Cout`/`ESR` to the fit via `--cout`/`--esr` (self-consistency check). "What else sits on the
  0.8 V net" sets this boundary.
- **Idealize only the supply pin's AC** (ideal DC source = AC ground at the OP rail level during
  Zout/noise runs) so PSRR is the LDO's, decoupled.
- Multi-supply ⇒ two PSRR arrays; the model gets two additive PSRR paths.

**Step 3 — export** each analysis to PSF (`psfascii`) or to CSV ("Export to CSV" in the results
browser).

> Tip: build/verify this in-situ TB **once** by hand in ADE, then OCEAN-sweep the 3 corners.
> A skillbridge driver can orchestrate it (ADE state → OCEAN → export); `cadence/skill_lib.py`
> shows the bridge idiom.

---

## 2. Convert export → npz  (`cadence/import_cadence.py`)
**CSV path (manual export — verified end-to-end here):** name the files per the layout in
`import_cadence.py`'s docstring (`z_<il>.csv`, `p_<il>.csv`, `noise_<il>.csv`, the two `*_hf`,
optional `dc_*`), each `f,Re,Im` (or `f,Sv`). Then:
```bash
python cadence/import_cadence.py csv  <csvdir>  --name mypmu --cout <Cout> --esr <ESR>
```
**PSF path:** `python cadence/import_cadence.py psf <psfdir> --name mypmu ...` — adjust the
analysis→file mapping in `import_cadence.from_psf` to match your ADE PSF naming.
Either way you get `results/ref/mypmu.npz`. (For the 2nd PSRR path, add the `p2_*` arrays and
extend the fit — ask and I'll wire the second-supply PSRR into `fit_model`.)

---

## 3. Fit + emit  (pure Python, no Cadence)
```bash
python harness/fit_model.py --variant mypmu
# -> model/ldo_mypmu.lib  (SPICE)  +  model/ldo_mypmu.va  (Verilog-A, PSRR-sign-correct)
```

---

## 4. Validate + drop-in (the payoff)
- **Score the fit** against the extracted reference:
  `python cadence/score_spectre.py --variant mypmu` (re-simulates the emitted `.va` in Spectre).
- **Drop-in acceptance (the real goal):** import `model/ldo_mypmu.va` as a veriloga cellview
  (`python cadence/skill_lib.py --lib <yourlib> --cell ldo_mypmu --va model/ldo_mypmu.va`),
  give it a **symbol matching the LDO's real pin interface**, swap it for the transistor LDO in
  the PMU top, and run your **PSS/HB**. Check: 304 MHz spur/sideband match + **speedup +
  convergence** vs the transistor PMU. `cadence/rf_accept.py` is the PSS-comparison template.

---

## What to expect (from the validation done on the test DUTs here)
- The model is a **linear small-signal supply element**: it reproduces linear PSRR/Zout/noise
  accurately (carrier PSRR matched the GT to ~0.5 dB at 304 MHz) and is all-passive ⇒ **PSS/HB
  converges robustly**.
- It is **LTI**: it will NOT reproduce the LDO's nonlinear supply-harmonic distortion (fine for
  small/moderate ripple spur transfer; not for large-signal distortion). See memory
  `rf-pss-acceptance-findings`.
- **Speedup shows up at system scale** — replacing a hundreds-of-FET LDO + its convergence-hard
  loop with the ~18-node behavioral model. Measure the real number with the drop-in in step 4.

## Carry-over gotchas (verified on this box)
- `spectre -64` for VA compile (if `gnu/stubs-32.h` error). The emitter's PSRR sign is fixed.
- PSRR is the **complex transfer** `V(out)/V(sup)` (harness takes −20·log₁₀|H|), not dB.
- Noise is **V/√Hz** (sqrt if the tool reports V²/Hz).
- Zout is driving-point **V/I at `<out>`** with the supply held ideal.
