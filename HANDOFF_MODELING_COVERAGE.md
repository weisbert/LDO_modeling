# HANDOFF — Coverage-Driven Modeling (load sweep · I-V · transient/slew · temperature)

**Status:** PLAN LOCKED (2026-06-19). This is the build spec for the next ultracode session.
Pure planning doc — no code changed yet. Grounded by two adversarial expert evaluations this
session (a senior LDO/PMU circuit designer + a behavioral-modeling/identification expert), both
of whom read the repo. Their convergent findings are distilled below.

---

## 0. Why this exists (the gap)

There are TWO modeling paths in this repo:

- **`harness/`** — the ORIGINAL, validated ngspice flow. It DID the full job: multi-load sweep,
  Zout/PSRR/noise AC, current-source I-V compliance (`emit_isrc`/`fit_isrc`, 8/8 vs MOS-GT),
  transient load steps incl. **slew** (`bench.py STEP_DI = {lin, big, slew}`), and a `slew_en`-gated
  nonlinear slew/dropout core (`fit_model.py`, validated in `score.py`/`report.py`).
- **`cadence/insitu/` + `cadence/cluster/`** — the NEWER in-situ Cadence-box path (unified source
  reuse, Donau+ALPS). It currently extracts **only small-signal AC + noise at ONE load → an LTI
  model**. `iv_sweep` is carried in the schema but **never wired** into the netlister/matrix.

So the in-situ tool is a **partial port** of the harness flow. This spec brings the in-situ path up
to the harness's coverage, makes the measurement set **user-selectable by coverage**, and fixes the
model/identifiability issues the experts found.

### The footgun to kill first
`harness/fit_multiport.export_single_port_refs` injects a **synthetic FLAT** `dc_loadreg` /
`dc_dropout` / `dc_linereg` stand-in. If anything emits the `slew_en=1` core from this, the
dropout/load-reg/current-limit behavior is **FABRICATED**, with no flag at the model boundary. This
must become structurally impossible (see §4 anti-footgun gating).

---

## 1. Core methodology decisions (locked)

1. **Small-signal comes from AC only.** Zout(s), PSRR(s), output noise, sink admittance Y(s),
   current-PSRR pi(s) are extracted from one-hot AC + `.noise`. Never fit these from transient.
   Rationale: for an LTI system a load step = convolution with Zout, so a small-signal transient is
   **information-free** — AC already determines droop/ring/settle exactly. (`trans_id.py` is the proof.)

2. **Transient is run ONLY for large-signal behavior** AC cannot see: slew-rate limiting, current-
   limit/foldback, recovery asymmetry (load-on ≠ load-off). **Fit large-signal params on the
   LTI-SUBTRACTED RESIDUAL** (predict the step from the already-fixed Zout, subtract, fit what
   remains) so the large-signal fit can never pull on the small-signal parameters.

3. **Static nonlinearity (dropout knee, current-limit, global load-regulation) comes from a DC
   sweep**, not transient — cheaper and more robust. Transient is reserved for the *dynamic* slew/
   asymmetry. NOTE: slew (a dynamic dV/dt rate limit) and dropout (a static compliance knee) are
   DIFFERENT physics — model them as separate terms (a rate clamp vs a knee), do not conflate.

4. **Load dependence = parameter scheduling** (each param a clamped quadratic in `ln(iload)`, the
   harness `_pexpr`/`_poly` + envelope-clamp approach) brought into the multiport emit. NOT per-load
   independent constants (current multiport bakes one OP), NOT an analytic load law.

5. **Coverage drives BOTH sims and emit.** A `coverage` selection deterministically gates which
   measurements run AND which VA terms are emitted, so a low-coverage run is structurally incapable
   of emitting a fabricated high-coverage term.

---

## 2. Coverage tiers (nested presets; items also individually selectable)

