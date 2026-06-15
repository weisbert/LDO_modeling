# HANDOFF — PMU current-output behavioral model: full build (G1–G11)

**Created 2026-06-15 (planning session). Build in a FRESH ultracode session — see
`[[working-mode-ultracode-execution]]`.** The user decided to model EVERYTHING an independent
behavioral-modeling expert flagged about the PMU **current** outputs (bias sources/sinks).

## Why this exists
Today the current outputs are an **AC-only small-signal model of a large-signal device**. The whole
emitted Verilog-A for each current bias is (harness/emit_pmu_model.py:199–204):
```verilog
I(o,VSS) <+ g0*V(o,VSS) + Cp*ddt(V(o,VSS));        // admittance Y=g0+sCp
I(o,VSS) <+ pi_dc*(V(AVDD,VSS) - vdc_AVDD);         // current-PSRR (magnitude only)
```
No DC bias current, no I-V (sat/triode/compliance), no noise, no temperature, magnitude-only PSRR.
The expert review (this session) found 11 gaps (G1–G11); the user wants ALL of them built.

## Locked decisions (2026-06-15, "都按推荐来")
1. **Corners (G6):** TYPICAL-first — get the whole pipeline green at the typical corner, but design
   `emit` to be **corner-parameterized / able to emit a `.lib` corner family**; fill the full corner
   set (tt/ss/ff/sf/fs) in a SECOND round. Do NOT loop all corners in round 1.
2. **Temperature (G2/G3):** **3 temperatures** (per the PDK; default −40 / 27 / 125 °C). Emit
   `Idc(T)` and noise(T) as **low-order polynomials**; for the PTAT pin a linear-in-absolute-T law.
3. **Coupling (G9):** **VERIFY-FIRST** — one early step injects on one sink / the shared bias and
   checks whether the other sinks move. Build the n² cross-admittance ONLY for confirmed coupling
   (don't pay n² extraction blindly on a shared-bias assumption).
4. **Enable / startup (G10):** add an `enable` port + soft-start IF the TB exposes EN pins for these
   biases; ELSE compliance clamp only. Conditional on the real TB — resolve at build start.
5. **G1 compliance dc:** upgrade `i_out.dc` from optional+warning to **REQUIRED (or auto-read from the
   OP)**. Today an unset dc defaults to 0.0 V (per review: manifest.validate), so the probe clamps the
   pin to 0 V — the I-V zero point is wrong. Fix the default-to-0 trap.

## The build, by pipeline stage
The 11 gaps collapse into a few new EXTRACTION passes + connected fit/emit extensions — not 11
independent efforts.

### Phase 1 — Extraction  (`cadence/insitu/augment.py`, `manifest.measurements`, `run.groups`, `importmp`)
New per-i_out sample points, batched into passes:
- **Pass A «DC»** — sweep the (already port-isolated) probe `dc` `0 → VDD+margin`, READ the OP probe
  current, and sweep VDD → `I(Vpin)`, `Idc`, `I(VDD)`. Covers **G1** (real bias current), **G5**
  (I-V: saturation/triode/compliance knee), **G8** (dIbias/dVDD).
- **Pass B «noise»** — probe as the noise output → current-noise PSD. **G3**.
- **Pass C «coupling»** (gated on the G9 verify) — inject at sink A / the shared bias, read sink B's
  probe current → cross-admittance. **G9**.
- **Outer loops** — temperature (3 pts) × corner (typical in round 1). **G2** (incl. PTAT) + **G6**.
- Work: new measurement tags (e.g. `idc_`, `iv_`, `ivdd_`, `ncur_`, `ccur_`), extend `run.groups`
  (these are new analysis groups — DC sweep / DC op / noise per sink), `importmp` derive branches,
  and the manifest schema (require `i_out.dc`, or auto-read it from the OP).

### Phase 2 — Fit  (`harness/fit_multiport.py`)
- `Idc` capture [G1]; soft-saturating / PWL I-V fit + compliance knee [G5]; `dIdc/dVDD` slope [G8];
  `Idc(T)` poly + PTAT law [G2]; current-noise bank — **reuse the voltage Norton noise fitter** [G3];
  **stop collapsing PSRR to `|PI(0)|`** at fit_multiport.py:188 — keep `pi(s)=c0+s·c1` complex
  (sign + frequency) [G4]; allow a 2nd-order admittance when `yrms` is high [G7]; cross-admittance
  [G9]; corner family [G6]. Report (fit_multiport.py:331–336): add an `Idc` column, a `pi` sign/phase
  column, and a **DC-current GT-vs-model check** so G1/G4 can't pass silently [G11].

### Phase 3 — Emit  (`harness/emit_pmu_model.py::_current_block`, lines 175–205)
Replace the 2-line block with: baked `Idc` + I-V (PWL or `tanh`) + `Cp` + higher-order poles +
**complex / 1-pole PSRR** + noise sections (`white_noise`/`flicker_noise`) + `Idc(T)` temp law
(uses `$temperature`) + `Idc(VDD)` line term + cross-coupling terms + optional enable/soft-start +
compliance clamp; corner-parameterized. Update `va_sanity` for the new ports/terms.

### Phase 4 — GUI  (`gui/ldo_modeler.py`)
Form: temperatures, corner mode, enable-port toggle; **compliance vdc now required**; surface the new
current-port outputs (Idc, I-V, noise, PSRR sign) in the report. Thread the new params through
`ExtractCore.build_manifest_from_gui` / `build_model_cell`.

### Phase 5 — Tests
Extend the offline tests for each new term (DC current, I-V knee, noise PSD, PSRR sign/freq, temp
poly, coupling); keep GUI `--selftest` + the backend pytest suites green.

## Build approach (ultracode)
Multi-phase — recommend a **workflow per phase** (extraction-design → fit → emit → integrate), each
with an **adversarial verify** pass. The V/I physics is exactly where silent-wrong slips through:
this session's review caught a per-group-sweep bug AND a stale-fit bug that "tests-green" would have
shipped. Box-validation: the new DC/temp/noise/corner analyses run on the company box; offline tests
use stand-in fixtures (extend `pmu_standin`).

## Resolve at build start (open inputs)
- The exact PDK **temperature points + corner names** (ask the user / read the PDK).
- Whether the TB exposes **EN pins** for these biases (decides G10).
- The **G9 coupling verify** result (decides whether n² cross-admittance is built).

## Anchors (from the expert review — verify before editing)
- emit: `harness/emit_pmu_model.py:175–205` (`_current_block`)
- fit: `harness/fit_multiport.py:143–190` (`_fit_admittance`/`_fit_cpsrr`/`_fit_current_ports`),
  `:188` (the `|PI(0)|` magnitude collapse — G4), `:331–336` (report)
- measurements / dc-default: `cadence/insitu/manifest.py:128–185`, dc default-to-0.0 (G1/G5)
- importmp: `cadence/insitu/importmp.py` (`_derive`, `current_ports`)
- real pins: `cadence/insitu/manifests/pmu_real.json` (`i_out` block; `IBP_PTAT_TUNE_1P5U_VCO` = PTAT)

## Done in the PRECEDING session (don't redo)
The GUI front-end is BUILT + pushed (`e2bb752`): pin form → resolve → manifest, Build & Run
(ade default), Create model cell (combined VA+symbol, AVDD1P0 left/outputs right/VSS bottom). The
current-model extension below sits ON TOP of that working pipeline.
