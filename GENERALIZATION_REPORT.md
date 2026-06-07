# LDO behavioral-model GENERALIZATION study (2026-06-06)

**Question (from the user):** does the Target-A modeling method generalize *smoothly*
to other LDO architectures, or where does it break and how is it extended?

**Verdict:** the method generalizes broadly. After fixing two harness bugs and adding
fitter upgrades (all validated, no regression on Target A), the LINEAR
`Zout(s) + PSRR(s) + shaped-noise + pwl-dropout`, OP-parameterized model covers a wide
family — different output caps (100 pF–10 nF), ESR (0.5–120 Ω), quiescent current, pass
sizing, peaking, **capless/high-UGB (resonance in the spur band)**, **NMOS source-follower
(flat low Zout)**, **two-stage Miller (multi-pole Zout)**, and — after adopting a
**per-block, data-selected** model architecture — **non-minimum-phase / feedforward PSRR**
(V4: composite 33→5.5), the **noise block** (decoupled Norton-@vout; V1/V3 npsd 9.6/21.8→1.0/2.1),
and the **discrete-spur block** (deterministic vout tones; V5/V6 reproduced to 0.00 dB).

**UPDATE (noise + spur blocks DONE — see §6/§7 below):** the two remaining items are closed.
The noise *shape* boundary is fixed by a decoupled Norton-@vout block (all npsd ≤ 3.6 dB,
zero regression). The user's reminder that *real noise has discrete spurs* is addressed by a
spur block (transient-FFT characterized, deterministic tones at vout, multi-tone HB manifest).
Residual is now only **Zout/PSRR fidelity on the hardest architectures** (V1 flat Zout zrms 1.94,
V3 migrating multi-pole resonance pkf 5.0, V4 PSRR phase 25°) — then Target B.

**Architecture decision (adopted):** rather than one fixed topology stretched to fit all,
or N monolithic per-architecture models, the model is a set of **swappable blocks**
(Zout / PSRR / noise / dropout) each with a **data-driven selector**. Real LDOs *combine*
traits, so per-block selection composes (4+2+2 blocks) where a monolithic library would
need the cross-product (4×2×2). The **PSRR block is the validated first instance** (below).

---

## Two things were tested (kept separate)
- **Expressiveness** — does the fixed model *topology* have the DOF to represent another
  architecture's {Zout, PSRR, noise, transient, dropout}(f, iload)? (the scientific question)
- **Automation** — does the *fitter code* fit a new LDO from terminal data with no hand-tuning?

Several apparent "breakers" turned out to be **automation** bugs (a hardcoded cap, a
non-robust fit), not expressiveness limits. Keeping the two apart is the main lesson.

## Variant family (12 DUTs, all on the same nlv/plv BSIM3 cards)
A-layer = same `ldo_gt` topology, swept operating regime (tests OP-param + auto-Cout).
B-layer = new transistor-level architectures (the real generalization test), each built +
stability-vetted (`harness/bringup.py`): converges, ring-decay < 1, sane Vout.

| id | class | what it stresses |
|----|-------|------------------|
| base | PMOS-pass / 5T-OTA | Target-A reference |
| cout10n / cout4n7 | 10×/4.7× output cap (+ESR) | Cout auto-extraction |
| esr_hi | ESR 0.5→3 Ω | ESR zero in band |
| iq_lo / iq_hi | quiescent current 4µ/20µ | UGB/gm vs load |
| wp_big | 2× pass width | gm, Zout LF |
| cg_hi | larger gate cap | low phase margin / high-Q peak |
| **v1_nmos** | **NMOS source-follower** | flat low Zout ~1/gm, no resonance |
| **v2_capless** | **cap-less, 100 pF, UGB→band** | resonance at 3.8–7.9 MHz (in 8 MHz spur band) |
| **v3_miller** | **2-stage Miller + nulling-R** | multi-pole Zout (resonance migrates 0.27→10 MHz with load) |
| **v4_ffpsrr** | **supply feedforward onto fb** | non-minimum-phase PSRR (deep notch, ~390° phase race) |

## Results (composite, lower = better; before → after the fitter upgrades)