The user selects coverage; default = FULL. Tiers are additive presets over individually-checkable
items. `slew_en` is a separate VA PARAMETER (default 0) — even when T1 is extracted (model carries
the slew core), the model runs LTI by default until the user sets `slew_en=1`.

| Tier | Adds (sims) | Adds (VA terms) | Identifies |
|---|---|---|---|
| **T0 · LTI** (today) | one-hot AC (z, couple, p, y, pi) + `.noise`, at the OP | Zout RLC, PSRR i_c+1 complex section, Norton noise, Y=g0+sCp, **signed** pi(s) | small-signal @ one OP |
| **T1 · +slew** | transient load steps (lin=validate / big / slew) | `slew_en`-gated **additive** slew/recovery correction (dV/dt rate clamp + recovery τ), fit on LTI-subtracted residual | slew rate, recovery asymmetry |
| **T2 · +I-V / dropout** | DC `iload` sweep past compression (per rail) + i_out vdc I-V sweep | additive dropout knee + current-limit (tanh/PWL, =0 at OP), real load-reg; current-bias large-signal core `idc55/vknee/g0/Cp` | static nonlinearity, dropout, current-limit, bias I-V |
| **T3 · +load schedule** | repeat AC/noise (and DC) at the per-rail log-spaced loads | every small-signal param → clamped quadratic in ln(iload) | load dependence |
| **T4 · +temperature** | repeat the selected set at each temp corner | `Idc(T)` didt / PTAT, temp-scheduled params | thermal drift |

**DEFAULT = full (T0–T4).** `slew_en` VA param default **0**.

---

## 3. Concrete sweep parameters (locked — capless LDO)

The designer's TB LDOs are **CAPLESS (no output decap)** → light-load is stability-limited (output
pole g_mp/C_par migrates toward UGB at light load); heavy-load is dropout-limited (only 200 mV
headroom, 0.8 V out from 1.0 V). Load mins raised to the capless stability floor (~1–5% of max).

### Per-rail load sweep
| | PLL rail (VDD0P8_PLL) | VCO rail (VDD0P8_VCO) |
|---|---|---|
| nominal OP | 500 µA | 2 mA |
| min / max | 50 µA / 2 mA | 200 µA / 6 mA |
| 4 log points | **50 / 170 / 580 / 2000 µA** | **200 / 620 / 1900 / 6000 µA** |
| held-out (crossval) | 300 µA | 3 mA |
| binding limit | light-load PSRR/stability | heavy-load dropout |

Capless implications baked in: (a) Zout/PSRR resonance peak MIGRATES with load → the held-out
crossval point validates the **resonance trajectory** (peak f + Q), not just magnitude; (b) Cout/ESR
auto-extracts to a small parasitic — fine; (c) a too-low load point will RING — the on-box stability
self-check (§5 guardrail 4) flags it so the user raises the floor.

### Per-rail transient steps (at the OP unless noted; USER-OVERRIDABLE in GUI)
| step | PLL @500µA | VCO @2mA |
|---|---|---|
| linear-validation (small) | ±50 µA | ±200 µA |
| compression-onset (big) | ±500 µA | ±1 mA |
| slew/large-signal | 0 → 2 mA | 0 → 6 mA |
Extra: run the VCO big step also at 1 mA OP (headroom dependence). Edge time fixed across the three
steps (≈1 ns or the real load-event edge) so amplitude is the only variable. Default transient loads
= the OP(s) above; user can add/change in the GUI.

### Temperature corners
**-40 / 55 / 125 °C.** Nominal/room = **55 °C** (matches `emit_pmu_model tnom_c=55.0` + the GT
library). Worst dropout = VCO max-load @125 °C; worst light-load stability = mins @ -40 °C.

---

## 4. Anti-footgun: tier-gated emit + provenance

- **A T0 (LTI) run MUST be structurally unable to emit a dropout/load-reg/slew term.** Gate every
  large-signal emit block on the coverage tier in `emit_pmu_model.py`.
