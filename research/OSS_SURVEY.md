# Open-Source Tooling Survey — what to reuse, how others model LDOs
*(compiled 2026-06-06; raw multi-agent findings in `research/oss_survey_raw.json`)*

Goal: find open-source code we can reuse for our behavioral LDO model, and see how other
open-source projects model/characterize regulators. Companion: `MODELING_SURVEY.md` (methods),
`../HANDOFF.md`, `../GENERALIZATION_REPORT.md`.

---

## 0. Headline

**No open-source project builds what we build.** Every OSS LDO generator (OpenFASOC, AnalogGym,
sky130 IP, ALIGN) emits a *transistor netlist* and characterizes it with narrow testbenches — and
they specifically **omit Zout, noise, and spur extraction**, which are exactly the quantities our
model is built on. So our extraction + 4 behavioral blocks + physical synthesizer + OP-parameterization
are the defensible, un-replicated core.

**What OSS *does* give us, cheaply, is better FITTING and a real PASSIVITY GATE** — feed their
poles/residues into *our* synthesizer. Plus an open **HB engine + Verilog-A toolchain** to validate
sideband asymmetry and compile our `.va` outside Cadence (ngspice has no PSS/HB).

**Verified linchpin (2026-06-06):** `scipy.interpolate.AAA` is **already in our venv (scipy 1.17.1)**,
returns complex poles + `.residues()`, native complex-data, auto-order. Zero new dependency, zero
license risk → the cheapest possible first move on the PSRR/Zout phase task.

---

## 1. Adopt (ranked)

| # | Component | License | Use for | Status |
|---|---|---|---|---|
| 1 | **`scipy.interpolate.AAA`** | BSD-3 | auto-order Zout + PSRR fit; **keep its complex-conjugate poles** (our `_sk_fit` currently discards them — the exact gap) → feed our synthesizer | **already in venv (verified)** |
| 2 | **scikit-rf** `VectorFitting` | BSD-3 | (a) **passivity gate** on synthesized Zout: `passivity_test()` = the Gustavsen-Semlyen half-size Hamiltonian/singularity matrix + `passivity_enforce()` (SVD residue perturbation); (b) `auto_fit()` vector-fit cross-check vs AAA | `pip install scikit-rf` |
| 3 | **Xyce** multi-tone `.HB` | GPL-3 (external subprocess) | **HB validation of sideband asymmetry** near 304 MHz, on the SPICE subckt + `.va`; runs our exact element set (R/L/C + E/G/F/H + B) natively | Windows binaries exist |
| 4 | **OpenVAF-reloaded + VACASK** | GPL-3 / AGPL-3 (external) | **compile-and-check our `.va` mirror** (closes "VA untested locally") → `.osdi` → ngspice v39+ AC/tran parity, → VACASK `hb` for open HB | OpenVAF has Win binary; VACASK Linux-first (check WSL) |

**Reference-only (do NOT vendor):**
- **baryrat** (BSD-2) — AAA + **BRASIL minimax**; only if worst-case-bounded / lower-order sections
  specifically help (AAA already covers the base case).
- **polyrat** (GPL-3) — has a **stabilized Sanathanan-Koerner**; reimplement the published algorithm
  if our SK conditioning ever needs it (don't vendor — copyleft).
- **vectfit3 ports** (GPL / no-license) — algorithm reference only.
- **DEPACT** — delay-extraction recipe is **paper-only, no maintained code**; we implement the
  `e^-sτ` lumped Bessel/Padé all-pass ourselves.
- **AnalogGym / sky130_cw_ip** — extra ground-truth DUTs + PSRR/transient extraction parsing.
- **Brinson's Qucs modular op-amp macromodels** — topology inspiration (per-block, controlled-source,
  HB-robust PSRR/Zout/noise — same philosophy as ours, but hand-designed not data-fit).

**Explicitly do NOT pursue:** ML/RNN-to-Verilog-A lineage (no public code, nonlinear, PSS/HB-fragile,
mismatched to our verified-linear spur band); any OSS SPICE *emitter* (`write_spice_subcircuit_s`,
SignalIntegrity, SINTEF) — they emit a generic S-parameter state-space net with port reference
resistors, **not** our physical per-block realization.

---

## 2. How open-source projects actually model LDOs (vs us)

- **OSS LDO generators don't macromodel at all.** OpenFASOC `ldo-gen` builds a **digital** LDO
  (comparator + digital controller) → RTL/netlist/GDS, characterized **transient-only** (VREG vs clock
  freq vs Cout); no continuous-time Zout/PSRR/noise concept. AnalogGym ships transistor sky130 LDO
  netlists + `perf_extraction_LDO.py` that parses ~15 DC/AC/transient metrics (load/line reg, PSRR-AC,
  GBW, PM, under/overshoot) but **no Zout, no noise**. ALIGN/sky130_cw_ip = layout/IP, not models.