| variant | composite | worst Zrms | worst Zband | worst pphase° | worst npsd(dB) | Cout fit/true (pF) | spur16 (dBc) |
|---|---|---|---|---|---|---|---|
| base | **3.7** | 0.41 | 0.06 | 1 | 1.2 | 997 / 1000 | −155 |
| cout10n | 3.0 | 0.10 | 0.13 | 0 | 2.6 | 9704 / 10000 | −145 |
| cout4n7 | 2.4 | 0.08 | 0.13 | 0 | 0.9 | 4622 / 4700 | −158 |
| esr_hi | 3.8 | 0.28 | 0.26 | 1 | 1.0 | 972 / 1000 | −155 |
| iq_lo | 5.4 | 0.10 | 0.04 | 0 | 2.3 | 998 / 1000 | −155 |
| iq_hi | 4.2 | 0.58 | 0.08 | 2 | 1.9 | 996 / 1000 | −155 |
| wp_big | 7.7 | 0.72 | 0.08 | 2 | 1.3 | 997 / 1000 | −158 |
| cg_hi | 6.4 | 0.04 | 0.03 | 0 | 2.4 | 998 / 1000 | −156 |
| **v1_nmos** | **13.5** (was 439) | 1.94 | 1.47 | 5 | **9.6** | 381 / 1000 | −163 |
| **v2_capless** | **9.9** (was 164) | 1.20 | 0.95 | 3 | **8.4** | 122 / 100 | −175 |
| **v3_miller** | **18.2** (was 116) | 1.14 | 0.78 | 10 | **21.8** | 998 / 1000 | −150 |
| **v4_ffpsrr** | **5.5** (was 33) | 0.13 | 0.19 | 25 | 3.9 | 972 / 1000 | −158 |

(v4 closed by the PSRR block — pband 6.7→0.04 dB, pphase 121°→25°; residual is now noise/transient.)

(Full per-corner scorecards + GT-vs-model overlays: `results/generalization/`.)

## What we learned

### 1. Two harness bugs masqueraded as generalization failures (now fixed)
- **`emit()` hardcoded `Cout=1n / ESR=0.5`.** Phase 0 un-hardcoded the *fit* but not the
  *emitter*, so cout4n7/cout10n/V2 fit perfectly yet emitted a 1 nF model (resonance at the
  wrong frequency → composite 104/ERR/164). Fixed: emit the auto-extracted Cout/ESR.
- **`fit_zout` diverged on flat / no-resonance Zout** (unbounded LM + `argmax`-of-flat init):
  cout10n@20µ → R_a = 32 MΩ, Vreg = 654 kV → the model timed out; V1 got a 92 dB spurious
  peak. Fixed: bounded TRF + peak-significance gate + multi-start.

### 2. Fitter upgrades that made the method generalize (all validated, base 3.8→3.7)
- **Cout/ESR auto-extracted from data** — from the capacitive band (`∠Z < −45°`,
  `C = −1/(ω·Im Z)`) so it stays correct even when a large ESR floors the HF tail.
  Recovers true cap within a few % across 100 pF–10 nF (V1's 30 Ω-ESR case is the one
  exception, 381 vs 1000 pF — see below).
- **Robust multi-start bounded `fit_zout`** — handles flat/overdamped Zout (source-follower)
  and resonances anywhere in band (capless 7 MHz, Miller migrating to 10 MHz).
- **`R_pl` damping resistor across L_a** — `R_pl→∞` is the classic resonant peak (base,
  unchanged); finite `R_pl` is the resistive *plateau* of a high-gain LDO. One DOF generalizes
  peaked ↔ plateau Zout.
- **Optional 2nd parallel R-L branch (branch B)** — engaged per-variant only when it beats
  1-branch by >40%. Closes the multi-pole **V3 Zout**: spur-band error 3.45 → 0.78 dB.

  Result: **Zout now generalizes across every variant** (worst Zrms ≤ ~1.9 dB, worst Zband
  ≤ ~1.5 dB) — flat (V1), in-band-resonance (V2), and multi-pole (V3) all fit.

### 3. The foundational "spur band is linear" finding GENERALIZES
`spur16 ≤ −145 dBc` on **every** variant — including **V2**, where the resonance/UGB was
deliberately pushed *into* the 8 MHz spur band. Small-signal supply/load disturbances still
convert ~linearly, so an LTI `Zout + PSRR` model is the right architecture across LDO classes
(true nonlinearity is dropout/slew, carried by `slew_en=1`). This was the project's biggest
risk and it held.