- **DELETE the synthetic flat DC stand-in** in `fit_multiport.export_single_port_refs`. If a tier
  that needs DC/transient data was not run, emit NOTHING for that term (not fabricated-flat data).
- **Stamp the `.va` header** with `// COVERAGE=<tier>  OP=<iload>@<temp>  VALID_LOAD=[min..max]` so a
  system-sim consumer can see the model's scope at the boundary.

---

## 5. Guardrails (cheap, prevent silent-wrong models — ALL four in scope)

1. **Additive `slew_en` gate (model-structure fix).** Today `slew_en` is a BRANCH SWAP
   (`R_a` ↔ PWL) so flipping it can DISCONTINUOUSLY change small-signal Zout. Restructure to an
   ADDITIVE correction: `I_branchA = V/R_a + slew_en * I_nl(V, dV/dt)` where `I_nl(OP)=0` AND
   `∂I_nl/∂V|OP = 0` (a tanh-style envelope flat through the OP, like the current block already does).
   Then `slew_en=0` ≡ the AC fit EXACTLY by construction, and HB/PSS run on a clean linear core.
2. **Preserve current-PSRR SIGN/phase.** The legacy current block collapses pi to `|PI(0)|`;
   `importmp` already has the full complex `PI = -I/Vsup`. Carry the sign (matters when sinks share
   VREF and ripple currents superpose).
3. **Zout(0) ↔ DC load-reg consistency assertion.** `Zout(s→0)` from AC == local `dVout/dIload` from
   the DC load-reg sweep. Assert `|Zout(0) - dVout/dIload_DC| < tol` — catches a fabricated/mis-scaled
   DC curve immediately.
4. **On-box linearity + stability self-check.** Add a cheap probe: rerun one AC point at 2× drive
   amplitude; ratio-invariance certifies the OP is linear (the one-hot superposition assumption
   holds). Also flag Zout peaking / Q→∞ at a load (capless light-load ringing) so the user raises the
   load floor. The in-situ AC path currently has NO such guard (`trans_id.linearity_gate` exists
   offline only).

---

## 6. Measurement → model mapping (the build target)

| VA term | Sim point | Fitter | Tier |
|---|---|---|---|
| Cout, ESR | AC `z` asymptotes | `fit_cout_esr` | T0 |
| Zout RLC (A/B/C) | AC `z` | `fit_zout` | T0 |
| coupling Z_a→b | AC `couple` | `fit_zout` | T0 |
| PSRR i_c + 1 complex | AC `p` (Zout fixed) | `fit_psrr` | T0 |
| Output noise Norton bank | `.noise` `n` | `fit_noise_bank` | T0 |
| Sink Y=g0+sCp | AC `y` | `_fit_admittance` | T0 |
| Current-PSRR pi(s) **signed** | AC `pi` | `_fit_cpsrr` (keep sign) | T0 |
| Slew rate, recovery τ | transient (LTI-subtracted residual) | NEW | T1 |
| Dropout knee, current-limit, load-reg | DC `iload` sweep past compression | `build_pwl` on REAL data | T2 |
| Bias-current large-signal `idc55/vknee/g0/Cp/gdd` | i_out vdc I-V sweep + temp | `fit_isrc` (already exists) | T2/T4 |
| Param schedules f(ln I) | repeat AC/noise at the log loads | `_poly`/`_pexpr` + clamp | T3 |
| `Idc(T)`, PTAT, temp drift | repeat at temps | existing `didt` + schedule | T4 |

P0 correctness fixes folded in: (a) load axis on Zout/PSRR/noise (today single-OP — the #1 PSRR-vs-
load gap); (b) bias-current promoted from legacy-AC-only to the large-signal core (today in-situ
produces no `idc55` → no DC value / I-V / tempco / PSRR sign for `IBP_*`).

---

## 7. Complete change-list (by file)