- **The SI/macromodeling world matches us conceptually** for the *linear* part: pole/residue fit →
  lumped realization (scikit-rf, baryrat, polyrat, DEPACT). But they target generic passive multiports:
  they fit immittance/S-params and synthesize **one generic state-space network**, with **no notion of
  a regulator's two distinct paths** (driving-point Zout that *should* be passivity-gated vs a PSRR
  transfer ratio that should *not*), **no noise/spur decomposition**, and **no OP-parameterization by
  load**.
- **Closest published philosophy = Mike Brinson's Qucs op-amp macromodels**: PSRR (auxiliary networks)
  + output impedance + noise from controlled sources + lumped elements, designed to survive HB — same
  per-block controlled-source HB-robust philosophy as ours. Difference: they **hand-design**; we
  **data-fit + gate**.

---

## 3. Mapping to our next tasks

**Task 1 — non-min-phase PSRR/Zout phase (2nd-order sections + delay).**
Today `_sk_fit` (harness/fit_model.py:147) **rejects complex poles** (returns None → min-phase shelf
fallback) — the precise gap.
- (a) Use `scipy.interpolate.AAA` now to fit `i_c = H/Zout` (PSRR) and Zout, **keeping** complex poles.
- (b) Extend `psrr_model` G-bank from `G0 + Σ Gᵢ/(1+s/wᵢ)` (real poles only) with **signed 2nd-order
  (complex-conjugate) RLC + VCCS sections** so notch *phase* is exact. AAA/baryrat `polres()` supplies
  the conjugate pairs; **our** synthesizer realizes them.
- (c) Cross-check with scikit-rf `auto_fit` (native conjugate pairs) on the same arrays.
- (d) **Delay**: we already have a Hilbert min-phase/linear-delay *diagnostic* (`bringup.py:_minphase_score`)
  but no `e^-sτ` *synthesis*. No OSS implements it (DEPACT paper-only). Implement DEPACT's recipe
  ourselves: extract linear-phase slope = τ, realize `e^-sτ` as a lumped Bessel/Padé all-pass.

**Task 2 — passivity gate on synthesized Zout.** Greenfield (no passivity code in harness). Adopt
scikit-rf `passivity_test()` + `is_passive()` + `passivity_enforce()`; feed our Zout poles/residues as
a 1-port immittance (positive-real test). **CRITICAL: gate/enforce ONLY Zout, never the PSRR ratio.**
Add as a hard gate in `score.py` after Zout synthesis; verify on **our hand-built RLC**, not just
skrf's internal realization.

**Task 3 — Target B + HB validation.** Import: if Cadence Zout/PSRR come as 2-port/Touchstone, use
scikit-rf `Network` Z/Y conversion + IEEEP370 de-embedding before fitting (optional for DC-extracted
GT). The extract→fit→emit pipeline (`import_cadence.py` CSV→gt_ref.npz, then existing fit+emit) stays
ours. HB validation: run emitted subckt under **Xyce multi-tone `.HB`** (304 MHz carrier + supply
tones); for the `.va`, OpenVAF-reloaded → `.osdi` → ngspice v39+ (AC/tran parity) and → VACASK `hb`.

---

## 4. Gaps with NO usable OSS (stay bespoke — our moat)

1. **Zout/PSRR/noise/spur extraction** from an LDO (`bench.py` + `gen_reference.py`).
2. **Per-block physical realization + emitter** (`fit_model.py` `emit`/`psrr_model`/`zmodel`) — all OSS
   emitters produce generic S-param state-space nets, wrong topology for us.
3. **`e^-sτ` all-pass delay extraction + synthesis** (DEPACT is paper-only).
4. **Noise Norton block** (white + Lorentzians, In=Sv/|Zout|) and **spur block** (deterministic vout
   tones) — no OSS models LDO output-noise PSD shape or discrete spurs.
5. **OP-parameterization by load** (poles/residues interpolated quad-in-ln(iload)) — no rational-fit lib
   couples a parametric sweep to circuit realization.
6. **Verilog-A mirror content** — OSS VA repos give device-model idioms only, no regulator behavioral
   patterns.

---

## 5. Recommendation

Adopt three, in order; keep everything else in-house:
1. **Now (free):** `scipy.interpolate.AAA` as the auto-order Zout/PSRR fitter; keep its complex poles;
   extend `psrr_model` with signed 2nd-order RLC+VCCS sections. Highest leverage, lowest risk.
2. **Install scikit-rf (BSD-3):** passivity gate on Zout only + `auto_fit` cross-check. Do NOT use its
   SPICE exporter.
3. **Install Xyce (GPL, external) + OpenVAF-reloaded/VACASK:** open HB validation of sideband asymmetry
   and a compile-and-HB check of the `.va` mirror.

Net: OSS gives a better fitter + a real passivity gate cheaply, plus an open HB cross-check. Extraction,
the four behavioral blocks, the physical synthesizer, the delay all-pass, and the OP-parameterization
remain our defensible core.
