# Supply-noise (AVDD1P0) injection into the LDO model

Does the behavioral LDO model simulate the effect of **supply noise** (a noise file on
AVDD1P0) on its output? **Yes** — validated locally in Spectre 18.1 on the emitted
Verilog-A, output = input PSD shaped by the model's PSRR, agreeing with the model's own
measured supply→output transfer to **0.02 %** at every spur.

## The unit that bites you first
A Spectre `vsource noisefile=` value — and a Verilog-A `noise_table` value — is a **power
spectral density in V²/Hz** (current source: A²/Hz), **not** the square-rooted V/√Hz you
plot. The `.noise` analysis `out` trace comes back as amplitude density **V/√Hz**. So:

| where | quantity | unit |
|---|---|---|
| input: `noisefile` / `noise_table` | PSD | **V²/Hz** (A²/Hz for current) |
| output: `.noise` `out` / `onoise` | ASD | **V/√Hz** |

Measured definitively (`verify_noisefile_units.py`): a flat file value `1e-14` → node output
noise `1e-7` V/√Hz = √(1e-14). Locked by `cadence/test_noisefile_units_spectre.py`.

## The stimulus (alignment artifact)
`avdd_spectrum.py` → a realistic 1.0 V analog-supply spectrum: a broadband floor (50 nV/√Hz
white + 1/f corner 50 kHz) with **8 spurs** (DC-DC switcher fundamental + harmonics, a
control-loop tone, a reference-clock comb). Emits:
- `avdd_noise_spectrum.png` — the curve (V/√Hz vs freq, 8 spurs labelled);
- `avdd_noise_table.dat` — **freq, PSD[V²/Hz]** pairs, ready to feed `noisefile`/`noise_table`;
- `avdd_spurs.csv` — the 8 spur tones for deterministic (AC/PAC/transient) injection.

## The propagation result
`inject_supply_noise.py` fits `v2_capless`, emits the VA, then in Spectre:
1. AC drive the supply (mag=1) → `Hsup(f)` = supply→output transfer (PSRR = −20·log₁₀|Hsup|);
2. `.noise` with the full AVDD `noise_table` on the supply → total output noise;
3. `.noise` with a quiet supply → intrinsic model noise;
4. quadrature-isolate the supply part: √(out_with² − out_intrinsic²);
5. cross-check vs `input_ASD(f)·Hsup(f)` → **0.02 %** at all 8 spurs.

Physics the model reproduces: PSRR rolls off (40 dB @ 100 kHz → 16.8 dB @ 2 MHz → 3.5 dB
@ 6 MHz on this capless rail), so HF spurs survive better at the output even though the
input is largest at 2 MHz. Locked by `cadence/test_supply_noise_prop_spectre.py`.

> ⚠️ **This `inject_supply_noise.py` check is SELF-CONSISTENT, not independent.** It confirms
> the model's `.noise` output == input × the model's OWN measured PSRR — a tautology about
> Spectre (noise vs AC agree on one model), NOT evidence the model behaves like a real LDO.
> Its 0.02 % is a simulator-consistency number, not an accuracy number. The real validation is
> below.

## The NON-CIRCULAR validation — vs the transistor GT (`gt_vs_model_supply_noise.py`)
Inject the **same** spurry AVDD onto BOTH the **independent transistor-level** GT
(`ground_truth/ldo_v2_capless.lib`: PMOS pass + 5T-OTA + feedback, run in Spectre via
`spectre_bench.spice_dut`) and the behavioral model, in the same engine, and compare their
**output** noise. This is the honest number — bounded by PSRR fit quality, not self-consistency.

Result: the model reproduces the real LDO's supply-noise→output to **1–3 % typical, ~12 % worst**
(at the 8 MHz harmonic, which sits on the steep recovery after the ~7 MHz PSRR notch where PSRR
is only ~3 dB and hardest to fit — a ~1 dB PSRR error → ~12 % output error, fully explained). The
GT and model PSRR curves overlay to ~1 dB across the band, notch included. Locked (2-spur, fast)
by `cadence/test_supply_noise_gt_vs_model_spectre.py` (GT-vs-model output <10 %, PSRR <2 dB).

## Run
```
python3 cadence/supply_noise/avdd_spectrum.py             # draw the stimulus
python3 cadence/supply_noise/inject_supply_noise.py       # self-consistency (model vs itself)
python3 cadence/supply_noise/gt_vs_model_supply_noise.py  # NON-CIRCULAR: model vs transistor GT
python3 -m pytest cadence/test_noisefile_units_spectre.py \
    cadence/test_supply_noise_prop_spectre.py \
    cadence/test_supply_noise_gt_vs_model_spectre.py -q
```
All tests are spectre-gated (skip cleanly when Spectre is absent, env-overridable guard).

## ⚠️ Spur sampling rule (mandatory — or the spur "doesn't simulate")
A `.noise` (or AC) analysis evaluates ONLY at the frequencies you request, reading the supply
PSD by PWL-interpolating the noisefile there. A spur is sub-kHz wide (HWHM ≈ f0/(2Q): ~833 Hz
for a 2 MHz / Q1200 spur). A coarse log/dec sweep has MHz-band point spacing of many kHz, so it
**steps clean over the spur**, samples the floor, and the spur never appears in the result — it
looks like it "didn't simulate".

So every noise/AC sweep must include **the spur center f0 + very-short-step points around it**
(in HWHM units). `avdd_spectrum.spur_brackets(f0, q)` / `analysis_freqs()` build this (steps at
0.05 … 4 × HWHM, Q-aware so it auto-narrows for higher-Q spurs). Negative control
(`test_supply_noise_prop_spectre.py::test_coarse_sweep_misses_the_spur`): in one run the
bracketed f0 shows a **353 nV** output spur while coarse neighbours ≥10·HWHM away read **6.5 nV**
(floor) — a **54×** miss. Never hand a Cadence noise sweep a bare log grid when spurs matter.

## Note: noise vs deterministic spurs
A `.noise` analysis treats the supply spectrum as random noise; because the small-signal
model is LTI, output PSD = input PSD · |Hsup|² regardless, so the spurs are shaped correctly
for a PSD/output-noise view. To model a spur as a *coherent* tone (e.g. phase/intermod with
a carrier), inject it from `avdd_spurs.csv` via AC/PAC or transient+FFT — a heavier second
step, not needed for the supply-noise→output PSD answer.