- **`cadence/insitu/manifest.py`** — schema: add `coverage` (tier + per-item enables + params);
  per-`v_out` `iload` spec = **sweep + additional points** (Cadence-style: `{sweep:{type:log,
  start,stop,n}, points:[...]}` merged/deduped/sorted); transient params (step list, edge, window,
  ΔT); temp corner list; `slew_en` default 0. `measurements()` becomes **coverage-driven** + gains a
  load axis and a temp axis; new point kinds: `iv` (DC sweep), `trans` (PWL step), `lin_gate` (2×
  amplitude AC self-check).
- **`cadence/cluster/netlist_augment.py`** — emit (a) per-`iload` AC/noise runs (rewrite the reused
  v_out load idc's `dc=` per load point); (b) a **DC-sweep** group (sweep the reused source: i_out
  vdc for I-V, v_out idc for load-reg/dropout); (c) a **transient** group (rewrite the stepped
  source `dc=` → `PWL(...)` step, others stay DC OP, read v(out) over time); (d) the 2×-amplitude
  linearity self-check point. Reuse `_modify_*`/`_resolve_role_src` machinery (it already rewrites a
  named source's parameters in place).
- **`cadence/insitu/pmu_corner.py` / `run.py`** — orchestrate the load × temp sweep (loop loads,
  loop temps); collect per-(load,temp) PSF; `assemble_multiport` already stitches loads. Add DC +
  transient + lin-gate groups to `run.groups`. Storage layout gains per-load / per-temp dirs.
- **`cadence/insitu/importmp.py`** — new derives: `iv` (DC → I-V knee / load-reg / dropout),
  `trans` (transient → slew rate, asymmetric over/undershoot, current-limit, on the **LTI-subtracted
  residual**). KEEP current-PSRR sign (don't collapse to magnitude). Add the **Zout(0) ↔ DC load-reg
  consistency assertion**.
- **`harness/fit_multiport.py` / `fit_model.py`** — DELETE the synthetic flat DC stand-in; consume
  REAL multi-load + DC + transient. Bring `ln(iload)` parameter scheduling into the multiport path
  (today multiport bakes one OP). Wire the bias-current large-signal core (`idc55/didt/gdd/vknee`).
- **`harness/emit_pmu_model.py`** — tier-gated term emission (T0 cannot emit dropout); the
  `slew_en` ADDITIVE-correction restructure (guardrail 1); split dynamic slew (ddt clamp) from static
  dropout (knee); `.va` provenance banner (§4).
- **`gui/ldo_modeler.py`** — coverage selector (tier preset + per-item checkboxes); per-`v_out`
  `iload` sweep+points editor; transient step + temp-corner params; `slew_en` toggle; PLUS the two
  already-agreed UI fixes: **Tab-0 vertical scroll + fixed block heights** (wrap `_tab_extract` in a
  QScrollArea, no vertical stretch) and **Scan netlist reads the main-window netlist path** (pass
  `xb_netlist` into the editor; don't re-prompt). Manifest editor updates for the new schema.
  **GUI changes require offscreen-render visual confirmation (screenshot) before declaring done.**
- **Tests + `--selftest`** — full coverage of every new path; keep existing 157 backend + GUI
  selftest green.

---

## 8. Open items / confirm on box

- True VCO max system load (incl. start-up/lock surge) > 6 mA? If so raise the max. The DC sweep
  auto-stops where Vout falls out of regulation, so an over-high max is self-limiting.
- Exact capless min-stable-load — the stability self-check (guardrail 4) flags a too-low load point;
  the user raises the floor then.
- All transient/DC step robustness against a REAL maestro netlist is box-only.

## 9. Provenance
Distilled from two expert evaluations run 2026-06-19 (circuit-design + behavioral-modeling agents,
both read the repo). Related: [[ldo-unified-source-reuse]], [[next-pmu-cli-corner]],
`CADENCE_EXTRACTION.md` (the data contract this extends), `harness/` (the validated original flow).
