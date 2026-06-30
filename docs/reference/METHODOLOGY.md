# METHODOLOGY — the modeling method + acceptance gates (what works AND what was refuted)

> Durable archive. Each method carries a verdict: **WORKS / REJECTED / SUPERSEDED / OPEN**.
> Refuted methods are kept deliberately — they are as valuable as the working ones (they stop us re-trying dead ends).

## Core architecture

- **Linear 2-port LTI model = Zout(s) + PSRR(s) + shaped Norton noise + discrete spurs + a SEPARATE nonlinear DC/dropout table** — **WORKS (conditional).** The FORM is physically correct for disturbances ABOVE the loop UGB (loop open → vout = the open-loop passive output network). FORM holds within the declared envelope; the historical VALIDATION was in-sample/block-level only (see Validation below).
- **The irreducible kernel = ONE phase-accurate, OP-parameterized Zout(s)** — **WORKS (the central insight).** All four observables are the SAME Zout driven by different sources: PSRR = Y_couple·Zout·vin; noise Sv = |Zout|²·S_in; transient = IFT(Zout·i_load) (carries NO new info beyond Zout in the linear regime); spur = Zout·i_spur. Only DC/dropout is a genuinely separate nonlinear object. ⇒ a Zout error is COMMON-MODE across all four.
- **One emitted instance, layered (NOT split per use-case)** — **ADOPTED.** Deployment needs ONE `.va`/`.lib` (a system PSS/HB run presents Zout+PSRR+noise+spur simultaneously on one node — separate fits would let them disagree about the one physical Zout = unphysical). Characterization is already per-block. Form = one shared phase-first identifiability-gated kernel + per-use shells + a use-case-resolved scorecard.
- **Per-block swappable blocks + data-driven topology selectors** — **WORKS.** 4+2+2 blocks compose vs a 4×2×2 monolithic cross-product. Confirmed by an independent survey to be the field's recommended composite.
- **PVT = route A: characterize each PVT cell separately → fit separately → emit `.lib` SECTIONS** (tt_25c_1v05, ss_125c_0v95…), Cadence corner setup picks the section. **ADOPTED** (deliberately avoids a new overfitting surface). Cross-PVT interpolation **REJECTED**; continuous PTAT-ish i(T) is the only exception.

## Zout block

