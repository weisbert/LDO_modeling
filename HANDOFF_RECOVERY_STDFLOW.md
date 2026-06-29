# HANDOFF — model the LDO large-signal recovery FROM THE STANDARD FLOW (not real_V)

Single source of truth for the next session. Supersedes HANDOFF_EMIT_BAKE_AND_VCO_RECOVERY.md (Part 0)
and RETRACTS HANDOFF_DECAP_LTI_RECOVERY.md (its "drop slew, go LTI" conclusion was wrong).

## GOAL (user, verbatim intent)
Recovery / large-signal params (La, slew SRa, the recovery network) must be MODELED FROM THE STANDARD
PMU FLOW (coverage.transient load steps + AC Zout) and the resulting model must match the real silicon
LDO output. NOT hand-tuned. NOT fit to `real_V` (the real system-TB output is not a standard-flow
deliverable; fitting to it = curve-fitting the answer, needs the real system per LDO).

## SHIPPED THIS SESSION (all on main, pushed; `bash apply` on the box to deploy)
- `0deaf5a` report digest carries the transient waveform `@trans` + Report-tab "export waveforms"
  per-family checkboxes (debug_report `include=`). Lets a pasted report rebuild the transient locally.
- `d8d8a32` `coverage.cdecap` (global) + per-rail `coverage.transient.<o>.cdecap` → emits an output
  decap on the TRANSIENT char only (AC/noise/I-V stay intrinsic). REAL_wur_pmu_top.json has cdecap=20pF.
  Proven on real data: PLL coverage.transient dip 310mV(bare)→160mV(20pF).
- `63452e1` GUI: removed the user-editable recovery/La fields from the trans editor (they are FITTED, not
  hand-typed; _la/_recov stores keep manifest values losslessly) + status-tree functional sort
  (load-major, temp-asc) + column 0 hugs content.

## THE KEY FINDING (negative result, all local Spectre, scratchpad/*.py + cadence/wur_real_tb/)
On the REAL GHz loading current (real_pmu_iload_PLL_2000.txt) vs real_V (silicon, 20pF):
- plain LTI (AC La24/Rpl160) = 26.6mV (dip 765 too shallow vs real 712) — slew IS needed.
- compensated (slew+La120+recovery, hand-tuned) = 2.47mV — matches.
- optimizer fitting {La,SRa,Lreg,Rreg,Cs,Rs} DIRECTLY to real_V from a GENERIC start → 2.02mV (La166u,
  SRa1e5, Lreg15.6u, Rreg496, Cs0.87p, Rs3010). Proves auto-fit works — BUT real_V is non-standard.
