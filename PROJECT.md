# LDO behavioral-model builder — project brief

Fast behavioral model to replace a transistor-level LDO in **Cadence Spectre
PSS/HB**, where the LDO destroys sim speed. Real use: **RF spur / sideband
fidelity** around a ~304 MHz carrier, Vin = 1.05 V, load ~121 µA. The LDO is a
*supply element* whose output impedance + PSRR shape the spur spectrum.

The thing that was missing before: a **feedback loop** that quantitatively
scores a candidate model against a trustworthy reference. This repo builds that
loop using a **local transistor-level ground-truth LDO** (we know its true
behavior) + a scoring harness. (Target A: nail the methodology first; sky130
generalization comes later.)

## Decisive finding (drives the whole model architecture)
Injecting a **pure 8 MHz load tone** (up to an extreme 500 µA, load swinging
121±500 µA) into the GT and FFT-ing the output: the fundamental scales
**perfectly linearly** (Zout(8M)=20.9 Ω, constant) and harmonics are tiny —
16 MHz at **−62 dBc**, 24 MHz at −94 dBc, even at that extreme drive.
Reason: 8/16/24 MHz sit **above** the 1.78 MHz loop UGB, where the loop is open
and the **linear** output cap (1 nF) dominates.

**Implication:** the user's 16/24 MHz spurs are NOT manufactured by LDO
nonlinearity from an 8 MHz tone — they are in the load/supply excitation and the
LDO converts them ~linearly via Zout/PSRR. So an **accurate, passive,
operating-point-aware LINEAR multiport model (Zout + PSRR)** is sufficient and
is the target architecture. The previous failure was not "LTI is wrong" but
"LTI was badly fit + missing PSRR + laplace_nd numerics" (defects 2/3/4/5).
Nonlinearity (defect 1) is a <−62 dBc second-order effect here.

Caveat: holds for disturbances ABOVE the loop UGB. Large fast glitches with
energy WITHIN the loop band (<2 MHz) engage loop slew → nonlinear; revisit only
if that use case appears. −62 dBc is the worst-case nonlinear floor — judge
against the actual spur spec.

## Transient fidelity (req#1) — governed by Zout(s), with a measured linearity edge
Load-step recovery RINGS at the Zout resonance (measured 1.85 MHz ≈ Zpk 1.778 MHz):
the transient is the inverse transform of Zout(s)·i(s), so matching Zout(s)+PSRR(s)
in **magnitude AND phase** reproduces the linear transient waveform for free — no
separate transient fit. Verified linearity edge (vs the GT's own Zout-LTI prediction,
fast 1 ns edge @121µA OP): **<1 % to ~10 µA, <5 % to ~50 µA, ~18 % off by 1 mA
(gm COMPRESSION, edge-rate-independent), dropout/slew at 5 mA** (Vout collapses).
For the RF-spur use case (121 µA, sub-µA/µA tones above UGB) the model is transient-
exact and linear. Large mA-class fast steps need an optional `tanh` slew/dropout
clamp (ship disabled via `slew_en=0`). NOTE: the scorer must compare PHASE (passive
RLC is minimum-phase so a dense |Zout| fit largely recovers it, but the PSRR
controlled-source path can be non-minimum-phase) — a transient-overlay metric is
stronger and is now in score.py.

## Noise model (req#2) — output-referred shaped Norton + PSRR path
GT output noise is LOOP-SHAPED, NOT white-through-Zout (measured In=Sv/|Z| is non-flat):
white floor ~115 nV/√Hz (loop noise-gain, load-independent) + resonance peak at UGB
(~4.4× @121µ) + Cout roll-off; flicker added to device cards (Kf=4e-29, 1/f corner
~9 kHz) so the reference is realistic. OP-dependent (integrated 207/412/436 µVrms
@20/121/250µA). Recommended realization (adversarially verified, 0.00 dB vs GT incl.
external-decap scaling): inject a **Norton current noise at vout** with PSD
Sin(f)=Sv_target(f)·|Yout(f)|² (Verilog-A `noise_table`/`white_noise`+`flicker_noise`,
ngspice `trnoise`/`.noise`-visible source) — NO laplace_nd; valid in pnoise+hbnoise.
Keep TWO paths: (a) intrinsic self-noise = Norton @vout fit to GT `.noise`; (b)
external supply (vdd) noise rides the existing **PSRR** path. IBP/bias-device noise
folds into (a) once devices have Kf; an optional `ibp` port can carry externally-swept
bias noise via the characterized IBP→Vout transfer (ibp_xfer_* in the reference).
This GENERALIZES the legacy "vdc + worst-case-PVT noisefile" trick (that = our model
with Zout→0, PSRR→∞), recovering the Zout-driven spur path and PSRR coupling it threw
away while staying just as convergence-friendly (linear/passive, no transistor loop).

## Ground truth (what the model must match)
`ground_truth/ldo_gt.lib` — PMOS-pass LDO + 5T NMOS-input OTA + R divider +
internal ideal Vref, on low-voltage BSIM3 cards (`models/*.mod`, Vth0≈±0.30,
Tox=3.5n for 1.05 V, now with `noimod=1 kf=4e-29` 1/f flicker ⇒ ~9 kHz output-noise
corner). On-chip Cout(=1 nF)+ESR is INSIDE the subckt; any external decap belongs in
the testbench (defect-6 port boundary). Nominal peak placed at ~1.78 MHz via cout=1n,
cg=1p. DC: Vout≈898 mV @121µA, load-reg 30 mV/mA, line-reg 11 mV/V.

Operating-point dependence is STRONG (so the model MUST be load-parameterized):

