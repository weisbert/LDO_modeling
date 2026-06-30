# Thread: large-signal load-transient / recovery (WuR PLL rail) — ACTIVE

owner: weisbert · last-touched: 2026-06-30 · supersedes the archived HANDOFF_RECOVERY_STDFLOW / EMIT_BAKE / DECAP_LTI

## Where it stands
Modeling the PLL-rail load transient FROM THE STANDARD FLOW (higher-order LTI Zout), not a hand-tuned recovery net.
- **Zout ladder STEP-2 SHIPPED** (`ab21b83`) — pll `extra=[(4.42µH,155Ω)]`, vco `[(1.23µH,50Ω)]`; |Z| err ≤0.34 dB @1/10/31.6 MHz; byte-identical when `extra` absent.
- **Minimal-emit SHIPPED** (`6833c61`) — all fitted params baked to `localparam`; only `vreg_<rail>` exposed. (Clarified: minimal limits the SCHEMATIC/CDF param set, NOT the internal physics — a bug had it strip the large-signal safety with slew/recov.)
- **PART2 compressive current-assist — BUILT + VALIDATED, UNCOMMITTED.** `i_assist = iaG·tanh(verr·|verr|/iaV²)`, verr=vreg−vout, ODD with f'(0)=0 EXACT → AC/PSRR/noise bit-identical (0.004 dB). Fixes the FF "trans goes negative" bug: the LTI Zout over-predicts a large step LINEARLY (163.6 mV/mA → −0.85/−1.924 V) because the real loop is class-AB SUB-linear; the assist saturates at iaG (class-AB), bending the dip to the silicon.
  - **DERIVED PURE-PYTHON, NO simulator** (`harness/fit_iassist.py`, folded into `fit_multiport`): the dip is a small linear branch-A-ladder+decap network + the assist nonlinearity → solved as an ODE (`predict_dip`, LSODA ~2 ms/solve, validated **<1.6 mV vs Spectre** both rails, baseline+assisted). `derive_iassist` fits iaG/iaV to the coverage.transient GT (`tr_*` already in the npz from the first ALPS run) — at the first fit, ≈5 s, runs box/GUI/CI. PLL ≈4.6 mA/0.44 V (held-out −1.2%), VCO ≈7.2 mA/0.31 V (0.0%) with the seed hidden. 2 params partly DEGENERATE → gate on dip-RMS+held-out, not the values. (The earlier Spectre-in-loop version + `pmu_corner.step_iassist` were REMOVED — local Spectre is not allowed in production.)
  - Wiring: `emit_pmu_model._iassist_*` (emit, BAKED localparams → SURVIVES minimal, NO new CDF param) + `fit_multiport` `derive_iassist` call + manifest `iassist` SEED (used only when no `tr_*`). Tests `harness/test_iassist_fit.py` (pure-Python) + `harness/test_iassist_core.py` (emit). Backstop `floor` OFF by default (manifest field to re-enable).
  - **VALIDATED vs REAL silicon**: my .va driven by the REAL idc=300µA iload (`cadence/wur_real_tb/va_model_iload_idc300_newtb.txt`) tracks `real_V_idc300_newtb.txt` to **6.2 mV RMS** (LTI-only 6.8). NB the assist is ~inactive for this fast-switching small-dip load (correct — small-signal, f'(0)=0); its value is on SUSTAINED big steps (0.5→4mA: 285→135 mV).
  - **GOTCHA — the replay REQUIRES the 20 pF decap** (`cadence/wur_real_tb/reconcile.py` setup: `Cext (VDD0P8_PLL 0) capacitor c=20e-12` + VCO/bias ports held at `vsource dc=0.8`). Driving the bare port current with NO decap over-predicts the dip ~4× (178 vs 37 mV real) — a TB error, not a model error.
- **Diagnosis: the trigger is NOT FF/V/T — it's the LOAD STEP.** Factorial on the bare model: supply 0.85→1.15 V and temp −40→125 °C move the trans min by 0 mV; higher vreg is SAFER (min=vreg−fixed-dip). The model has no process dependence. "FF"/"VDD>0.8" only correlate because the real PLL/VCO draws more current there → bigger di → deeper dip. The assist targets dip depth, so all corners are covered.
- **"30 mV startup drop" RESOLVED + CLOSED** (2026-06-30, 3-expert panel + user fix). It was the DC-OP initial charge of the 22.9 µH/534 ns branch-A fit-inductor (preload 37→405 µA ⇒ drop 55→4.8 mV). **User resolved it TB-side**: output iload redesigned as a STEP 600µA→300µA (the 600µA bakes the steady loading into the DC-OP → pre-charges the inductor → no phantom slew). Caveat to keep in the TB: stimulus-side fix; the big-inductor IC fragility remains — record the 600µA choice.
- **Slew core RETIRED** (2nd expert panel, unanimous): slew is WRONG-SIGN for the coverage dip; manifest knobs removed (`7b76d8b`). real_V (88 mV @25 ns) is a cold-start envelope (corr(I,V)≈0), held-out reference only.

Numbers: DATA.md §5. Verdicts/why: METHODOLOGY §"Large-signal / recovery".

## Next action
PART2 (the FF-corner "trans negative" 治本) is COMMITTED (`78f5a7a`) for both rails + stress-tested. What remains is deploy + optional refinement:
1. **`bash apply` + box re-validate** the assist (the box pulls; the deployed `.va` must regenerate WITH the assist, not the stale minimal box.va).
2. **(optional) clamp the UNLOAD overshoot** — the stress test found a hard load-removal overshoots ABOVE the supply (branch-A fit-inductor kick; assist halves it but the floor is one-sided). Non-physical for an LDO; needs a high-side clamp or a symmetric branch-A current cap. See BACKLOG.
3. **(optional) T55 z_pll re-export** to kill the documented ~×0.65 T-confound (AC z=tt_25c vs step GT=tt_55c). The assist gain currently absorbs it; a same-temp z would let the gain be pure compression and shrink the residual GT-RMS.
3. **(out of scope, reference-only)** the 88 mV cold-start envelope (real_V, corr(I,V)≈0) is a turn-on phenomenon, NOT this load transient — excluded by design; do not fit to it.

## Checklist
- [x] PART2 compressive i_assist built + Spectre-in-loop fit + held-out across amplitudes (PLL −3.7%) + AC bit-identical (0.004 dB) + no-negative under the floor backstop
- [x] B5: VCO assist fit (iaG=4.0 mA, iaV=0.22 V; rms 0.9 mV vs GT)
- [x] auto-fit `cadence/fit_iassist.py` (Spectre-in-loop) — params DERIVED from coverage GT, not hand-set; seed=manifest fallback; minimal-intervention tie-break (gentlest assist in the degenerate valley)
- [x] diagnosis: negative-trans is LOAD-driven (V/T/process-independent); refutes the "VDD>0.8 causes it" guess
- [x] WIRED into `fit_multiport` (pure-Python `derive_iassist`, after the LTI fit) — runs everywhere (box/GUI/CI), no simulator; the GUI gets it too (it IS pure-Python, not a sim). Backstop `floor` OFF by default (one manifest field to re-enable).
- [x] committed (`78f5a7a`) — assist + the Path-handling fix (`gt_dips_from_npz`/`derive_iassist` now accept `os.PathLike`; a `pathlib.Path` npz used to silently fall back to the seed on the GUI run path + emit/fit CLIs; only the box `step_fit` str()'d it) + Path regression test
- [x] stress-tested (local Spectre, full PMU .va, VDD 0.75–0.95, old/new/+floor): no runaway, all settle to vreg; FF-negative bug gone (abuse ~12 mA PLL −1.1 V→−0.39 V, floor ≥0). Found the UNLOAD overshoot-above-supply (BACKLOG).
- [ ] `bash apply` + box re-validate the re-emitted model
- [ ] (optional) clamp the unload overshoot; (optional) same-temp 55°C z_pll re-export to kill the T-confound