- optimizer fitting to the STANDARD coverage.transient (decap'd tr_pll_2m, 1ns edge) → in-sample 12.8mV
  but it ABUSES Cs→1nF (snubber as fake decap); HELD-OUT on real_V = **35.5mV** (WORSE than plain LTI).
  => the standard coverage.transient as configured does NOT generalize.

WHY: (1) regime mismatch — coverage.transient is 0.5→2mA/1ns single step (dip 160mV); the deployment is
0↔0.8mA GHz continuous (dip 88mV). (2) MODEL STRUCTURE — en_ls=1 makes the reg branch FULLY slew-limited
(slew REPLACES the fast linear Ra path), so the model can't deliver current on a fast step → vout crashes
to the 300mV clamp → the optimizer can only fit by going non-physical. The real LDO has a FAST linear loop
AND a slew limit (shallow-then-slow).

## BUILD PLAN (next session; ultracode)
Priority order. B1/B2 are independent & immediately buildable; B3 is the core; B4 needs one user input.

B1 — BAKE + REORG emit (the user's original ask, still not done). Convert every FITTED `parameter real`
   in emit_pmu_model.py to `localparam real` (vreg/Cft/SRa/en_ls/Lreg/Rreg/Cs/Rs/Imax/Vcl/Gcl/idc55),
   keep `iload_<rail>` a parameter (it's an input). Group decls by rail+function with section comments.
   Re-bless the byte/param tests. (Detail: HANDOFF_EMIT_BAKE_AND_VCO_RECOVERY.md Part A.) After re-emit
   the box runs `ahdlUpdateViewInfo` to drop the stale CDF params (skartistref.pdf p.600).

B2 — SINK g0-source bug (long-pending; now trips all 3 sinks). emit derives sink rout from the FULL-sweep
   I-V chord (crosses the ~1.7V turn-off knee → ~225× too steep → 29-37% IVrms in the .va); the report
   GRADE re-fits rout from AC-admittance DC real part → 0.3-1.2%. FIX: fit_multiport._fit_one_current_sink
   derive rout from `cp['y']` DC real part (mirror report_multiport) else a POST-knee chord. Lock test:
   emit-path IVrms ≈ grade-path IVrms. See [[insitu-sink-g0-source-bug]].

B3 — MODEL STRUCTURE: fast linear path + slew COEXIST (not replace). Today slew(V/Ra) replaces the linear
   reg current → no fast path. Design a structure where the small-signal Zout (La||Rpl+Ra, fast) is
   preserved and the slew rate-LIMITS the large-signal current ramp (e.g. slew applied to the loop's
   target current, or a parallel fast-linear + slew-limited pass). Constraints: DC-convergent (the prior
   high-gain-integrator attempts failed DC — drive slew from a resistive target, see memory), passive,
   AC bit-identical when slew off, byte-identical default. Validate: replay vs real_V reaches ~2-3mV with
   PHYSICAL params (no Cs=1nF). This is a design-panel job (try several topologies, score on the replay).

B4 — REPRESENTATIVE char + fit recovery from STANDARD FLOW. The real load di/dt is DERIVED (no user
   input needed) from real_pmu_iload_PLL_2000.txt: mean ~297uA, peaks ~1.96mA (NDIV), p2p ~2mA, raw
   per-edge |di/dt| up to 6.6e7 A/s, envelope(10ns) ~6.7e5 A/s; 0-25ns trend ~flat (~265uA) -> the 25ns
   dip is driven by the SWITCHING, not a slow ramp. KEY: the real load is TRANSIENT SWITCHING (mean 300uA,
   snaps back each cycle), NOT a sustained step -- so a single clean coverage.transient step (0.5->2mA
   HELD) is the WRONG excitation (the model crashes on the SUSTAINED 1.5mA demand, which the switching
   load never imposes). => a fixed-step "representative" char doesn't exist; if B3 alone doesn't
   generalize, the char must MIMIC THE LOAD PROFILE (mean+switching+peaks) or use the real load current
   itself as the char stimulus (a current, derivable from the load circuit -- arguably more "standard"
   than real_V). Then auto-fit La/Rpl(from AC Zout) + SRa/recovery(from that char) -- productionize
   scratchpad/autofit.py into fit_multiport (consume the char + coverage.cdecap; de-embed; emit). VALIDATE
   once (held-out) vs real_V on this LDO to CALIBRATE the std flow; thereafter std-flow-only per LDO.
   PRIORITY NOTE: B3 (excitation-independent structure) is the real lever; do it first, then re-test
   whether a clean char generalizes before investing in profile-mimicking char.

B5 — VCO recovery: once B3/B4 work on PLL, VCO is the same fit on its coverage.transient (no special case).

## LOCAL VALIDATION LOOP (survives compact)
- cadence/wur_real_tb/: real_pmu_iload_PLL_2000.txt (real load I), real_V_VDD0P8_PLL.txt (real silicon
  Vout, the held-out validation target ONLY), ldo_pll_compensated.va / baseline_noslew.va, replay_pll.py.
- scratchpad/: autofit.py (optimizer vs real_V, 2.02mV), autofit_cov.py (vs coverage.transient, 35.5mV
  held-out), tr_pll_decap.py (the decap'd coverage.transient tr_2m), cov_*.py (the LTI exploration),
  plain_lti_vs_real.png / autofit_vs_real.png (the overlays). NOTE scratchpad is session-temp — re-derive
  if gone; the wur_real_tb/ files are committed.
- Local Spectre IS available (spectre_run.available()); the whole replay-fit loop runs at the desk.

## VALIDATION / ACCEPTANCE
The standard flow "works" when: a recovery model fit from the (representative) coverage.transient + AC
Zout reproduces real_V (held-out) to ≲ ~5mV with PHYSICAL params. Then the method is calibrated and
applies to other LDOs with standard flow only.
