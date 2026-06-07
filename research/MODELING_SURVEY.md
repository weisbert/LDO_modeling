# LDO / Supply-Element Modeling — Methods Survey & Gap Analysis
*(compiled 2026-06-06; raw multi-agent findings in `research/modeling_survey_raw.json`)*

Scope: how the field models an LDO **as a supply element** for fast, convergent spur/sideband
fidelity in Cadence PSS/HB near an RF carrier (~304 MHz). This is the literature/industry
baseline against which our per-block behavioral model is compared. Companion docs:
`../GENERALIZATION_REPORT.md`, `../HANDOFF.md`.

---

## 1. Landscape — 7 method families

### ① Analytical / classical small-signal (production "mental model"; a *recipe*, not a runnable model)
- **Rincon-Mora / Gupta impedance-divider PSRR**: `PSR = 1/AIN`, `PSRdc = (Aol·β)⁻¹`, zero @ BWA,
  pole @ UGF (worst PSR), output pole @ pout, ESR zero @ `1/(2π·RESR·CO)`. One-to-one with the
  measured PSRR Bode plot. (Rincon-Mora, *Analog IC Design with LDOs*, 2009; TI/ADI app notes.)
- **Loop-gain feedback Zout**: `Zout(s) = Z_o,ol/(1+T(s))` → the +20 dB/dec **inductive rise** and
  the load-step ring; ESR zero placed to recover phase margin. (Razavi, *A Circuit for All
  Seasons*, SSC-M 2019; ADI *Ask The Applications Engineer-37*.)
- Closed-form output-noise PSD = input-referred noise × noise gain; capless/multi-stage symbolic
  compensation (DFCFC, nested Miller, pole-splitting); NMOS-SF vs PMOS-CS supply-path analysis.
- **Limit**: a recipe whose extracted poles/zeros you then realize as a behavioral block; real-pole
  counting misrepresents resonant (complex-pole) magnitude **and** phase — exactly where spur
  amplitude/phase live. Says nothing about intrinsic spurs.

### ② Behavioral macromodeling — Verilog-A/AMS + controlled-source equivalent circuits *(system-sim mainstream)*
- Boyle-style / modern G-E controlled-source equivalent circuits; **laplace_nd/zp/zd/np** transfer
  blocks; pure lumped-RC / ddt-idt ODE realizations; event-rich wrappers (soft-start, current-limit,
  UVLO, PWM/PFM); HB-engineered Verilog-A (Brinson/Qucs RF discipline).
- **Kundert constraint (critical)**: SpectreRF supports only **ddt/idt/idtmod/Laplace** state in
  periodic analyses. `delay`, z-transform, `transition/slew/cross/last_crossing`, `$random` are
  hidden-state → either abort or **silently kill sideband propagation**.
- The two accepted realization routes are **(a) laplace_nd** (Kundert/Beckett: best-conditioned,
  supports complex poles + non-min-phase + explicit delay) and **(b) synthesized RLC + controlled
  sources** (simulator-agnostic, "safest"). *(We use (b) only — see §3.)*

### ③ Black-box system ID / rational + passive macromodeling *(SI/PI baseline tooling)*
- **Vector Fitting (VF)** — Gustavsen-Semlyen, incl. **Relaxed VF** & **Orthonormal VF** (de-facto
  standard); **Sanathanan-Koerner (SK)** / Levy linearized LS *(we use SK for PSRR)*;
  **Loewner framework** (Mayo-Antoulas); **AAA** (adaptive Antoulas-Anderson, barycentric).
- State-space realization + **passivity enforcement** (Hamiltonian / half-size singularity tests);
  pole-residue → **RLC + controlled-source synthesis**; **delay-extraction / non-min-phase**
  (DEPACT, delayed-VF).
- **Key edge over us**: AAA/Loewner are auto-order, parameter-free, **make no minimum-phase
  assumption** → capture RHP-zero/high-Q-notch magnitude **and** phase with few poles.