- **Multi-start bounded TRF + R_pl damping resistor + optional 2nd parallel R-L branch** (engaged only when it beats 1-branch by >40%) — **WORKS.** R_pl→∞ = resonant peak; finite R_pl = resistive plateau (one DOF generalizes peaked↔plateau). Branch B closes V3 (3.45→0.78 dB).
- **Higher-order (L‖R) ladder in series** (Ra + N×(L‖R) + Cout/ESR; magnitude-shape gate) — **WORKS** for a rising-then-plateau resistive shelf (the real WuR rails). Productionized through the in-situ pipeline, byte-identical when the `extra` list is absent.
- **Single inductor (La‖Rpl)+Cout to model a rising shelf** — **FAILED** (can't make rising-then-plateau → mislocates the corner ~70×, to 447 kHz).
- **Cout/ESR auto-extract from the capacitive band** (∠Z<−45°, C=−1/(ω·Im Z)) — **WORKS** within a few % over 100 pF–10 nF, EXCEPT high-ESR/cap-less parts (v1 381 vs 1000 pF; 30 Ω ESR floors the HF tail). Joint-LS Cout/ESR for those — **REJECTED** (underdetermined when ESR≫output-R, the cap is near-invisible → diverges; reverted to median/keep-best).
- **Unbounded LM + argmax-of-flat init** — **REJECTED** (diverged: cout10n R_a=32 MΩ; V1 92 dB spurious peak; v2 Cout → 1e269 F).
- **Passive-by-construction RLC** — **WORKS** (the #1 HB-convergence lever). It CANNOT reproduce a genuinely non-passive GT (v3 Re(Z)<0, ~−0.25 Ω floor); that residual is a documented FLOOR, not a bug. Non-passive controlled-source realization **REJECTED** (HB-stability risk).
- **Zout shelf fit = sign-agnostic SHAPE gate, magnitude-only, cap-open** — **WORKS.** The old "resonance mislocated / Q2000" diagnosis was WRONG (a metric artifact on a loop-active negative-real shelf). importmp passivity-normalizes Zout to Re≥0, so the gate must be shape-only (Q>3 + ipk≥0.6N + plateau>0.6peak), not Re-sign-dependent.

## PSRR block

- **Identify i_c = H_psrr/Zout by Sanathanan–Koerner rational fit (freq-scaled, relative-weighted); realize as a bank of signed first-order real-pole sections** — **WORKS.** The earlier claim "PSRR doesn't factor as i_c·Zout" was WRONG/REFUTED — it was a fit-method failure (shelf insufficient order; naive fit won't converge over 60 dB), not structural.
- **One signed complex-conjugate 2nd-order section, AAA-initialized + least_squares-polished on the realizable form** — **WORKS** for non-min-phase parts (v4 mag to 0.04–0.06 dB; 25° residual phase remains). N2=1 is the sweet spot. Keep a 1-section shelf if min-phase fits (no regression on 9 variants); the 3-real bank polish must be UNGATED (was wrongly `if CFT>0`).
- **Raw AAA poles dumped into the model** — **REJECTED** (over-fits: 3–6 spurious pairs, Q~1700). AAA is an INITIALIZER only.
- **Ranking a real PSRR notch fit by analytic residual** — **REJECTED** (low analytic residual but huge realized phase error from fragile pole-zero cancellation). Always realize + score, never rank by analytic residual.
- **PSRR is NOT non-minimum-phase by default** — the Hilbert "non-min-phase" reading was an artifact of sparse silicon AC points.
- **AAA complex-section initializer on sparse silicon AC** — **FAILED / OPEN** — the remaining WuR **pll PSRR blocker** (resid 3.16 > shelf 1.06, ~47-pt sweep, phase RMS ~51°). Needs a robust complex-section initializer for sparse real AC.
- **Two separated PSRR notches in a single-loop integrated LDO** — **REJECTED (physics).** Two feedforward paths merge into one effective notch; an on-chip LC trap at 10s of MHz needs unphysical µH. The single-complex-pair block matches single-loop physics; exceeding it needs a cascade/multi-loop topology.

## Noise block

- **Decoupled Norton-@vout: In = Sv_meas/|Zout_model|, In² = white + 6 Lorentzians fit in the LOG domain jointly over 3 corners (shared corner freqs, per-corner amplitudes); realize as white-R + 6 R‖C VCCS sections** — **WORKS.**
- **log-AMPLITUDE noise fit** — **WORKS**; replaced linear-LS-in-power (LF flicker inflated the white floor 5–30×; real flicker af~0.85 sub-1/f).
- **CAVEAT — "decoupled" is a MISLABEL, not a defect.** It's an algebraic round-trip (In=Sv/|Z| → Sv=In·|Z|) that decouples SYNTHESIS, not PHYSICS — a Zout error leaks identically into noise. DO NOT "fix" the round-trip (the physics is correct); relabel it and score Sv vs GT end-to-end.
- **Series-voltage noise in branch A riding the Cout divider** — **REJECTED** (base-specific + coupled to Zout synthesis; adding branch B for V3 perturbed noise 5.6→21.8 dB). EXCEPT: a **HYBRID series-voltage bank WORKS for loop-shaped rails** (Norton can't hold In=Sv/|Zout| for the pll/vco ~11 dB rails; synthetic DUTs stay Norton).
- **`flicker_noise()` / `noise_table()` for the 1/f tail** — **REJECTED** (no byte-matching ngspice analog → forfeits the 9e-6 cross-engine lock; the free Lorentzian bank is equal-or-better).
- **Spur sampling rule** — **LOCKED.** `.noise`/AC sweeps MUST sample the spur center + very-short-step points (0.05…4×HWHM, Q-aware) around it, else the narrow spur is stepped over (54× miss measured).

## Spur block

- **Deterministic vout current tones `Isp 0 vout`, I_k = vout_amp_k/|Zout_model(f_k)|, characterized by transient-FFT (NOT `.noise`)** — **WORKS (0.00 dB).** Split detected lines into fundamentals vs IM/harmonic products (the linear model emits only fundamentals; the user's nonlinearity regenerates IM). PSS/HB manifest: GCD-fold commensurate tones vs declare separate incommensurate fundamentals. External supply/bias spurs are NOT emitted — they ride the PSRR path.

## Load dependence

- **Parameter scheduling: each param a clamped quadratic in ln(iload)** — **WORKS as the default; FAILS at the edge.** The 3-corner, 0-residual-DOF quadratic is the PROVEN OVERFIT LOCUS (in-corner error ≡ 0, carries no generalization info; held-out 20–30×; flat/frozen outside [20µ,250µ] via clamp). Band-aids (_pexpr clamp, MIN_LOG_GAP, branch-B 40% gate) censor the symptom, not the correctness — they are local heuristics, not AIC/BIC.

## Large-signal / recovery (the WuR PLL thread)

In the order it evolved — most are RETIRED; read top-to-bottom to see why:
- **Hand-tuned `la_override` + Lreg‖Rreg recovery network (La=120 µH)** — **REJECTED** (a crutch; a real user can't know La/Lreg; must be fitted). AC-inconsistent (La120 |Z|@10MHz=558 Ω vs measured ~160).
- **"Drop slew, go pure LTI with the deployment decap"** — **SUPERSEDED/RETRACTED.**
- **Fit La + recovery from AC Zout alone** — **SUPERSEDED** (a single inductor can't make the rising shelf; AC gives 24 µH, the transient seemed to want ~120 µH = the small-signal-vs-large-signal gap).
- **slew + recovery network (compensated.va) under decap** — **REJECTED** (crashes to the 300 mV anti-windup clamp, RMS 92–105 mV).
- **Add a branch-A slew term for the startup undershoot** — **REJECTED** (2nd expert panel, unanimous: WRONG SIGN — the coverage dip is minimized at SRa=∞; a positive SRa makes the already-over-predicted dip catastrophically worse, 245→1047→4456 mV).
- **Fit anything to real_V** — **REJECTED** (overfit 2.47→35.5 mV held-out; real_V is a turn-on/cold-start ENVELOPE, corr(I,V)≈0, NOT a load transient → reference-only, never a gate).
- **Higher-order ladder Zout** (above) — **WORKS** — is the load-transient fix from the standard flow.
- **User TB-side fix: redesign the output iload as a STEP 600µA→300µA** (bakes steady loading into the DC-OP → pre-charges the fit-inductor → no phantom upward slew) — **WORKS** (resolved the startup drop, user-scoped). Caveat: stimulus-side fix; the big-inductor IC fragility (droop depth set by the IC, not physics) remains — record the 600µA choice.
- **Compressive branch-A current-assist `i_assist = iaG·tanh(verr·|verr|/iaV²)`** (verr=vreg−vout; ODD, f'(0)=0 EXACT → AC/PSRR/noise bit-identical at OP — verified 0.004 dB; saturates at iaG = class-AB loop stiffening; 2 params fit Spectre-in-loop, held-out across amplitudes) — **WORKS / SHIPPED (PART2)** — the fix for the GT sub-linear stiffening dip; replaces the LINEAR over-prediction (163.6 mV/mA) with the SUB-linear silicon shape. Fitted PLL (iaG=2.8 mA, iaV=0.33 V) → dips 154/224/290 mV vs GT 160/227/282, held-out middle −3.7%, full-waveform GT-RMS 10–16 mV (was 24–69); VCO (iaG=4.0 mA, iaV=0.22 V) → 74/103/129 vs GT 74.7/103.4/128.7 (rms 0.9 mV). BAKED localparams (INTERNAL physics → SURVIVES minimal-emit; minimal minimizes the SCHEMATIC param set, not the model — adds NO CDF param). Manifest override `m['v_out'][rail]['iassist']={iaG,iaV[,floor,gfloor]}` → `_fit_iassist` → emit (mirrors slew_a/recovery escape-hatch; absent → byte-identical). Optional `floor` = a DEEP out-of-envelope backstop (one-sided clamp below `floor` V, zero value+slope above → invisible in the validated regime) so a system sim never sees a negative rail far past VALID_LOAD where the saturated assist can't hold — the assist is the CURE, the floor is a SEATBELT (NOT the rejected "crashes-to-clamp" primary mechanism). Residual GT-RMS conflates genuine compression with the documented T-confound (AC z_pll=tt_25c, step GT=tt_55c, ~×0.65) — a T55 z re-export would clean the absolute gain. **STANDARD-FLOW DERIVATION — PURE-PYTHON, NO SIMULATOR** (`harness/fit_iassist.py`, folded INTO `fit_multiport`): the model's load-step dip is NOT re-simulated — it is a small KNOWN linear RLC network (branch-A ladder + external decap) with one nonlinear feedback (the assist), so `predict_dip` solves it as an ODE (LSODA, ~2 ms/solve, validated <1.6 mV vs Spectre on both rails). `derive_iassist` reads the coverage.transient GT dips already in the npz (`tr_*`, from the FIRST ALPS/Donau run) and `fit_rail` solves iaG/iaV against them (grid+refine, minimal-intervention tie-break, held-out) — at the first fit, no second simulation, runs on box/GUI/CI. Re-derives PLL ≈4.6 mA/0.44 V (−1.2% held-out), VCO ≈7.2 mA/0.31 V (0.0%) with the seed hidden; whole derive ≈5 s. The manifest `iassist` is a SEED used only when the npz has no `tr_*` (legacy/single-OP). (The earlier Spectre-in-loop version was REMOVED — company policy forbids local Spectre; only ALPS/Donau or pure-Python.) The 2 params are **partly degenerate** (a valley matches the same dips → gate on dip-RMS+held-out, not the values). Diagnostic FACT: the negative-trans is purely LOAD-driven — supply (0.85–1.15 V) and temp (−40–125 °C) move it 0 mV; higher vreg is SAFER (min=vreg−dip); "FF"/"VDD>0.8" only correlate via the real load drawing more current.

## Current-output behavioral model

- **Object = MOS-transistor-level GT, deliverable = behavioral; anchored-OP + 2-point gate fit, no optimizer** — **WORKS** (1 template → all 8 GT current sources).
- **Sink rout from the AC-admittance DC real part**, NOT the full-sweep I-V chord — **WORKS** (the chord crosses the turn-off knee → 225× too steep → 36% IVrms; emit==grade after the fix).
- **Data-driven I-V knee detect {lo,hi,none} + keep-best vs 'none'** — **WORKS** (real refs are SINKS with a HIGH-Vo compliance ceiling, not a low-Vo knee).
- **|Y| second-order zero: Y = g0·(1+s/wz)/(1+s/wp) + sCp** (passive series Cz-Rz internal node; opt-in keep-best) — **WORKS** (g0+sCp missed the cascode/Wilson zero; i500n 1.93→0.09 dB).
- **Collapse PSRR to |PI(0)| magnitude** — **REJECTED** (loses sign/phase; matters when sinks share VREF and ripple currents superpose).
- **Verify-first coupling (G9):** inject on one sink/shared bias, build the n² cross-admittance ONLY if confirmed — **METHOD/locked** (don't pay n² blindly on a shared-bias prior).

## Validation & gates

- **Composite byte-identical invariant** (model passes through every corner exactly → score evaluated only at corners → any between/beyond-corner change is composite-safe; new CLI flags go in `__main__` with lazy imports; `run_matrix` calls `score()` directly) — **WORKS** (the safety net for every "safe increment").
- **Keep-best opt-in DOF gating** (adopt a new DOF only if it beats baseline by a margin, else byte-identical fallback — d2, Cft, |Y|-zero, ladder, hybrid noise) — **WORKS** (anti-overfit).
- **Out-of-sample guardrails** (LOCO leave-one-corner-out, off-grid through the emitted `.lib`, identifiability cond(J)/σ) — **WORKS as a detector.** Confirmed broad in-sample overfit; the LOCO interp gap is a fundamental few-corner limit, NOT fixable at the fitter layer (needs more corners or physically-constrained per-corner params).
- **Adversarial overfit probe (8 fair GT DUTs) + 5 new observational gates** (heldout_idc, y_rms_db, psrr_offvc, model_iv×TEMPS, large-signal load-step) — **WORKS** (8/8 exposed; META-FINDING: 4/8 are BLIND to the in-sample composite, only the new gates catch them). Kept OBSERVATIONAL, not folded into the verdict.
- **System acceptance test (LDO + buffer @ carrier, coherent FFT)** — **WORKS.** KEY PHYSICS: an LTI LDO cannot self-make a sideband → the BUFFER must mix (a vout-dependent B-source). Surfaced v4 as the dominant deliverable-level failure that block-metrics masked.
- **Fixed-vrip system stimulus** — **SUPERSEDED** by small-signal auto-calibration + an empirical GT-linearity gate (vrip=0 complex-subtraction isolates intrinsic spurs). Fix the TEST, not the fit, when the GT is over-driven.
- **Validation independence was historically NONE** (report.py predict() reuses the fitter's transfer functions; _selftest is a tautology; score.py re-sims via ngspice but only on the fitted loads/freqs) — this is the standing **OPEN** methodological gap; the LOCO/off-grid/system-test gates above were built to close it.
- **One multitone .tran replacing 2 AC sweeps (trans-ID)** — **WORKS for Zout** (validated + productionized as compiled VA stimulus + importer + GUI tab). PSRR on multi-pole parts is an information/conditioning limit (NOT a fixable fitter bug — R7 negative). Band-split is REQUIRED; more tones is NOT the fix.
- **fit_psrr/fit_zout multi-start regrid for coarse trans grids** — **REJECTED/REVERTED** (helps the dev path but regresses the production VA path via a realization-dependent selector flip; no selector may peek at AC). The fix is RECIPE-side (denser tones / a cheap AC anchor).

## Orchestration / extraction

- **Capture-and-augment, not reconstruct** (tag the designer-TB pin roles, append ONLY extraction stimuli, never rebuild the OP) — **WORKS** (foundational).
- **Unified source-reuse** (v_out/i_out REUSE the TB's own idc/vdc, set mag=acm in place) — **WORKS**; SUPERSEDES the old "append Iext_/Vprobe_" insert behavior.
- **npz contract firewall + pure-Python fit** (raw V / probe:p saved, all ratios computed in Python) — **WORKS** (the project's central invariant; firewall gate reproduces the stand-in npz to 0.00e+00).
- **Mechanism A = ADE-native in-situ extraction** (manifest-driven, drive ADE-XL via `axlRunAllTests`, inherits Maestro Job-Setup → cluster; only append stimuli at tagged DUT pins) — **WORKS** end-to-end live.
- **In-situ = AC superposition: one sim per measurement GROUP, exactly ONE `acm_*`=1 (one-hot), only that group's analysis enabled; psf_map keyed BY MEASUREMENT TAG never by corner** — **WORKS.** Voltage rails in-situ (AC-only source dc=0, the TB's own load biases the rail). Current outputs port-isolated (a probe voltage source applies compliance DC + injects AC; needs per-i_out vdc = the real operating node voltage; if omitted, validate() defaults dc=0 and warns).
- **Pure-CLI dsub+ALPS path (Path B)** over ADE/skillbridge — **WORKS** (classic PSF byte-identical to ADE). Offline (no-ADE) text-augmenter `group_netlister` — **WORKS** (plugs into the injectable seam; the ADE `insituNetlistTest` stub is kept as a documented fallback).
- **Air-gap digest iteration loop** (red-zone GUI report → paste a `[MPD1]`/`[MPD1-GZ]` self-contained digest → local `digest_import.py` rebuild → iterate → ship ONE incremental package) — **WORKS** (THE red-zone workflow; gzip+base64 7.6× shrink). Carry the box's fitted voltage model in the digest (off-box refit of ill-conditioned capless rails diverges; densifying does NOT fix).
- **Local Spectre full-pipeline + desk parse-gate** (offline netlister → spectre_run → importmp → fit → emit; run every emitted card shape through local Spectre at the desk) — **WORKS** (catches oprobe-class syntax bugs before the box).
- **Validate against an INDEPENDENT transistor GT, never model-vs-itself** — **RULE** (caught a self-fulfilling supply-noise check; honest number 1–3%/12%, not the fake 0.02%).
- **GUI = thin shell over a Qt-free core + headless `--selftest`; analytic-only `predict(P,f)` overlay (the GUI NEVER calls a simulator)** — **WORKS.**
- **Minimal emit** (bake all fitted params at the OP → `localparam`; expose only `vreg_<role>` per rail) — **SHIPPED.** Baking kills the stale-CDF-instance-override bug ("still shows old 6000"); only `iload_<rail>` + `en_ls` stay CDF.
- **Synthetic FLAT DC stand-in (`export_single_port_refs`)** — **REJECTED/deleted** (fabricated dropout/load-reg with no flag at the model boundary).

## Refuted — do NOT re-attempt

- Two-notch PSRR in a single-loop integrated LDO (physics).
- Strong negative-Re Zout in a passive RLC (representational floor ~−0.25 Ω).
- A clean blind-spot B1 (collinear CTAT+PTAT, bowed interior, clean I-V) in this CTAT-Vth BSIM3 PDK (a hard 3-way tension; documented FALLBACK, monotonic-convex isrc kept).
- A fitter-layer fix for multi-pole PSRR grid sensitivity (R7 negative — if a fit looks PSRR-limited, the fix is recipe-side tone/grid placement).
- Joint-LS Cout/ESR for high-ESR/cap-less parts (underdetermined).
- The branch-A slew term for the startup undershoot (wrong sign).
- Fitting to real_V (it's a turn-on envelope, overfits).