### 4. The PSRR block — non-minimum-phase PSRR CLOSED (per-block, validated)
V4 was the headline boundary: Zout fit perfectly (0.13 dB) but PSRR phase was 121° off
because a feedforward / loop-borne supply path makes the PSRR *higher-order*. The earlier
conclusion "PSRR doesn't factor as `i_c·Zout`" was **wrong — it was a fit-method failure**,
not a structural one: a shelf / 1-signed-section `i_c` simply isn't enough order, and naive
fits don't converge on a 60 dB-dynamic-range response. The fix:
- **Identify `i_c = H_psrr/Zout` by Sanathanan–Koerner** rational fitting (freq-scaled,
  relative-weighted). For V4 this is a **3rd-order, all-real-pole** rational — reconstruction
  of H to **0.04–0.06 dB / 0.4°** at every corner. (So no inductors / complex sections needed.)
- **Realize it as a bank of signed first-order real-pole sections**
  `i_c = G0 + Σ Gi/(1+s/wi)` (RC low-pass + VCCS each) — the existing min-phase shelf is just
  the 1-section case, so this *unifies* the PSRR path; HB-robust, no laplace_nd.
- **Data-driven selector:** if the 1-section shelf already fits (min-phase), keep it (base /
  all A-layer / V1 / V2 / V3 — no regression); else engage the SK-identified bank.

  Result: **V4 composite 20.2 → 5.5**, PSRR band **6.7 → 0.04 dB**, phase **121° → 25°**,
  with **zero regression** on the 9 min-phase variants (they stay on the shelf). This is the
  first realized instance of the per-block-selection architecture.