### ④ Nonlinear frequency-domain behavioral (HB/PSS-native)
- **X-parameters / PHD** (spectral linearization around a large-signal op point);
  **hot-S / LSSS "6 = 4 + 2"** — a supply tone at `fc±fd` scatters into **coupled upper/lower
  sidebands** needing both the **S term and a conjugate-T term** (governs sideband **asymmetry**).
- Bias/load-dependent & envelope-tracking X-params; long-term-memory (electrothermal/trapping).
- **Simulator-side counterpart = PSS/shooting + PAC/PXF/Pnoise** (what we will actually run).
- For our near-carrier *linear* spur band these are **overkill** and risk non-passive in-loop
  behavior — but the hot-S lens is a useful **validation metric** for sideband asymmetry.

### ⑤ Machine-learning / surrogate *(mostly academic; few PSS/HB-deployable)*
- CTRNN→Verilog-A (Rosenbaum lineage), **ISS-Neural-ODE**, NODE-RNN, **SSDNN** (provable stability),
  GaN ANN large-signal model (validated in HB), GP / PCE / stochastic-testing surrogates, GNN/DNN
  performance predictors (LASANA).
- **Verdict**: not worth it for our LTI-supply use case; data-hungry, opaque, convergence-risky.

### ⑥ Power-integrity / PDN
- Regulator **Zout(f) 1-port** for PDN/decap co-design; **Zout + PSRR + intrinsic-noise small-signal
  N-port**; VF + passivity + equiv-circuit synthesis; **reduced-order modeling (MOR: PRIMA/SPRIM/PVL)**;
  SIMPLIS PWL/POP+AC for switchers; **EMI 3-terminal Norton** (impedances + independent sources);
  **PSRR → supply-induced jitter / sideband mapping (PSIJ)**.
- The PDN community independently converged on **our exact abstraction**: supply = impedance + noise
  + ripple.

### ⑦ SOTA for fast PSS/HB spur fidelity *(the "recommended composite")*
- **Passive rational 2-port (PSRR ≈ S21, Zout ≈ S22) + shaped-Norton noise + forced spur tones →
  PSS/HB + PAC/PXF/Pnoise to fold ripple/noise to sidebands.** Endorses op-point parameterization
  by load and the "near-carrier spur band is linear" finding. **This is essentially what we built.**

---

## 2. Dimension-by-dimension comparison (mainstream vs ours)

| Dimension | Mainstream | Ours | Assessment |
|---|---|---|---|
| Core supply abstraction | passive rational 2-port + shaped-Norton noise + injected spur tones, folded by PSS/HB | exactly this (per-block + PSS manifest) | **Even** — we independently landed on the textbook-endorsed architecture |
| Realization primitive | (a) laplace_nd *or* (b) synthesized RLC+ctrl-source | **(b) only, hard ban on laplace_nd** | **Mixed** — max convergence/portability; forfeits easy complex-pole/delay realization |
| Zout(f) | `Z_o,ol/(1+T)` rational/RLC; complex-pole pair; VF + passivity; MOR | `(R+sL)‖(ESR+1/sC)` + opt. 2nd R-L; multi-start LS; ln(iload) quadratic | **Close** — passive-by-construction (good); fixed 1–2 branch **under-fits V3 migrating resonance** |
| **PSRR mag+phase (non-min-phase)** | complex-data fit, **stability-only**; **signed 2nd-order**; explicit **RHP zeros**; **delay extraction** | SK-fit but realized as **strictly-real 1st-order signed** sections → falls back to min-phase shelf | **Mainstream ahead — our dominant residual** (V4 mag 0.04 dB / **phase 25°**; V3 phase 10°) |
| Output noise PSD | referred-source × noise-gain, Zout-shaped Norton | **decoupled** Norton@vout, `In=Sv/\|Zout\|`, white+6 Lorentzians, joint log-fit | **We ≥ mainstream** — decoupling survives Zout topology change; ~closed (≤2–3.6 dB) |
| Discrete spurs | LTI core relays only; inject deterministic source at vout; PSS folds | det. SIN at vout, `I_k=vout_amp/\|Zout\|`, **fundamentals only**, GCD manifest, **aggressor at vin** | **We ahead in execution** — avoids double-counting IM; correct PSS bookkeeping; closed |
| Op-point / load | parameterized fits / load X-param tables (smoothness = HB risk) | ln(iload) quadratic, 3 corners, **envelope clamp** | **Aligned + pragmatic** — clamp treats the smoothness liability directly |
| Fitter algorithm | VF (relaxed/orthonormal) baseline; AAA/Loewner emerging (auto-order, no min-phase); SK | bespoke multi-start LS (Zout) / SK (PSRR) / log-LS (noise) | **Mainstream tooling more capable** — biggest adoptable-tooling gap |
| PSS/HB robustness | linear-dominant, every node touches linear elt, passivity on Zout | all-lumped-passive-**by-construction**; no internal loop to stall | **We ahead** — the constraint buys the #1 convergence lever for free |
| Validation harness | usually AC + Pnoise + PSS/PAC, per single part, often mag-only | **phase-aware weighted composite, ~14 variants**, coherent transient-FFT | **We ahead** — lets us even *see* V1/V3/V4 phase residuals |

