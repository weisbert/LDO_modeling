# HANDOFF — model the LDO load-transient FROM THE STANDARD FLOW (higher-order LTI Zout)

Single source of truth for the next session. Supersedes HANDOFF_EMIT_BAKE_AND_VCO_RECOVERY.md (Part 0)
and RETRACTS HANDOFF_DECAP_LTI_RECOVERY.md (its "drop slew, go LTI" conclusion).

## ⭐ RESHAPE 2026-06-29 (LDO EXPERT PANEL, user-accepted) — the body below is PARTLY SUPERSEDED
A 4-lens LDO expert panel (workflow wc7ni4bva; full report results/redzone/wur_pmu_real_sweep.report.txt's
sibling — saved in the session transcript) UNANIMOUSLY reshaped the approach. Verdict = RESHAPE (3 reshape +
1 blocked, adjudicated RESHAPE). Key findings (all data-grounded; experts ran convolutions + local Spectre):
- The "SS-vs-LS Zout gap" (AC peak ~197 vs transient-needed 374-558) is an **ARTIFACT**, not physics. The
  measured z_pll is a monotonic rising resistive SHELF (0.095Ω DC → 197Ω plateau @~31.6MHz). The OLD single
  inductor (La‖Rpl)+Cout structure CANNOT make a rising-then-plateau shelf, so the fitter mislocated the
  corner to 447kHz and under-represented |Z| in 1-10MHz (where the dip lives). Convolving the MEASURED
  z_pll(‖20pF) with the clean 0.5→2mA step gives ~164-201mV vs GT 160mV — **the AC-consistent Zout ALREADY
  reproduces the coverage dip; La=120/558Ω is unnecessary** (558 was just the recovery net's series HF R).
- The recovery (~60-70ns, constant-τ/linear) IS the closed-loop dominant pole, lives IN the AC band, fittable
  from a richer AC Zout. SRa slew is NOT load-bearing for PLL (lti_la120 no-slew 2.37mV beat slew 2.47mV).
- **real_V is very likely a TURN-ON/SETTLING envelope, NOT a load transient**: 88mV dip bottoms where the load
  is at a local MIN, recovers as load RISES, corr(I,V)=+0.50, clean single-exp τ=39ns. So the prior 2.47mV
  "match" was an LC-tank overfit of a startup artifact (→ broke AC-consistency, collapsed to 35.5mV held-out).
  User CANNOT run the DC-held-load mechanism check (off-site) → **real_V is REFERENCE ONLY, not a gate.**
- **RETIRED as wrong physics:** the slew core, the Lreg‖Rreg recovery network, la_override=120µH, the en_ls
  gate, the Vcl/Imax/Gcl anti-windup clamp (the deadzone was pinning the clean-step dip at a constant 308mV).

NEW STRUCTURE (building now, workflow wq06g7awt): a SINGLE passive higher-order LTI Zout = a ladder of N
(L_i‖R_i) rising-shelf sections in series + series Ra (Z_DC=Ra=0.095Ω) + Cout/ESR. DC: inductors short→Ra;
HF: →Ra+ΣR_i=197Ω plateau; corners R_i/L_i place the multi-decade rise. Passive, convergent, AC-consistent,
no gating. ONE model owns peak+dip+recovery AND fixes the flagged PSRR non-min-phase (same missing section).
NEW ACCEPTANCE (user-locked): G1 Zout SHAPE gate (|Z| ±1.5dB @1/10/31MHz + plateau 197±10% + corner located,
NOT just broadband RMS which hid the 447kHz miss behind 1.9dB); G2 TIME-DOMAIN gate (clean 0.5→2/3/4mA into
20pF reproduces GT dips 160/227/282mV ±15% + sub-linear + τ~60-90ns); G3 passivity+DC/tran; +PSRR/noise no
regress. real_V = reference. Scope this run = PROOF on the PLL standalone .va; productionize into
fit_model/fit_multiport/emit afterward. Build_spec lives in the wq06g7awt synthesis.

## ✅ PRODUCTIONIZATION STEP 2 DONE (2026-06-30, `ab21b83`, main pushed) — ladder wired, zero-regression
The higher-order (L||R) ladder Zout is now WIRED through the in-situ pipeline (fit_multiport + emit +
report) with `extra=` threaded through fit_model's helpers. extra=None/absent EVERYWHERE -> byte-identical
(standalone crossval 15/15 artifacts identical to HEAD; emit_va/fit_all UNTOUCHED). fit_multiport
`_fit_voltage_output` calls `fit_zout_ladder` per rail with a GATED keep-best adoption (adopt iff N>=2 AND
|Z| dB-RMS cut >=0.3dB on the same grid; else extra=[] -> synthetic views byte-identical), stores
`P[il]["extra"]`, threads it into every per-corner PSRR/noise/score call (PSRR=i_c*Zout + noise=In*|Zout|
auto-reconcile to the richer Zout). emit_pmu_model `_extra_{nodes,rvars,asg,body}` insert the sections IN
SERIES in branch A (o->nA->nA2->...->[reg]->vrg; `_branchA_reg` gained `from_node`); DC shorts inductors
(Z_DC=R_a preserved), HF -> R_a+R_pl+sum(R_i) plateau; PSRR/noise emit bodies UNCHANGED (currents inject
at o). Scheduled path bakes the nominal-corner ladder load-independent (documented; real pll/vco Zout is
single-corner). Lock: `harness/test_zout_ladder_wired.py`. VERIFIED (build->verify workflow, 4 adversarial
lenses ALL pass): standalone byte-identical; emitted pll/vco .va local-Spectre AC pass G1 (|Z| err
<=0.34dB @1/10/31.6MHz, plateau +/-1-1.5%, corner ratio 0.89/0.76; single-section contrast over-predicts
1-10MHz +5.68dB -> ladder is a real fix; pll adopts extra=[(4.42uH,155)], vco [(1.23uH,50)]); PSRR
IMPROVED (pll 0.115 vs 0.208dB, phase 0.57 vs 4.14deg), noise flat; passive (0 neg-Re), DC->0.8V, tran
bounded; harness 208 + cadence 312 pytest green. ALSO `29179d7`: fixed a pre-existing red
cadence/test_pmu_loadreg_gt_spectre.py (synthetic step was inside the _settled_step 15% turn-on guard;
moved edge to 50% of capture, assertions unchanged). DEFERRED (needs user/box, unchanged): the G2 dip =
large-signal current-assist term OR a same-temp 55C z_pll re-export. STEP 2 thread CLOSED.

## PRODUCTIONIZATION (higher-order Zout into the pipeline) — STEP 1 DONE, STEP 2 = next
The build (workflow wq06g7awt) proved + emitted `cadence/wur_real_tb/ldo_pll_hiorder.va` (2-section
(L||R) ladder). KEY refinement of the panel claim, rigorously established (3 fits + Spectre + adversarial
verify): **G1 (AC Zout) PASSES emphatically** (|Z| <0.27dB @1/10/31MHz, plateau 198.8Ω, corner relocated
25kHz, 447kHz mislocation GONE), but **G2 (coverage dip) FAILS as a DATA inconsistency**: the AC-correct
ladder OVER-predicts the step dip ~1.7-2× (245/409/573 vs GT 160/227/282) and is exactly LINEAR vs GT's
sub-linear/concave (107/91/81 mV/mA). No passive LTI + any decap reconciles (dip needs ~250pF, tau ~20pF).
=> the AC 197Ω shelf is small-signal loop-Zout; the load-step dip is a SHALLOWER, saturating large-signal
response (out of passive-LTI scope). real_V (reference) = startup envelope. Plots: cadence/wur_real_tb/
cmp_{zout,step,dip_vs_di,realv}.png. User decision: PRODUCTIONIZE the AC-correct Zout now; defer the dip
(needs either a large-signal current-assist term OR a same-temp 55C z_pll re-export to rule out the 25/55C
confound -- user off-site, can't re-sim/re-export now).

STEP 1 SHIPPED (`940ec01`, additive, zero-regression): `fit_model.zmodel` gained optional `extra` list of
series (L_i||R_i) branch-A sections (None/[] -> byte-identical); `fit_model.fit_zout_ladder()` gated keep-best
ladder fit. On z_pll it recovers the build ladder (Ra=0.095, sec1=22.9uH/43, extra=[(4.42uH,155)], 0.22dB).
Lock `harness/test_zout_ladder.py`; 49 fit_model-dependent tests green. NOT yet wired.

STEP 2 = WIRE the ladder through the pipeline (best as a focused ultracode workflow w/ no-regression verify):
- `fit_multiport._fit_voltage_output`: call fit_zout_ladder per rail; store `extra` on P[il]; pass `extra=`
  to every zmodel/psrr/noise/error call (lines ~119/138/172/179/186) so PSRR(=i_c×Zout) + noise(=In×|Zout|)
  AUTO-reconcile to the richer Zout (re-fit by construction); carry `extra` to emit.
- thread `extra=None` kwarg through `fit_model`: psrr_model, _shelf, fit_psrr, _za, noise_model_sv,
  fit_noise_bank, fit_noise_hybrid (all just pass to their internal zmodel/_za; None -> byte-identical).
- `emit_pmu_model._voltage_body`/`_voltage_block`(+scheduled): emit the extra (L_i||R_i) sections in branch
  A between nA and the reg node (gated; absent -> byte-identical). Standalone `emit_va`/`fit_all` UNTOUCHED
  (pass nothing -> crossval byte-identical).
- VERIFY (acceptance): (a) crossval/standalone byte-identical (extra absent everywhere); (b) on z_pll/z_vco
  the emitted .va AC |Z| passes G1 (shape gate: peak freq/mag, not just broadband RMS) AND PSRR/noise do NOT
  regress vs today (0.45/1.22dB pll); (c) local-Spectre DC/tran converge + passive; (d) suites green.
  NOTE the report Zout grade should be upgraded to a SHAPE gate (peak-freq + per-decade), per the panel
  (broadband 1.9dB RMS hid the 447kHz/70× mislocation). DROP/ignore the retired slew/recovery/en_ls in emit.

---
### (ORIGINAL BODY — kept for context; the slew/recovery/guards framing is SUPERSEDED by the RESHAPE above)

## GOAL (user, verbatim intent)
Recovery / large-signal params (La, slew SRa, the recovery network) must be MODELED FROM THE STANDARD
PMU FLOW (coverage.transient load steps + AC Zout) and the resulting model must match the real silicon
LDO output. NOT hand-tuned. NOT fit to `real_V` (the real system-TB output is not a standard-flow
deliverable; real_V is held-out validation ONLY).

## SHIPPED THIS SESSION (all local-validated; commit pending push)
- **B2 sink g0-source bug FIXED** (`harness/fit_multiport.py` `_fit_current_largesignal`): emit now
  derives the sink output conductance from the AC-admittance DC real part (mirrors the report grade
  `report_multiport: rout=1/|ac_y[0].real|`) -> emit==grade. The OLD full-sweep I-V chord crossed the
  ~1.7V turn-off knee -> 29-37% IVrms baked into the .va vs the graded 0.3-1.2%. Fallback when no AC: a
  knee-AGNOSTIC saturation-region slope (|I|>=0.5*Iplat), not the full sweep. Lock test
  `harness/test_sink_rout_emit_grade.py` (emit==grade, knee-agnostic fallback). 66 sink tests green.
- **B1 BAKE emit DONE** (`harness/emit_pmu_model.py`): every FITTED `parameter real` is now `localparam`
  (vreg/Cft/SRa/Lreg/Rreg/Cs/Rs/Imax/Vcl/Gcl/idc55) -> a re-emit ALWAYS takes effect (no stale CDF
  instance override can shadow the new cellview default = the "slew rate still shows my old 6000" bug).
  The ONLY CDF `parameter`s left: `iload_<rail>` (the load OP the user sweeps) + `<rail>_en_ls` (the
  LTI/large-signal A/B MODE switch -- not a fitted value). PLL & VCO now expose the SAME 2 CDF params.
  12 emit/slew/recovery tests re-blessed (parameter->localparam); 61 green.

## THE DECISIVE FINDINGS THIS SESSION (all local Spectre, cadence/wur_real_tb/ + scratchpad)
Local replay loop re-confirmed faithful: compensated.va=2.47mV, baseline_noslew(La24)=26.58mV vs real_V.

1. **The slew is NOT the recovery mechanism.** Pure LTI (en_ls=0, slew->resistor) with La=120uH + the
   recovery network (Lreg||Rreg + Cs/Rs) = **2.37mV** vs real_V (BETTER than the slew version's 2.47).
   The recovery SHAPE is a LINEAR-network effect. (`cadence/wur_real_tb/ldo_pll_lti_la120.va`.)

2. **BUT La=120uH is AC-INCONSISTENT.** Its Zout peaks at **558** (|Z|@10MHz) vs the measured small-signal
   AC Zout peak ~**160** (baseline La24 reproduces the AC sweep to 1.69dB). So the deep dip needs an
   effective Zout 3.5x the small-signal AC -> it is a genuine LARGE-SIGNAL (SS != LS) effect that CANNOT
   be fit from the small-signal AC Zout alone. (scratchpad/zout_ac.py.)

3. **An AC-CONSISTENT model + slew IS feasible and STAYS PHYSICAL.** Fitting {SRa,Lreg,Rreg,Cs,Rs} to
   real_V with **La FIXED=24uH (AC value)** and **Cs BOUNDED <=5pF** -> **5.79mV** with PHYSICAL params:
   Cs converged to **0.98pF** (NOT the 1nF fake-decap abuse), SRa=8.4e3, Lreg=26.7uH, Rreg=245, Rs=5713.
   => the two GUARDS (Cs<=physical, La=AC-fixed) PREVENT the overfit that made the earlier coverage.transient
   fit non-generalize (35.5mV held-out, Cs->1nF). (scratchpad/autofit_acconsistent.py.)

4. **Neither model "crashes" on a sustained step** (earlier claim was over-stated): on a 0.5->2mA/1ns
   sustained step both stay physical (dip ~492mV, no rail-cross), but the La120 model OVER-DIPS vs the
   box's shallower 640mV decap'd step -> the excitation-overfit signature.

### NET METHODOLOGY (the answer to "fit recovery from standard flow"):
- small-signal Zout (La/Rpl/Cout/ESR) <- the **AC Zout sweep** (standard; = today's baseline fit).
- large-signal dip + recovery (SRa + Lreg/Rreg/Cs/Rs) <- the **decap'd coverage.transient step**
  (standard, large-signal char), fit with the two GUARDS (Cs<=physical decap, La pinned to the AC value)
  so it stays physical and generalizes. Validate ONCE (held-out) vs real_V (~5-6mV is the proven floor at
  La=AC; the AC-inconsistent La120 reaches 2.4 but is not standard-flow-fittable).
- en_ls A/B: en_ls=0 = AC-consistent small-signal (AC/PSRR/noise/Zout-grade); en_ls=1 = large-signal
  transient (deployment). **TODO (B3-emit): GATE the recovery network by en_ls** so en_ls=0 is the true
  AC-consistent baseline (today Lreg/Rreg/Cs/Rs are always-on -> en_ls=0 still carries them; gating linear
  idt/ddt branches is numerically stiff -- gate via en_ls-scaled L/C effective values or a switched node).

## BUILD PLAN (next session; ultracode)
- **B4 (CORE, needs ONE box deliverable)** — productionize the guarded transient fit into fit_multiport:
  consume coverage.transient (decap'd, the cdecap feature) + AC La/Rpl + coverage.cdecap; fit
  {SRa,Lreg,Rreg,Cs,Rs} with Cs<=cdecap-scale + La pinned to the AC value; de-embed; emit. Replaces the
  hand-tuned `_fit_recovery` manifest override. Recipe PROVEN in scratchpad/autofit_acconsistent.py.
  NOTE: fit_multiport is today "Pure-Python; no simulator" -- the transient fit needs a Spectre-in-loop
  replay (or an analytic step-feature fit: dip-depth->SRa, recovery-tau->Lreg/Rreg). Decide which.
  **BOX DELIVERABLE NEEDED:** the DECAP'D coverage.transient TARGET waveform (the box's clean-step
  response WITH cdecap) -- re-export via the Report-tab `@trans` "export waveforms" checkbox (the prior
  decode lived in a now-gone scratchpad). Optional: the measured z_pll AC Zout curve to confirm peak~160.
- **B3-emit** — gate the recovery network by en_ls (see TODO above) so en_ls=0 is AC-consistent.
- **B5 VCO** — same guarded transient fit on the VCO's coverage.transient once B4 works on PLL.

## LOCAL VALIDATION LOOP (survives compact)
- `cadence/wur_real_tb/`: real_pmu_iload_PLL_2000.txt (real load I, replay INPUT), real_V_VDD0P8_PLL.txt
  (real silicon Vout, held-out TARGET only), ldo_pll_compensated.va (La120+recov+slew, 2.47mV),
  ldo_pll_lti_la120.va (en_ls=0, NO slew, 2.37mV -- the linear-recovery proof), ldo_pll_baseline_noslew.va
  (La24 AC-consistent, 26.6mV), replay_pll.py (replay/--dc/--step).
- `scratchpad/` (SESSION-TEMP, re-derive if gone): zout_ac.py (AC Zout + sustained-step probe),
  autofit_acconsistent.py (the guarded feasibility fit -> 5.79mV, the proven recipe).
- Local Spectre IS available (`cadence/spectre_run.py`, spectre_run.available()==True).

## VALIDATION / ACCEPTANCE
Standard flow "works" when a recovery model fit from the (decap'd) coverage.transient + AC Zout, with the
two guards, reproduces real_V (held-out) to ~5-6mV with PHYSICAL params. Then the method is calibrated and
applies to other LDOs with standard flow only.
