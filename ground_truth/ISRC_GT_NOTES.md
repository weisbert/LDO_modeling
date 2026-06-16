# MOS current-source ground-truth library (`isrc_gt.lib`)

**Built 2026-06-16.** The OBJECT we model is MOS-transistor-level (real current
mirrors, run in SPICE); the DELIVERABLE model is behavioral — the same split the
LDO uses (`ldo_gt.cir` device-level GT → behavioral fit). This file is the
**current-source half of the GT object set**. The behavioral current model
(G1–G11) is fit against these in the next step.

## Why a *set* of ≥6 (not 3)
The real PMU has 3 current pins, but a behavioral fit against 3 operating points
overfits. So the GT is **8 deliberately diverse transistor-level archetypes**
spanning polarity / output-Z / compliance / Cp / current-noise / temperature law
/ PSRR sign. A fit that generalizes across all 8 is trustworthy; one that only
matches a narrow subset is caught. (Mirrors the LDO `variants.py` generalization
set.) Real-pin → archetype map is in `harness/isrc_variants.py::REAL_PIN_ARCHETYPE`.

## The 8 variants
| variant | topology | stresses |
|---|---|---|
| v1_nmos_simple  | simple NMOS mirror sink        | low-Z, early knee, ideal PSRR (IBP_POLY_1P8U archetype) |
| v2_nmos_cascode | cascode NMOS sink              | very high rout, higher compliance |
| v3_nmos_long    | L=4u simple NMOS sink          | high rout + low knee + low noise (IBP_POLY_500N archetype) |
| v4_pmos_simple  | simple PMOS source             | opposite polarity, knee near out→vdd |
| v5_pmos_cascode | cascode PMOS source            | high-rout sourcing branch |
| v6_ptat         | self-biased constant-gm (β-mult) | ~PTAT linear-in-T (IBP_PTAT_TUNE archetype) |
| v7_nmos_rbias   | resistor-to-vdd biased NMOS    | strong dIout/dVdd, poor +sign PSRR, CTAT-lean |
| v8_wilson       | Wilson mirror                  | feedback-boosted rout, distinct PSRR/noise |

## Validated terminal characterization (2026-06-16, ngspice-46)
```
variant          pol      Idc[uA]    knee[V]  rout[Mohm] Cp[fF] dId/dVdd[nS] In@1k[fA/rt] PTAT(I125/I-40)
v1_nmos_simple   sink       1.815  0.14-1.05        6.9    0.5        0.000       4933.8          1.009
v2_nmos_cascode  sink       1.000  0.10-1.05      240.9    0.9        0.000       3665.6          1.001
v3_nmos_long     sink       0.805  0.10-1.05       33.8    1.7        0.000        991.5          1.005
v4_pmos_simple   source     1.013  0.00-0.94       18.0    0.8       55.614       3378.3          1.003
v5_pmos_cascode  source     2.000  0.00-0.93      285.1    1.4        3.507       4704.2          1.000
v6_ptat          sink       1.518  0.08-1.05        9.0   34.0     -145.390      16191.8          1.679
v7_nmos_rbias    sink       1.338  0.13-1.05        8.3    0.7    -1801.930       4121.3          1.197
v8_wilson        sink       0.574  0.34-1.05     1150.9    0.4        0.000       2764.4          1.022
```
Diversity is real on every axis: Idc 0.57–2.0 µA · both polarities · **rout 6.9→1151 MΩ
(2.5 decades)** · sink-knee 0.08–0.34 V vs source-knee 0.93–0.94 V · **dIout/dVdd
0→1800 nS AND both signs** (v4/v5 +, v6/v7 −, v1/v2/v3/v8 ~0) · In 0.99–16 pA/√Hz ·
**temperature: v6 PTAT 1.679 ≈ ideal 1.708, rest flat (1.0)**. No single behavioral
operating point can fake all of these → anti-overfit.

## Behavioral model fit + cross-validation (2026-06-16)
The deliverable behavioral model (offline twin of the Cadence Verilog-A
`_current_block`) is:
```
I_pin(Vo,Vdd,T) = ( Idc(T) + g0*(Vo-vc) + gdd*(Vdd-Vdd0) ) * tanh( (knee_arg/Vk)^p )
   Idc(T)=idc55+didt*(T-55) [G1/G2] · g0=1/rout [G7/G8] · gdd signed [G4] · Cp [G7]
   knee_arg = Vo (sink) | Vdd0-Vo (source);  p = knee sharpness [G5]
   + In^2(f)=iw^2+kf/f current-noise params [G3]
```
`harness/fit_isrc.py` (anchored OP + 2-point gate, no optimizer fragility) →
`harness/emit_isrc.py` (ngspice B-source). **`harness/crossval_isrc.py` re-simulates
each model and compares to its GT — ONE template, ALL 8 archetypes (anti-overfit):**
```
variant          Idc err  IV rms  rout err  PSRR-sign  PTAT err  PASS
v1_nmos_simple     0.00%   3.75%     3.7%       ok       0.000    yes
v2_nmos_cascode    0.01%   0.64%     1.9%       ok       0.000    yes
v3_nmos_long       0.02%   1.25%     0.1%       ok       0.000    yes
v4_pmos_simple     0.02%   2.29%     0.2%       ok       0.000    yes
v5_pmos_cascode    0.00%   0.47%     6.6%       ok       0.000    yes
v6_ptat            0.36%   3.72%     0.1%       ok       0.001    yes
v7_nmos_rbias      0.07%   4.94%     3.0%       ok       0.000    yes
v8_wilson          0.17%   2.62%     0.0%       ok       0.000    yes        8/8
```
I-V fit R² 0.93–0.999, noise In²(f) fit R² ≥0.9999. Two convention bugs caught &
fixed by the diverse set: the sharp Wilson knee needed the sharpness exponent `p`
(a single-`tanh` knee fails it), and the sink PSRR needed a sign flip (probe reads
`i(vout)=-I_pin`) — both invisible if only v1 were modeled. **That is exactly what the
≥6-source anti-overfit requirement buys.**

NOT yet validated in-sim offline: current-noise re-simulation (ngspice B-source has
no flicker primitive) — noise is fit numerically (R²≥0.9999) and re-simulates on the
Cadence side via `white_noise`/`flicker_noise`; the PSRR frequency pole is fit but the
offline cross-val checks LF sign+magnitude only.

## Run it
```bash
# 1) characterize the MOS-GT objects -> work_isrc/<variant>.npz (modeling input)
python harness/isrc_char.py
# 2) fit the behavioral model + show params
python harness/fit_isrc.py
# 3) emit behavioral subckts + cross-validate model vs GT (8/8)
python harness/crossval_isrc.py
# fast regression guards
python -m pytest harness/test_isrc.py harness/test_fit_isrc.py -q
```

## ngspice note
EPEL el8 does not package ngspice; it was **built from source** (GitHub mirror,
`AC_PREREQ` lowered to 2.69, linked with `-lstdc++`) to `~/.local/bin/ngspice`
(v46), which is on PATH so `harness/ng.py` picks it up automatically.

## Next step
Carry the validated behavioral form into the Cadence Verilog-A emit
(`harness/emit_pmu_model.py::_current_block`) — same math (Idc(T)+I-V knee+g0+Cp+
signed PSRR pi(s)+noise), plus the G9 coupling / G6 corners / G11 report items per
`HANDOFF_PMU_CURRENT_MODEL.md`. The offline ngspice twin here is the reference the VA
emit must match.