---

## 3. Where we stand

**✅ Aligned with best practice** — 2-port architecture = the survey's "recommended composite";
`In=Sv/|Zout|` shaped Norton; spurs as explicit injected tones; near-carrier linear → no X-param
needed; SK for PSRR (cleaner than VF for explicit RHP zeros); passive-by-construction Zout;
RLC+ctrl-source synthesis (the "safest" mainstream route); op-point clamp mitigates table-smoothness.

**🟢 Deliberate departures that are *better* for the spur/PSS goal**
- **Hard ban on laplace_nd** → best-in-class cross-engine PSS/HB robustness + portability, and avoids
  the documented **HB-vs-transient laplace evaluation discrepancy** (a direct spur-fidelity risk).
- **Noise Norton decoupled from Zout synthesis** (adding a 2nd Zout branch no longer re-shapes noise;
  fixed V1/V3 9.6/21.8 → 1.0/2.1 dB).
- **Fundamentals-only spurs + GCD manifest + "inject aggressor at vin"** discipline (no IM
  double-counting; correct commensurate/incommensurate PSS funds).
- **Data-driven per-block selector** (zero regression on 9 min-phase variants; complexity only where
  data demands).
- **Multi-variant phase-aware composite scoring.**

**🔴 Where we genuinely lag**
1. **PSRR phase on non-min-phase architectures — the dominant unresolved residual.** Our
   `i_c = G0 + Σ Gᵢ/(1+s/wᵢ)` uses **strictly-real first-order** sections (SK fitter returns None and
   reverts to a min-phase shelf the moment poles come out complex). Real signed sections give
   magnitude notches/sign flips but **bounded phase** — structurally cannot carry the phase of a
   genuine RHP zero or net loop delay. → V4 mag 0.04 dB but phase **25°**; V3 phase **10°**.
2. **No complex-conjugate (2nd-order RLC) PSRR sections at all** (we have them only on the Zout path).
3. **No explicit transport delay `e^-sτ`** — in HB a pure delay is an *exact per-harmonic* phase term,
   the most direct fix for the V4 "390° phase race".
4. **Fixed-topology Zout** under-fits load-migrating multi-pole resonance (V3 resonance migrates
   0.27 → 10 MHz with load); free-order VF/AAA would auto-select order.
5. **No explicit passivity verification** (Hamiltonian/half-size) as a guard.
6. **Not leveraging standard fitters** (relaxed/orthonormal VF, AAA, Loewner).
7. **No hot-S (S + conjugate-T) sideband-asymmetry check** — asymmetry depends entirely on PSRR/Zout
   phase, our weak spot.

