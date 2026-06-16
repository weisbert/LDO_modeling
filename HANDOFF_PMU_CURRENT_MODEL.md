# HANDOFF — PMU current-output behavioral model: full build (G1–G11)

> **GUI PROGRESS 2026-06-16 (turns 7–10, all on `main`, HEAD `976c73a`):** the in-situ Tab-0 GUI got a
> big round of work ON TOP of the current-model engine. Done + green (GUI `--selftest` + 106 cadence
> tests): current-port overlay on the Compare tab + import-tab layout fix (`55dc4d5`); red-zone deploy
> crash fixed (lazy skillbridge in `adestate.py`, `43bf39d`); form-config persistence in `~/.ldo_modeler/`
> (`dfe7f00`); **Tab-0 MODE split** schematic/import + multi-supply (`gui['supplies']`) + engine/location
> split + Donau cluster panel + cluster `dsub` PREVIEW (`45cf3e4`, built via ultracode scout→implement→
> adversarial-review); ahdllibdir + PDK now OPTIONAL — sim self-resolves from the netlist (`2651286`);
> Mode-B input trim (ADE off, local/cluster meaningful, model-cell auto-name, `2bd74a1`); skillbridge
> connection indicator (`976c73a`). **Box-coupled GUI remainders:** per-group netlists for a REAL Mode-B
> cluster sweep (one input.scs = preview/plan-only); N-supply-input model cell (emit PSRR vs ALL supplies
> — today only the first, LOUD-warned). The engine-side NEXT list below is unchanged.

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

## PROGRESS 2026-06-16 — current model BUILT + validated offline AND on local Spectre (4 commits, all PUSHED)
User reframed: **object = MOS-transistor-level, deliverable = behavioral** (the LDO pattern).
Whole pipeline built + validated twice (ngspice + Spectre). Commits on `target-b-cadence-bringup`:
`df2eb3f` (GT+fit), `6fc3f83` (VA emit), `ca9fbbb` (Spectre flow+bugfix), `0127a28` (user knobs).

1. **GT object set** `ground_truth/isrc_gt.lib` — **8 diverse transistor-level current sources**
   (anti-overfit; user asked ≥6): simple/cascode/long/Wilson NMOS sinks, simple/cascode PMOS sources,
   PTAT β-multiplier, resistor-biased. Char'd → `work_isrc/*.npz` (`harness/isrc_char.py`,
   `isrc_variants.py`, registry+real-pin→archetype map).
2. **Behavioral fit** `harness/fit_isrc.py` (anchored OP + 2-point gate, no optimizer) → **emit**
   `harness/emit_isrc.py` (ngspice B-source) → **cross-val** `harness/crossval_isrc.py`: **1 template
   reproduces ALL 8** (Idc ≤0.36%, IV ≤4.9% RMS, rout ≤6.6%, PSRR sign ok, PTAT ≤0.001).
   Satisfies **G1/G2(PTAT)/G3/G4(sign)/G5/G7/G8** + **G11** (GT-vs-model DC+sign in crossval).
3. **Cadence VA emit** `emit_pmu_model.py::_current_block_largesignal`:
   `I=(Idc(T)+g0*(Vo-vc)+gdd*(Vsup-vdc))*tanh((knee_arg/Vk)^p) + Cp*ddt + white/flicker noise`
   ($temperature KELVIN; sink drives o→gnd, source sup→o; legacy AC-only path kept). Bridge
   `current_crow_from_isrc_fit`. `supply_dc`/`tnom_c` are kwargs (read fit meta).
4. **WHOLE FLOW ON LOCAL SPECTRE 18.1** `cadence/isrc_spectre.py --sc`: GT ported to Spectre spice
   (BSIM3 level 8→49, `{p}`→bare) → char IN Spectre → fit → emit VA → **ahdlcmi −64 compile + sim** →
   vs GT same probe. **8/8 self-consistent.** Caught a REAL bug: `emit_pmu_va` hardcoded
   `supply_dc=1.0` vs GT 1.05 → fixed (kwarg/meta). Env in `cadence/spectre_run.py`.
5. **User-definable knobs (cross-project reuse)** — `iv_sweep` (per-i_out I-V knee sweep, G5),
   `temps`, `tnom_c` flow GUI form (`gui/ldo_modeler.py` xf_ivsweep/xf_temps) → `build_manifest`
   → manifest → emit. Plus `supply_dc`. **No project-specific constant left in the harness.**

6. **Current ports in the air-gap REPORT (paste-to-reproduce)** — `report.py` now emits a **`[8]
   CURRENT PORTS`** section when the ref carries current ports: per-port behavioral-fit-vs-device-GT
   scorecard (Idc, IVrms%, rout, Cp, signed gdd + sign-match flag, current-PSRR/noise RMS, PTAT) +
   plain-language diagnosis + a **machine-readable `[8d] CURRENT GT DIGEST`** (I-V / Idc(T) / Y(s) /
   current-PSRR / current-noise). `digest_import.py` parses `[8]` back into a **fit_isrc-ready
   `results/ref/<name>__<pin>.npz` per port** (+ folds them into the main ref). New module
   `harness/current_digest.py` (ref↔per-port namespace `iport_<pin>__*`, the model-vs-GT metrics, the
   digest emit/parse); `fit_isrc.py` gained `predict_*` (pure-numpy analytic twin of `fit_model.predict`)
   and now accepts a dict view. **So a copied report reproduces a CURRENT discrepancy locally** — the
   same way `[7]` does for voltage. Test `harness/test_report_current.py` (round-trip: GT→report→paste
   →fit_isrc within tol; PSRR sign + PTAT preserved). HONEST scope (in the report): the scorecard is the
   ANALYTIC fit-vs-GT; emitted-netlist/probe-sign fidelity still needs `isrc_spectre.py --sc`.

