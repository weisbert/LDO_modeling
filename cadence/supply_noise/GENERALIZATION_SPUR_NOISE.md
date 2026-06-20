# Supply-spur-noise generalization — difference report

Same spurry AVDD (floor + 8 spurs) injected onto the **independent transistor GT** and the **behavioral model**, per variant, in **ngspice** (fit-basis engine; supply-noise output = input·|PSRR|, so the GT-vs-model number is the PSRR-magnitude error) and **Cadence Spectre** (independent engine, REAL `noisefile` on the supply + a true `.noise` run, supply part quadrature-isolated). The agreement = how well the model GENERALIZES that architecture's supply-noise behavior. Sorted best→worst by Spectre worst-spur output error.

**Cross-engine consistency:** Spectre and ngspice agree to within **0.4 percentage-points** on every variant except `v10_3lc` (where a 30 dB PSRR miss makes the absolute output huge; both engines still flag it as a gross failure). That the real-`noisefile` Spectre run and the PSRR-derived ngspice number land on top of each other is itself a check that the methodology is sound.

| # | variant | tier | worst out (Spectre) | med (Spectre) | worst out (ngspice) | PSRR maxΔ dB (Spec/ng) | stressor |
|---|---|---|---|---|---|---|---|
| 1 | `v9_vldo` | excellent | 1.4% | 0.5% | 1.4% | 0.14 / 0.14 | very-low-dropout (~50mV): small-signal fits, large steps hit dro |
| 2 | `cg_hi` | excellent | 1.6% | 0.4% | 1.6% | 0.15 / 0.15 | higher gate cap -> lower-Q / damped resonance |
| 3 | `v4_ffpsrr` | excellent | 2.7% | 0.9% | 2.5% | 0.33 / 0.32 | feedforward onto fb node: non-minimum-phase PSRR (notch + RHP ze |
| 4 | `iq_lo` | excellent | 2.8% | 0.9% | 2.8% | 0.27 / 0.27 | low quiescent current (lower UGB/gm) |
| 5 | `v1_nmos` | excellent | 3.2% | 1.2% | 2.9% | 0.36 / 0.31 | NMOS-pass source-follower: low flat Zout, high PSRR, ~no peak (E |
| 6 | `v3_miller` | excellent | 4.7% | 2.7% | 4.5% | 0.42 / 0.40 | two-stage Miller + nulling-R: multi-pole Zout (resonance migrate |
| 7 | `base` | good | 5.3% | 2.7% | 5.4% | 0.65 / 0.67 | Target-A PMOS-pass / 5T-OTA reference |
| 8 | `v5_spur` | good | 5.3% | 2.7% | 5.4% | 0.65 / 0.67 | intrinsic spurs via 3 paths: ref 1.0MHz / charge-pump 2.5MHz / c |
| 9 | `v6_spur2` | good | 5.3% | 2.7% | 5.4% | 0.65 / 0.67 | two INCOMMENSURATE intrinsic tones (1.0MHz + 3.7MHz) -> multi-to |
| 10 | `base_ghz` | good | 5.3% | 2.7% | 5.4% | 0.65 / 0.67 | GHz-carrier demo: ldo_gt characterized to 10GHz (B-cover validit |
| 11 | `cout4n7` | good | 6.4% | 2.7% | 6.4% | 0.62 / 0.62 | mid output cap |
| 12 | `esr_hi` | good | 6.6% | 3.2% | 6.6% | 0.65 / 0.66 | high ESR -> ESR zero moves into band |
| 13 | `cout10n` | good | 7.0% | 3.3% | 7.0% | 0.67 / 0.67 | 10x output cap + higher ESR (Cout autoextract) |
| 14 | `v7_esl` | good | 7.1% | 1.1% | 7.2% | 1.37 / 1.36 | output-cap series ESL: Zout dips at SRF then RISES inductively ( |
| 15 | `iq_hi` | good | 8.6% | 4.7% | 8.6% | 0.83 / 0.84 | high quiescent current (higher UGB/gm) |
| 16 | `wp_big` | marginal | 11.4% | 2.8% | 11.4% | 1.11 / 1.14 | 2x pass-device width (gm, Zout LF) |
| 17 | `v2_capless` | marginal | 11.7% | 2.8% | 11.8% | 1.09 / 1.10 | cap-less small-Cout: resonance pushed to ~4-8MHz spur band (ESR- |
| 18 | `v8_dlc` | FAIL | 27.9% | 6.9% | 28.0% | 5.93 / 5.94 | double-LC pi output net: 2 resonances + an anti-resonance NOTCH  |
| 19 | `v10_3lc` | FAIL | 285.0% | 46.3% | 259.9% | 30.20 / 30.20 | multi-stage PDN (3-cap ladder): >2 Zout resonances -> exceeds th |

## Reading it

Of 19 variants: **6 excellent** (<5%), **9 good** (5–10%), **2 marginal** (10–15%), **2 fail** (>15%).

- **Generalizes well** (the bulk): the 2-branch-RLC Zout + PSRR coupling bank captures the architecture — PMOS/NMOS pass, Miller, feedforward-PSRR (non-min-phase notch), the OP/Cout/ESR/Iq sweeps, even the GHz-characterized and intrinsic-spur variants (their deterministic tones are invisible to `.noise`, so v5/v6 == base by construction).
- **Fails — and this IS the finding** (the model has no term for the structure, by design; do not patch the shared fit code to chase these):
    - `v8_dlc` (28%, PSRR maxΔ 5.9 dB): double-LC π net has an **anti-resonance notch** a single parallel-RLC branch cannot dip to.
    - `v10_3lc` (285%, PSRR maxΔ 30 dB): 3-cap-ladder PDN has **>2 Zout resonances** — well beyond the 2-branch RLC order; the model misses the HF resonant comb entirely.
- **Borderline** `v2_capless`/`wp_big` (~11–12%): a ~1 dB PSRR fit error near the capless resonance / pass-device-gm shift, worst at the HF spur on the steep PSRR slope.

Worst PSRR fit (Spectre): `v10_3lc` (30.2 dB), `v8_dlc` (5.9 dB), `v7_esl` (1.4 dB).