---

## 4. Adoptable ideas (prioritized)

### For the Zout/PSRR fidelity task (next)
1. **[HIGHEST] Add signed 2nd-order (complex-conjugate-pole) PSRR sections**, realized as **RLC + VCCS
   (no laplace_nd needed** — a damped 2nd-order RLC driving a VCCS *is* the section). **Stop discarding
   the SK fit's complex poles** — realize them. Lifts the phase ceiling that strictly-real sections
   impose; primary fix for V4/V3 phase.
2. **For the V4 390° phase race**: explicit **delay extraction** `H(s)=e^-sτ·H_rational(s)` (fit τ,
   Hilbert-causality-checked), realize `e^-sτ` as a low-order **Bessel/Padé all-pass of R/L/C +
   ctrl-sources** (stays in the no-laplace constraint). Exact per-harmonic phase in HB.
3. **Swap fixed-topology Zout LS for a real VF/AAA fitter** (scikit-rf `VectorFitting`, or AAA) as the
   front-end — auto-order directly addresses V3's migrating resonance; AAA/Loewner make no min-phase
   assumption; then synthesize to RLC+ctrl-source as we already do.
4. **Fit PSRR from complex (real+imag) data with stability-only enforcement** (never passivity — PSRR
   gain is legitimately non-passive); add RHP-pole flip + stability guard.
5. **Add automated passivity verification** (Hamiltonian/half-size, in scikit-rf) on the synthesized
   Zout port as a harness CI gate.
6. **Add a hot-S / S+conjugate-T validation metric**: after PSS+PAC, compare upper vs lower sideband
   (`fc±fd`) amplitude **and** phase vs the transistor-level reference — turns the PSRR-phase residual
   into a directly spur-relevant pass/fail.

### For Target B (real Cadence LDO import)
7. Mirror the **EMI 3-terminal-Norton extraction**: Zout by AC (shunt-through), noise by Pnoise, spur
   comb by coherent transient-FFT → same **extract → VF/AAA → RLC** pipeline.
8. **Beware SpectreRF shooting-Pnoise**: a slow LDO supply pole can cause LF supply-noise upconversion
   to be **under-reported** — validate vs HB-Pnoise or a fine PAC sweep, not shooting alone.
9. Keep **switching/PWL/SIMPLIS-POP characterization strictly offline** (MHz fsw vs GHz carrier
   timescale separation); extract Zout/PSRR/ripple-comb, then inject as rational paths + forced tones.
10. Use **Relaxed VF** for noisy real-silicon data (better pole relocation than naive SK over the
    mHz–100 MHz span where monomial bases are ill-conditioned).

---

## 5. Verdict

Our architecture is correct and, in several respects (noise decoupling, spur manifest / aggressor-at-vin
discipline, multi-variant phase-aware scoring, convergence-by-construction), **ahead of generic
published practice** — the survey's "recommended composite" essentially describes what we built. The
genuine, well-justified departure is banning `laplace_nd` for pure lumped R/L/C: it buys best-in-class
PSS/HB robustness and cross-engine portability at the cost of fit expressiveness. **That cost is now
concentrated in exactly one place: PSRR (and migrating-Zout) phase on non-minimum-phase architectures**,
because our PSRR coupling current is restricted to strictly-real first-order sections with a bounded
phase ceiling. This is **not a reinvention problem** (we don't duplicate a standard we ignored) **but an
under-exploitation problem** (we use SK then throw away its complex/RHP content at realization). Single
highest-leverage move: **add signed 2nd-order (complex-conjugate RLC) PSRR sections + explicit delay
extraction as a lumped all-pass (both inside the no-laplace constraint), and swap fixed-topology Zout LS
for a VF/AAA fitter with auto-order + a Hamiltonian passivity gate.**