Tooling: **ngspice built from source** at `~/.local/bin` (EPEL el8 lacks it — see
`[[ngspice-built-from-source]]`); local **Spectre 18.1** via `cadence/spectre_run.py`.

**REMAINING (box-coupled — the real PMU):**
1. **Wire `fit_multiport` to PRODUCE the large-signal fields** (idc55/didt/vknee/knee_p/gdd-signed/
   in_white/in_kf/pol/vc/cp) — today it only emits AC small-signal. The field schema is fixed by
   `fit_isrc`/`current_crow_from_isrc_fit`; match it.
2. **Phase-1 extraction** (`augment`/`manifest.measurements`/`run.groups`/`importmp`) must CONSUME the
   new manifest fields: `i_out.iv_sweep` (DC sweep the probe → I-V knee), `temps` (temp loop →
   Idc(T)/PTAT/noise(T)), producing the npz `fit_multiport` reads.
3. **Thread** manifest `supply_dc` + `tnom_c` into `fit_result.meta` so `emit_pmu_va` bakes them.
4. Then **G9** coupling (verify-first), **G6** corner-family `.lib` (round-2). **G11 report columns are
   DONE for the analytic channel** (`[8]` above); on the box, make the real extraction populate the
   `iport_<pin>__*` ref keys (via `current_digest.embed_port`, schema = `fit_isrc`) so the report's
   `[8]` shows REAL ports, not just offline GT.
The offline `emit_isrc` (ngspice) + `cadence/isrc_spectre.py --sc` (Spectre) are the REFERENCES the
on-box run must reproduce.

**GOTCHAS (learned, don't rediscover):** ngspice/VA gate `(V/vk)^p` blows the OP Jacobian at Vo=0 when
p<1 → **sqrt-floor the base**. Sink PSRR sign: probe reads `i(vout)=−I_pin` → **gdd_eff=−gdd (sink),
+gdd (source)**; source drives `I(supply,o)`, sink `I(o,gnd)`. VA `$temperature` is **Kelvin**
(328.15=55 °C). BSIM3 **ngspice level=8 → Spectre level=49**, strip `{param}` braces. **supply_dc MUST
equal the characterization supply** (the bug that cost v4/v5/v7 until fixed).

## CONFIRMED build inputs (2026-06-15) — the 3 former open items, now CLOSED
- **Temperatures: −40 / 55 / 125 °C** (the 55 °C center matches the typical-corner nominal — NOT the
  earlier −40/27/125 placeholder). `Idc(T)`/noise(T) = low-order poly; PTAT = linear-in-absolute-T.
- **Typical corner label = `tt_55c`** (round-1 typical). Corner still `pull_from_session` with this as
  the GUI fallback; foundry corner-family names only needed in round-2 `.lib`.
- **G10 = compliance-clamp ONLY, no enable port.** Confirmed against the repo: the PMU's only enables
  are block-level `BIAS_EN/PLL_EN/VCO_EN` (`extract_pmu.py:43-44`, `pmu_top_symbol.il:33-35`), already
  in `leave_alone` (`pmu_top.json:28`) and held by the TB; the 3 current outputs have NO per-output EN,
  and those block enables are NOT on the locked model symbol (AVDD1P0 left / 6 outs right / VSS bottom).
- **G9 coupling stays verify-first on the box** (structural prior: all 3 biases are `*_VCO` off a shared
  `vref_bias`/`IBIAS` ref → expect coupling, but confirm before paying n²).

## Locked decisions (2026-06-15, "都按推荐来")
1. **Corners (G6):** TYPICAL-first — get the whole pipeline green at the typical corner (`tt_55c`), but
   design `emit` to be **corner-parameterized / able to emit a `.lib` corner family**; fill the full
   corner set (tt/ss/ff/sf/fs) in a SECOND round. Do NOT loop all corners in round 1.
2. **Temperature (G2/G3):** **3 temperatures −40 / 55 / 125 °C** (CONFIRMED). Emit `Idc(T)` and
   noise(T) as **low-order polynomials**; for the PTAT pin a linear-in-absolute-T law.
3. **Coupling (G9):** **VERIFY-FIRST** — one early step injects on one sink / the shared bias and
   checks whether the other sinks move. Build the n² cross-admittance ONLY for confirmed coupling
   (don't pay n² extraction blindly on a shared-bias assumption).
4. **Enable / startup (G10):** **compliance-clamp ONLY — no enable port** (CONFIRMED; see above). Do
   not add EN to the model symbol; the TB holds the block enables at the real OP.
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

## Resolve at build start (open inputs) — ALL CLOSED 2026-06-15
- ~~PDK temperature points + corner names~~ → **−40 / 55 / 125 °C, typical `tt_55c`** (above).
- ~~TB EN pins (G10)~~ → **compliance-clamp only, no enable port** (above).
- **G9 coupling verify** — the ONE thing still resolved during the build (Phase-1 step on the box),
  not a pre-build input. Structural prior = coupled (shared `vref_bias`/`IBIAS`); confirm before n².

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