### 5. Remaining residual boundary — now **Zout/PSRR fidelity on the hardest architectures**
(The loop-noise-shape boundary below is CLOSED — see §6. The discrete-spur requirement is
addressed — see §7.) What is left after the noise+spur blocks:
- **V1 NMOS source-follower:** flat Zout still fits only to zrms 1.94 / zband 1.47 (the
  source-follower's 1/gm-dominated low-Z shape + 30 Ω-ESR Cout tail). PSRR band 0.95.
- **V3 two-stage Miller:** Zout resonance *migrates* with load (pkf 5.0 at 121µ) and PSRR
  phase residual (pband 1.78, pphase 10) — the multi-pole shape is only partially captured by
  the 2-branch RLC + shelf/SK PSRR. Composite 9.0 is now dominated by these, not noise.
- **V4:** PSRR phase residual 25° (SK bank reproduces magnitude to 0.04 dB; phase is the gap).
- **Minor:** V1's Cout auto-extract reads 381 vs 1000 pF because its 30 Ω ESR dominates the
  HF tail; the flat Zout still fits, so impact is small.

### 6. Noise block — decoupled Norton-@vout (CLOSED, validated)
The old realization (series voltage in branch A riding the Cout divider) was base-specific and
**coupled to the Zout synthesis** (adding branch B for V3's Zout perturbed its noise 5.6→21.8 dB).
**Fix:** output PSD = `In(f)·|Zout|`, so the Norton target is `In = Sv_meas/|Zout_model|`; fit
`In² = white + 6 Lorentzians` in the **log domain, jointly over the 3 load corners** with
**shared** corner freqs + per-corner amplitudes. Realized as a white-R floor + 6 fixed `R‖C`
sections, each transconducted into vout by a VCCS (gm sets amplitude, interpolated) — all
passive + VCCS, HB/PSS-robust, decoupled from the Zout branches. **Result: npsd v1 9.6→1.0,
v2 8.4→2.1, v3 21.8→2.1, v4 3.9→3.6, base 1.2→1.1 — all ≤ 3.6 dB, zero regression.** Design
evidence: `harness/probe_noise.py` (white+6 Lorentzians reaches <2.1 dB on every variant).
**Two bugs found+fixed:** (a) ngspice `.param` names are **case-insensitive** → noise g1/g2/g3
silently overwrote PSRR G1/G2/G3 (PSRR became a 35× gain) → renamed gnw/gn1..gn6; (b) the
`exp(quad-in-ln(iload))` interpolation **overshot +15.7 dB in the spur band at off-corner
loads** → `_pexpr` now CLAMPS every log-space param to its corner envelope [min/1.5, max·1.5]
(exact at corners, bounded between) + the shared noise corners are sorted/separated.

### 7. Discrete-spur block — deterministic vout tones (CLOSED, validated)
Addresses the user's reminder that **real LDO noise has discrete spurs** a smooth PSD cannot
reproduce. **Intrinsic** spurs (bandgap/ref, charge-pump, clock) are emitted as deterministic
SIN **current tones injected at vout** (`Isp 0 vout` — into vout), with `I_k = vout_amp_k /
|Zout_model(f_k)|` (same Norton decoupling; reuses the fitted Zout). f & phase are
load-independent; amplitude is interpolated through the clamped path. **External** supply/bias
spurs are NOT emitted — they ride the existing validated PSRR path (the model documents the
recommended vin-injection amplitude). Characterization is **transient-FFT** (`harness/spur_char.py`),
NOT `.noise`; detected lines are split into **fundamentals vs IM/harmonic products** (the linear
model emits only fundamentals — the user's nonlinearity regenerates IM). A **PSS/HB fundamental
manifest** is emitted (netlist comment + `{name}_spurs.json`): GCD-fold commensurate tones
(V5: base 0.5 MHz, maxharm 8) vs declare separate funds (V6: incommensurate 1.0 + 3.703 MHz).
Selector: no detected spurs → empty block → byte-identical no-spur models. **GT aggressors:**
`ldo_v5_spur` (ref 1.0 / charge-pump 2.5 / clock 4.0 MHz via 3 internal paths) and
`ldo_v6_spur2` (incommensurate). **Result: model reproduces every fundamental to amp 0.00 dB,
phase ~1e-5 rad, 0 missed / 0 false; small-signal blocks byte-identical to base.**

## Coverage map (the answer)
| LDO class | status | needs |
|---|---|---|
| output-cap value / ESR / Iq / sizing / Q (A-layer) | **covered as-is** | OP-param + auto-Cout |
| NMOS source-follower (flat Zout) | **covered** | robust fit + R_pl |
| cap-less / high-UGB (resonance in spur band) | **covered** | auto-Cout + robust fit |
| 2-stage Miller / multi-pole Zout | **covered (Zout)** | + 2nd R-L branch |
| non-minimum-phase / feedforward PSRR | **covered** | PSRR block (SK-fit signed-section bank, auto-selected) |
| accurate *noise* on the above new architectures | **covered** | noise block: decoupled Norton-@vout (white+6 Lorentzians), §6 |
| discrete output **spurs** (bandgap/charge-pump/clock) | **covered** | spur block: deterministic vout tones, transient-FFT characterized, §7 |
| V1 flat Zout / V3 migrating multi-pole / V4 PSRR phase | **residual** | tighter Zout/PSRR synthesis on the hardest architectures |

## Deliverables / reproduce
- `harness/variants.py` — variant registry. `harness/run_matrix.py [--reuse] [variants...]`
  runs gen_reference→fit→score for each and writes `results/generalization/{matrix.md,
  matrix.json, *.log, *_overlay_*.png}`.
- `harness/fit_model.py` — UPGRADED fitter (auto-Cout, robust multi-start Zout with R_pl +
  optional 2nd R-L branch, signed-2nd-section PSRR). Emits SPICE + Verilog-A.
- `harness/bringup.py` — DUT-generic stability/character diagnostic (Q, ring-decay,
  non-min-phase score).
- `ground_truth/ldo_v{1,2,3,4}_*.lib` — four new transistor-level GT LDOs;
  `ldo_v5_spur.lib` / `ldo_v6_spur2.lib` — spur-block validation DUTs.
- `harness/probe_noise.py` (noise-fit design evidence), `harness/spur_char.py` (transient-FFT
  spur characterizer + fundamental classifier + scorer).
- Per-variant models in `model/ldo_<variant>.lib` (+ `.va`, `_dropout.tbl`, `_spurs.json`).

## Recommended next steps
1. ~~Independent PSRR transfer~~ **DONE** — PSRR block closes V4 (33→5.5), no regression.
2. ~~Noise block (decoupled)~~ **DONE** (§6) — decoupled Norton-@vout, all npsd ≤ 3.6 dB.
3. ~~Discrete spurs~~ **DONE** (§7) — deterministic vout tones, reproduced to 0.00 dB; multi-tone
   HB manifest (commensurate vs incommensurate). External supply spurs ride the PSRR port.
4. **Tighten Zout/PSRR on the residual architectures** (the new dominant error): V1 flat
   source-follower Zout (zrms 1.94), V3 migrating multi-pole resonance (pkf 5.0, pband 1.78),
   V4 PSRR phase (25°). Candidate: load-dependent branch-B placement / higher-order PSRR phase.
5. **Target B** (real Cadence LDO) is unblocked: harness is DUT-generic, the fitter auto-discovers
   Cout / Zout order / PSRR order / noise shape / spur tones, so a characterized real LDO drops in.
   Spur tones import from a measured spectrum or Spectre PSS `.fi` sweep (same `(f,amp,phase)` schema).