| Iload | Zout LF | Zout peak | PSRR LF | PSRR worst |
|-------|---------|-----------|---------|------------|
| 20 µA | 85.2 Ω | 379 Ω @ 0.944 MHz | 51.1 dB | 16.1 dB |
| 121 µA| 23.3 Ω | 388 Ω @ 1.778 MHz | 44.3 dB | 3.7 dB |
| 250 µA| 18.3 Ω | 342 Ω @ 2.113 MHz | 36.7 dB | 1.1 dB |

## Repo layout
```
.venv/                 py3.11: numpy scipy matplotlib (PySpice unused)
tools/Spice64/         ngspice 46 (called via subprocess, -b batch + wrdata)
models/{nmos,pmos}_lv.mod
ground_truth/ldo_gt.lib    GROUND TRUTH (fixed reference circuit)
model/ldo_model.lib        CANDIDATE model (currently a deliberately-bad stub)
harness/ng.py              ngspice driver + wrdata parser
harness/bench.py           measure_zout / measure_psrr / measure_spur (DUT-generic)
harness/gen_reference.py   GT -> results/ref/gt_ref.npz  (the target dataset)
harness/score.py           THE FEEDBACK LOOP: grade a model vs reference
harness/recon.py           scratch diagnostics (linearity sweep, DC reg, noise)
results/ref/gt_ref.npz     reference (23 arrays, see below)
results/score/overlay_*.png  GT-vs-model 2x2 overlays (Zout/PSRR/noise/transient) per load
```
Reference keys: `z_{il}`,`p_{il}` (Zout/PSRR complex @3 loads), `z_121u_hf`/`p_121u_hf`
(to 500 MHz), `noise_{il}` (Sv PSD), `trans_lin_{il}` + `trans_big_121u`/`trans_slew_121u`
(load steps), `dc_loadreg`/`dc_linereg`, `ibp_xfer_{il}` (bias→Vout), `spur_500u` (sanity).
Scorer reports per-load Zout/PSRR mag+PHASE, transient droop/ring/waveform-RMS, noise
PSD log-RMS/peak/integrated-rms, a spur PASS/FAIL sanity gate, and a weighted composite.
**Stub baseline composite ≈ 403** (dominated by missing PSRR + missing noise + no peak).

## Fitted model — DONE (composite 3.8)
`harness/fit_model.py` fits per-corner {R_a, L_a, Rpass, g_lf, wz, Vn, Vreg} to the
reference (scipy) and EMITS `model/ldo_model.lib` (SPICE) + `model/ldo_model.va`
(Verilog-A) + `model/ldo_dropout.tbl`. Params interpolated as quadratics in ln(iload);
`score.py` passes `iload={corner}`. Achieved vs GT (slew_en=0): Zout band 0.02-0.04 dB,
peak <1 dB at exact freq, phase <1°; PSRR band 0.1 dB; transient-lin droop <0.3% with
correct ring; noise PSD log-RMS ~1 dB, peak -0.7 dB, integrated -3%. **All small-signal
+ noise acceptance targets met.**

Large-signal transient (slew_en=1, nonlinear branch = exact DC-curve via `pwl`/`$table_model`
with La providing di/dt slew): **DC dropout exact** (54 vs 54 mV @4 mA collapse), **5 mA
dynamic slew/dropout exact** (2354 vs 2354 mV, wrms 1%), 1 mA step within wrms 13% (initial
droop spike +18%, residual dynamic gm-expansion — settled+ring match). slew_en defaults 0
so PSS/HB spur runs stay linear & convergent; enable for large load-step transients.
Verilog-A uses native white_noise+flicker_noise (exact 1/f, no RC ladder), valid in
pnoise/hbnoise. NOTE: Verilog-A is a faithful translation of the ngspice-validated topology
but is NOT locally testable (needs Spectre/OpenVAF compile); $table_model control string may
be Spectre-version specific.

## The model contract (for the fitting phase)
Write `model/ldo_model.lib` defining `.subckt ldo_model vin vout` — **same 2-port
interface as ldo_gt** (global 0 = gnd). It must:
1. Hold the regulated DC Vout (~0.9 V) with correct load regulation.
2. Reproduce **Zout(f)** across loads {20µ,121µ,250µ}: LF floor, resonance
   peak (f & Q), and Cout/ESR roll-off. Realize as a **passive RLC network**
   (synthesized) — NOT `laplace_nd` — so it is PSS/HB-robust.
3. Add a **PSRR path** (Vin→Vout transfer): ~44 dB floor + notch at the peak.
   Use a controlled source / passive coupling.
4. Be **operating-point parameterized** (interpolate element values vs Iload, or
   provide load corners). Cout stays INSIDE = on-chip; external decap external.

Deliverables (both): the **SPICE subckt** above (scored locally) AND an
equivalent **Verilog-A** (same passive RLC + controlled sources, no laplace_nd)
for Cadence.

## How to run the feedback loop
```
.venv/Scripts/python.exe harness/gen_reference.py     # once (re-run if GT changes)
.venv/Scripts/python.exe harness/score.py             # grade model/ldo_model.lib
.venv/Scripts/python.exe harness/score.py --lib <f> --subckt <name>
```
`score.py` prints a per-load scorecard (Zout max/rms/band err, peak Δf/ΔdB,
PSRR band/max err), a spur fingerprint, a composite score (lower=better), and
writes overlay plots. The fitting loop = edit model → score → read breakdown →
repeat. Stub baseline composite ≈ 196 (dominated by missing PSRR).

## Suggested acceptance targets
- Zout @ 8/16/24 MHz band: |err| < 0.5 dB  (the critical spur band)
- Zout peak: f within ±5 %, magnitude within ±2 dB
- Zout broadband RMS < 2 dB
- PSRR @ band: |err| < 2 dB; floor within ±2 dB; notch freq within ±20 %
