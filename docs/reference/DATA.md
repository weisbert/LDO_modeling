# DATA — measured numbers, acceptance thresholds, golden values

> Durable archive. Curate-only — NO status, NO changelog. Not loaded by default; read on need.
> Provenance tags point at the (now archived) source docs in `docs/archive/`.

## 1. Target-A synthetic LDO (methodology-proof DUT)

Operating-point table — **strong load dependence**, resonance MIGRATES with load:

| load | Zout LF | Zout peak | peak f | PSRR LF | PSRR worst |
|---|---|---|---|---|---|
| 20 µA | 85.2 Ω | 379 Ω | 0.944 MHz | 51.1 dB | 16.1 dB |
| 121 µA | 23.3 Ω | 388 Ω | 1.778 MHz | 44.3 dB | 3.7 dB |
| 250 µA | 18.3 Ω | 342 Ω | 2.113 MHz | 36.7 dB | 1.1 dB |

- DC/design: Vout≈898 mV @121µA; load-reg 30 mV/mA; line-reg 11 mV/V; Cout=1 nF on-chip+ESR; cg=1 p; loop UGB 1.78 MHz; vin 1.05 V; carrier ~304 MHz; Vth0≈±0.30, Tox=3.5 n; flicker kf=4e-29, noimod=1 → ~9 kHz noise corner.
- GT topology: `Zout(s)=(R_a+sL_a)||(ESR+1/sCout)`, resonance @ `1/2π√(L_a·Cout)`. LOADS=[20u,121u,250u], nominal 121µA. PSRR ref node 1.05 V (Vrf). gt_ref.npz = 23 arrays (z/p/noise/trans/dc/dropout/ibp/hf).
- **Linearity ("spur band is linear"):** 8 MHz tone driven to 500 µA extreme swing → fundamental scales perfectly linear, Zout(8M)=20.9 Ω const; 16 MHz −62 dBc (worst nonlinear floor), 24 MHz −94 dBc. 8/16/24 MHz are ABOVE the 1.78 MHz UGB (loop open → linear Cout dominates). Edge-vs-LTI err: <1% to ~10µA, <5% to ~50µA, ~18% off @1mA (gm compression, edge-rate-independent), dropout/collapse @5mA.
- Noise: white floor ~115 nV/√Hz + resonance peak ~4.4× @121µA + Cout rolloff; integrated 207/412/436 µVrms @20/121/250µA.
- **Acceptance targets:** Zout@8/16/24M |err|<0.5 dB; peak f ±5%, mag ±2 dB; broadband RMS<2 dB. PSRR@band |err|<2 dB; floor ±2 dB; notch f ±20%. Healthy composite ~2–4 no gate FAIL; stub baseline ≈403.
- Achieved Target-A grade: composite **3.8**; Zout band 0.02–0.04 dB / peak <1 dB / phase <1°; PSRR band 0.1 dB; noise PSD ~1 dB / peak −0.7 dB / integrated −3%; transient-lin droop <0.3%; 5 mA dropout wrms 1%, 1 mA wrms 13%, 1 mA step initial droop spike +18% (open gm-expansion item).
- **Composite weights** (score.py): zband=3.0, pband=2.0, zphase=0.04, pphase=0.03 (phase de-weighted). Resonance peak searched fz<1e7 only. Re-baseline added spurph W=0.03/deg, zhf/phf W=0.5 each.

## 2. Generalization variant matrix (composite, lower=better)

After fitter upgrades: base 3.7 · cout10n 3.0 · cout4n7 2.4 · esr_hi 3.8 · iq_lo 5.4 · iq_hi 4.2 · wp_big 7.7 · cg_hi 6.4 · v1_nmos 13.5 (was 439) · v2_capless 9.9 (was 164) · v3_miller 18.2 (was 116) · v4_ffpsrr 5.5 (was 33).

- Cout fit/true (pF): base 997/1000; cout10n 9704/10000; cout4n7 4622/4700; v1_nmos 381/1000 (30 Ω ESR floors HF tail); v2_capless 122/100. spur16 ≤ −145 dBc every variant.
- Per-block: PSRR v4 33→5.5, band 6.7→0.04 dB. Noise npsd V1/V2/V3/V4 9.6/8.4/21.8/3.9 → 1.0/2.1/2.1/3.6 dB. Spur 0.00 dB amp / ~1e-5 rad phase, 0 missed/0 false. PSRR-phase v4 25→1°, v3 10→2°, v1 6→3°, v2 3→2°. N2=1 is the PSRR complex-section sweet spot (N2≥2 overfits — v3@20µ 107°). Shelf short-circuit gate: e_shelf<0.05 AND shelf-phase<2.5° (SHELF_PH_TRIG=2.5°).
- **Round-2 v7–v10** (base=3.9): v7_esl 4.2 (SILENT in-band; systest@1.2GHz FAIL −18.8 dB, |Z|7.5Ω@2GHz, SRF~200MHz); v8_dlc 5.6 (anti-res notch 0.05Ω@112MHz =21.6 dB miss, >100MHz cap blinds composite); v9_vldo 1.7 (BEST; ~50 mV headroom; 1mA step→dropout 108 mV GT vs 136 mV model over-droop; knee ~1mA); v10_3lc 57.2 (15×; systest@600MHz FAIL −20 dB; GT|Z|14.3Ω@42M/43Ω@211M vs ~0.4/~0.16 model; fitter saw 211MHz res in NOISE PSD as 216MHz Q15 but Zout topo can't place; autoextract latched 10nF bulk vs 200pF on-die). Re-baseline deltas: v7 4.2→16.7, v8 5.6→18.1, v10 57→71, v1 8.9→10.6.
- **Cross-sim Spectre/ngspice** (12-arch): base 3.8/3.9, cout10n 2.4/2.7, cout4n7 2.6/2.7, esr_hi 4.0/4.1, iq_lo 4.95/5.0, iq_hi 4.06/4.1, wp_big 7.66/7.8, cg_hi 6.33/6.3, v2_capless 6.51/6.8, v4_ffpsrr 3.9/4.0, v1_nmos 9.23/8.9, v3_miller 20.6/6.3 (fitter bifurcation, not pipeline bug). Zout ΔRMS ≤0.006 dB; 1/f noise band Δ0.000 dB; 304 MHz Zout 0.724 vs 0.724.
- Task-3 Zout passivity: v3 GT NON-passive min Re(Zgt)=−0.23 Ω (only non-passive of 14). Residual floors: v3 zrms1.14/zband0.78/pkf5.01; v1 zrms1.94/zband1.47 [ESR=30]; v2 1.20/0.95 [small Cout]. Underdetermined Cout: unbounded LS → v2 1e269 F (or 1e269); bounded+keep-best 1 pF; v1 fit 381 pF for 1 nF.

## 3. Overfit / LOCO cross-validation

- In-sample: Zout 0.1–1.6 dB / PSRR 0.07–0.27 dB. Held-out (LOCO): Zout 1.6–13.0 dB (v1@20µ=13.0) / PSRR 1.8–8.6 dB (v4=7.46, v3=1.80). **10×–100× gap CONFIRMED.** DO NOT cite the old 66.2/33.2 dB (didn't reproduce); cite 7–13 dB Zout / 1.8–8.6 dB PSRR.
- Zout cond(J): base/v1/v2 = ∞ (σ_min=0, R_pl/R_b pinned 1e9); v3 2.7e8; v4 46 (only well-conditioned). R_pl is a SWITCH: v1 R_pl 7.8e6→54.5→53.7 across corners; v3 pcw0 [60.4, 8.23e7, 8.33e7]. _pexpr clamp [min/1.5, max×1.5] escaped by v3 pcw0→1.245e8. R2 clamp CLAMP_M=1.005 (log)/CLAMP_ADD=0.005-of-span: off-grid PSRR@174µ v3 21.70→10.11 dB; LOCO v4 63.89→8.04, v3 39.26→11.11. Off-grid test loads 49µ/174µ (geometric mids). crossval structure LOCO: 17/19 STABLE, 2 FAIL = v1_nmos + v2_capless (PSRR shelf↔complex flip near 2.5° trigger).

## 4. Adversarial overfit probe (8 fair GT DUTs, 8/8 exposed)

Corners 20µ/121µ/250µ, nominal 121µ, asymmetric in ln (gaps 1.80 vs 0.73). Generalization ceilings the fixes must hold: IVrms ≤3.82% · |Y| ≤7.16 dB · cPSRR ≤25.91 dB · Nrms ≤9.40 dB · ivR2 ≥0.93.

- A1 qbow: composite 18.96 (pkdb 15.8 dB), LOCO+OFFGRID+STRUCT FAIL; |Z|peak ~1.2/9/1.5 Ω @3–6 MHz (Cd≈30p, Rd≈2k, Q≈8–12@121µ, Q121≈45 vs bringup Q≤30 → jointly infeasible).
- A2 pzmig: composite 5.03; PSRR LOCO 0.22→4.30 dB (19.5×); corner 0.4/3.0/3.3 MHz, linear interp places √(0.4·3.3)≈1.15 MHz vs true ~3 MHz.
- A3 swbleed: composite 5.15; LOCO Zout 0.31→1.10 (3.5×), PSRR 0.18→5.50 (30×); R_pl switch 5.15k@20µ→off; branchB {20µ off,121µ off,250µ on}; falsifier = composite≤6 yet structloco FAIL.
- A4 classab: composite **1.62 < base 3.95** — ALL small-signal gates clean; only a4_verdict catches (slew wrms 30%, GT asym 0.35 vs model 0.12). The DEEPEST blind spot. Accept wrms ≤15% AND asym ≤20%.
- B1 inflect: idc 16%, ptat 0.158; gate = 15.0% interior miss (25/85°C). TEMP_QUAD_MIN_PTS=5, TEMP_QUAD_MIN_GAIN=0.10. Clean-blind B1 (collinear CTAT+PTAT) NOT achievable in this CTAT-Vth BSIM3 PDK.
- B2 double_cascode: scalar PASS clean (rout 2.1%); y_rms gate = 2 zero-steps 2.09 dB (zeros 1.2e5 & 1.3e7 Hz). Y_PZ_KEEP_DB=0.5, Y_PZ_MIN_SEP=1.05; accept y_rms_db>1.0 while rout_err<0.20.
- B3 bias_flip: in-vc PSRR sign ok; psrr_offvc gate = sign flip +8340→−1490 nS across compliance (@vc±0.2).
- B4 tempload: ptat_err 0.000 passes; iv_temps gate = 47 mV compliance-knee shift with T; accept >5% RMS over plateau. **BONUS: baseline v8_wilson trips B4 at 139 mV knee shift** (pre-existing undetected gap).
- No-regression: crossval_isrc 8/8; base re-scores 3.952783 byte-neutral; pytest 145.

## 5. Real WuR_PMU silicon (the deployment chip)

- Topology: 2 V-rails VDD0P8_PLL / VDD0P8_VCO + 3 current sinks IBP_POLY_500N_LPF / IBP_POLY_3P6U_VCO / IBP_PTAT_TUNE_1P5U_VCO; supply AVDD1P0 = 1.0 V; rails regulate ~0.8 V (200 mV headroom, capless). Modeled to composite **1.81** (red box, build 76c630d). ~22–30 hand-exported sims/LDO.
- **Final scoreboard** (dB unless noted): VDD0P8_VCO **[OK]** Zout 0.72 / PSRR 1.0 / noise 0.49; IBP_POLY_500N_LPF **[OK]** IVrms 0.30%; IBP_POLY_3P6U_VCO **[~]** IVrms 1.18% / |Y| 4.27; IBP_PTAT_TUNE_1P5U **[~]** IVrms 0.02% / |Y| 6.94; VDD0P8_PLL **[!!]** Zout 1.6 / noise 2.75 / **PSRR 6.0 (blocker)**. Box re-validate s2: vco Zout 0.86 / PSRR 0.25 / noise 0.49; pll PSRR 5.85 still blocker.
- **Red-zone fit grades before→after** (local GT): pll Zout 7.04→1.69, vco 9.60→0.76 (shelf fit); pll PSRR 5.85→0.35 (mag) / 5.72→0.21 (phase), vco PSRR 6.96→0.15; current-noise sinks 9.92/13.58/15.61→0.87/1.03/1.50 (log-amp fit); sink |Y| pole-zero i500n 1.93→0.09, i3p6u 4.27→0.65, i1p5u 6.94→0.26; I-V knee fix IVrms 63.5/63.5/31.7%→0.30/1.18/0.02%; noise hybrid mode pll 11→2.75, vco 11.95→0.49.
- cPSRR ~100 dB = metric artifact (2fF→90.8 dB, 5fF→97.8 dB); observability gate |gdd|/g0<1e-3. i1p5u_ptat Idc(T) 1.13/1.50/1.78 µA @ −40/55/125°C (+3.9 nA/°C); **tnom_c=55°C = house nominal = code fit_isrc.TNOM = GT library temp.**
- **z_pll Zout** = monotonic rising resistive SHELF: 0.095 Ω DC → 197 Ω plateau @~31.6 MHz; vco plateau 52 Ω. AC grid 10 Hz–500 MHz × 155 pts. Old single-inductor fit mislocated corner to 447 kHz (~70× off). Higher-order (L‖R) ladder recovers: Ra=0.095 Ω, sec1=22.9 µH/43 Ω, extra pll [(4.42 µH,155 Ω)] vco [(1.23 µH,50 Ω)]; |Z| err ≤0.34 dB @1/10/31.6 MHz, plateau ±1–1.5%. Single-section over-predicts 1–10 MHz by +5.68 dB. (.va default byte size 24810 B / 26 KB.)
- **PLL load-step / dip:** coverage.cdecap=20 pF; bare dip 310 mV → 160 mV with 20 pF decap. AC-correct ladder OVER-predicts: 245/409/573 mV vs GT 160/227/282 mV; model LINEAR 163.6 mV/mA vs GT SUB-linear 107/91/81 mV/mA (real loop STIFFENS, class-AB-like). Convolving measured z_pll(‖20pF) with 0.5→2mA step → ~164–201 mV vs GT 160 (AC-consistent Zout already reproduces dip). Effective node cap ~60–280 pF (20pF decap + VCO parasitic). Dip would need ~250 pF passive-LTI vs tau-fit ~20 pF → out of passive-LTI scope; τ ~60–90 ns.
- **PART2 compressive current-assist (SHIPPED) — the 治本 for the FF "trans negative" bug.** `i_assist = iaG·tanh(verr·|verr|/iaV²)`, verr=vreg−vout (ODD, f'(0)=0 EXACT). GT = the saved `results/redzone/wur_pmu_real_sweep.repro.npz` step waveforms `tr_{pll,vco}_*_Lnom_T55` (step 0.5→{2,3,4}mA pll / 2→{4,5,6}mA vco, 1 ns edge, 20 pF decap, T55). Fit Spectre-in-loop on the desk (`spectre -64`), held-out across amplitudes. **PLL iaG=2.8 mA, iaV=0.33 V** → dips 154/224/290 mV vs GT 160/227/282 (held-out fit-outer-predict-middle −3.7%), full-waveform GT-RMS 10.2/12.7/16.0 mV (LINEAR was 23.6/38.2/69.2). **VCO iaG=4.0 mA, iaV=0.22 V** → 74/103/129 vs GT 74.7/103.4/128.7 (rms 0.9 mV). AC |Z| ON-vs-OFF (same fit) ≤0.004 dB across 10 Hz–100 MHz; DC unchanged. Saturates at iaG (class-AB) so the bare-LTI −0.85/−1.924 V FF excursion is gone. The assist ALONE holds the rail ≥0 up to a step ≈ iaG + vreg/Z_lf (≈9.5 mA at vreg=0.8); for steps FAR past that (>~10 mA, where the saturated assist can't hold) the **deep `floor`=0.0 backstop is ON** (both rails; one-sided clamp engaging only below 0 V, zero value+slope above → invisible to DC/AC/the validated regime; `derive_iassist` carries `floor`/`gfloor` onto the DERIVED assist, not just the seed) → the rail drops to ~0 (dropout) instead of a non-physical negative. Verified on the deployed `.va` (300 µA→step, vdc=0.8): Vmin 498/220/−0.6/−1.3 mV at 4/8/10/12 mA (assist+floor) vs 193/−462/−787/−1117 mV (bare LTI). BAKED localparams → SURVIVES minimal (NO new CDF param — only `vreg_<rail>` exposed). Residual GT-RMS conflates compression with the AC(tt_25c)/step(tt_55c) ~×0.65 T-confound; a T55 z re-export would clean it.
- **The "FF-corner" / "VDD>0.8" trigger is INDIRECT — the negative-trans driver is the LOAD STEP, period.** Bare-model factorial (Spectre, 0.5→6 mA / 20 pF): sweeping **supply AVDD1P0 0.85→1.15 V → trans min = −0.0905 V at EVERY value** (0 mV change); **temp −40→125 °C → −0.0905 V at every value** (0 mV); **rail setpoint vreg 0.74→0.90 V → min rises −0.150→+0.010 V** (dip is a FIXED 0.890 V; min = vreg−dip, so HIGHER setpoint is SAFER, opposite of the "VDD>0.8 breaks it" guess). The behavioral model has ZERO process dependence (emits identically FF/TT/SS). ⇒ whatever correlates "FF" or "high-VDD" with the failure is the REAL PLL/VCO load drawing more current there (bigger di → deeper dip → crosses 0), NOT the LDO model reacting to V/T/P. The assist+floor attack the dip depth (the true driver), so FF and high-VDD are covered identically.
- **`fit_iassist` (PURE-PYTHON auto-derive, STANDARD FLOW — NO simulator).** `harness/fit_iassist.py`, folded into `fit_multiport`: the iaG/iaV are DERIVED from the coverage.transient GT (the `tr_*` dips already in the npz from the first ALPS/Donau run), not hand-set and not re-simulated — `predict_dip` solves the rail's branch-A-ladder + Cext + assist as an ODE (LSODA ~2 ms/solve, validated <1.6 mV vs Spectre on PLL+VCO, baseline+assisted), and `derive_iassist`/`fit_rail` solve the 2 params against the GT (grid+refine, minimal-intervention tie-break, held-out). At the first fit, ≈5 s total, runs on box/GUI/CI. Re-derives PLL ≈4.6 mA/0.44 V (held-out −1.2%), VCO ≈7.2 mA/0.31 V (0.0%) with the seed hidden. The manifest `iassist` is now a SEED used only when the npz lacks `tr_*` (legacy/single-OP). (The Spectre-in-loop version was removed — local Spectre is not allowed in production.) NOTE the 2 params are **partly degenerate** — a valley of (iaG,iaV) matches the same 3 dips; the dip SHAPE is determined, the exact split is not (gate on dip-RMS+held-out). Tests `harness/test_iassist_fit.py`.
- **Startup-drop diagnosis (RESOLVED 2026-06-30, TB-side):** "30 mV startup drop" = DC-OP initial charge of the 22.9 µH/534 ns branch-A fit-inductor — vary preload 37→150→300→405 µA ⇒ drop 55.1→39.6→19.0→4.8 mV (load current unchanged). Model robust droop ~3× too SHALLOW vs real ~25 mV. **Slew is WRONG-SIGN:** coverage clean-step dip minimized at SRa=∞; positive SRa worsens 245→1047→4456 mV @SRa=5e4/1.2e4 → fix is a COMPRESSIVE term. LTI-from-DC-OP startup dip = 0.0 mV (small-signal can't make a large-signal turn-on dip).
- **Model-vs-real (PLL):** real startup undershoot 712 mV (−88 mV) @25 ns, model only ~776 mV (−24 mV, ~4× too shallow); steady +14 mV (796 vs 781), new-TB bench +10 mV (800.6 vs 791.3); avg port current 404 µA vs 375 (+30/+8%). real_V single-exp τ≈33–45 ns, corr(I,V)≈0 (recomputed −0.05..−0.19; an earlier +0.50 was WRONG) → autonomous COLD-START envelope, NOT a load transient. Model near-ideal source DC Rout≈0.097 Ω (=Ra): 799.95 mV@0.5mA, 799.81@2mA; silicon@0.5mA 776.6.
- **PMU DC-layer 20 mV bug:** real startup ~704 mV from 800; new model steady ~20 mV high (load-dependent). vreg(iload)=Vout_settled + R_a·iload. Transient steps pll 100µ→{2m,3m,4m}, vco 100µ→{4m,5m,6m}, 1 ns edge, 10 µs. Independent-GT verify: transient-consuming model <0.8 mV vs GT; non-consuming baseline 19.9–26.0 mV off (= user symptom). Cft feedthrough 1.74e-13.
- **UNLOAD-discharge (Route 1, SHIPPED 2026-07-01) — before/after (local Spectre, deployed params, 4 mA→0.3 mA, 1 ns edge, 20 pF ext decap, AVDD=0.98, vreg=0.82).** Baselines showing the trade-off any OUTPUT clamp faces: no clamp → peak **+301.8 mV (1.122 V, ABOVE the 0.98 V supply)**, recovery-to-5 mV ~1.95 µs; an output high-side clamp (Ghi=200, knee +20 mV) → bounded **+24.2 mV** but **HANGS 4.68 µs** (overshoot×recovery ≈ const). Route-1 branch-A discharge (drains the fit-inductor): **PLL peak +34.3 mV (0.854 V, ≤ supply), never above vreg+40 mV, monotone settle (0 chatter), ~1.9 µs to 5 mV; VCO +124 mV→+28.5 mV**. Bit-identical small-signal: AC |Zout| 1 Hz–1 GHz after-vs-before dev **0.000000 % (abs 0)** at both 0.3 mA and 4 mA OP; clean load-dip 0.3→4 mA **0.0000 µV**; deep-dip 1 ns fast-edge loading **−301.68 mV before = after** (not degraded). +40 mA floating-rail abuse-sink → **0.824 V, no runaway** (final=peak, no transient excursion). A NAÏVE unbounded series dump-R (no voltage clamp / no source-gate) instead RUNS AWAY to **142 V** on the same +40 mA (de-pin) — motivates the bounded+source-gated form. HB(200 MHz, 7 harm) + PSS converge with the discharge; full 3-rail+3-bias model compiles/op/ac/tran. **`ovVdz` = LARGE-SIGNAL TRANSPARENCY FLOOR** (adversarial-review, local-Spectre): a periodic load ripple swinging the rail ~20 mV above vreg gets clip-distorted **16.4 mV at ovVdz=3 mV, 9.6 mV @10 mV, 0.12 mV @20 mV, 0.002 mV @25 mV** → default ovVdz=**25 mV** (2.5× the ~10 mV char) keeps the spur/PSRR/Zout spectra bit-identical; sub-µs-to-5mV (needs ovVdz<5 mV) was the traded axis. Other defaults: ovR=4 kΩ, ovVmax=2 V, ovVsc=8 mV, ovIsc=1 mA (`emit_pmu_model._OVD_*`; per-rail `unload_discharge` override). Deployed L_a: PLL 22.883 µH + ladder 4.422 µH; VCO 0.850 µH + 1.227 µH.
- Retired (wrong-physics) hand-tuned knobs: La=120 µH (1.2e-4), Lreg=16 µH, Rreg=750 Ω, Cs=25 pF, Rs=2000; replay 2.47 mV vs silicon BUT AC-inconsistent (La120 |Z|@10MHz=558 Ω vs measured ~160). AC-consistent+guarded (La fixed 24 µH, Cs≤5 pF): 5.79 mV, Cs=0.98 pF, SRa=8.4e3, Lreg=26.7 µH, Rreg=245 Ω, Rs=5713. Replay: compensated.va 2.47, lti_la120 no-slew 2.37, baseline_noslew La24 26.58 mV; fit-to-real_V overfit 2.47→35.5 mV held-out. slew_a=12000.
- Digest sizes: 3-temp 125→53 KB; budget 53→28.6 KB@30; load×temp gz 236→31 KB (7.6×). ALPS jobs: full 225 (75/temp×3); temp_sweep:['dc'] → 81 jobs (64% fewer).

## 6. Coverage sweep parameters (locked, capless LDO)

- PLL rail: nominal OP 500 µA; min/max 50µA/2mA; 4 log pts 50/170/580/2000 µA; held-out (crossval) 300 µA; binding = light-load PSRR/stability.
- VCO rail: nominal 2 mA; min/max 200µA/6mA; 4 log pts 200/620/1900/6000 µA; held-out 3 mA; binding = heavy-load dropout.
- Transient steps: PLL @500µA lin ±50µA / big ±500µA / slew 0→2mA; VCO @2mA lin ±200µA / big ±1mA / slew 0→6mA (+VCO big also @1mA OP); edge ≈1 ns fixed.
- Temps −40/55/125°C, nominal/room = 55°C; light-load floor ~1–5% of max.
- bench defaults: STEP_BASE=121e-6, STEP_DI={lin:50e-6,big:1e-3,slew:5e-3}, LIN_FRAC=0.3, AC dec 40 10 100meg, NOISE dec 20 10 100meg. HF_STOP=500e6, AC_HF="ac dec 40 10 500meg".

## 7. Current-source behavioral model (8-GT library)

- 8 transistor-level GT current sources; ONE template reproduces ALL 8: Idc ≤0.36%, IV ≤4.95% RMS, rout ≤6.6%, PSRR sign ok, PTAT ≤0.001. Idc range 0.57–2 µA, rout 6.9→1151 MΩ, PTAT v6 1.679 (≈ideal 1.708). Archetypes: v4_pmos_simple SOURCE dId/dVdd +56 nA/V; v6_ptat SINK −145 nA/V + PTAT. supply_dc GT=1.05 (hardcoded 1.0 caused 17.75% IV bug). Temps −40/55/125, typical corner tt_55c.
- **Sink g0/rout bug:** chord rout 3.6 MΩ → IVrms 33% vs AC-admittance rout 802 MΩ → 0.88%; g0 225× too steep; matches box 36.49%/0.30% (emit-vs-grade divergence). Fix: derive rout from AC-admittance DC real part.
- 2nd-order Idc(T) engages only ≥5 unique temps AND quad beats linear ≥10% SSE AND linear resid ≥0.01% RMS (TEMP_QUAD_RESID_FLOOR; without floor a near-linear fit produced meaningless d2=5e-24).

## 8. Trans-ID (one multitone .tran replaces 2 AC sweeps)

- Level-1: Zout ≤0.45 dB everywhere, PSRR ≤~1.6 dB RF band (≥100 kHz), mid-band PSRR ≤0.1 dB, smoke <1° phase. Level-2 model: max|dComposite|=2.60 (base +0.06, v1 −0.68, v3 +2.18, v2 +2.60). Linearity/IM half-amp gate ≤0.15 dB.
- Cost: 3 coherent transients/corner (bands 1k–100k / 100k–10M / 10M–ceiling), ~10–15 s/variant vs single sweep ~1e8 pts; 12 tones/dec (20/dec made v3 WORSE +2.2→+5.4). Deep LF-PSRR @20µ: up to ~19 dB pt error (SNR floor). Compiled-VA e2e max|d_path|=0.04. R7 negative: v3 B-source 2.18→1.61 but compiled-VA 2.14→2.37 (d_path 0.04→0.76); v2 Zout candidates ALL identical AC-zrms (20u 1.797, 121u 0.957, 250u 0.321); v2 Cout 130 pF/ESR 116 Ω invisible.

## 9. System acceptance test (LDO + buffer @ carrier)

- 13/14 reproduce carrier <0.13 dB, sidebands <0.5 dB. v4 FAIL +7.2 dB/+67° due to |PSRR(Δ=1MHz)|=+16.8 dB; later proven a GT large-signal artifact: GT .ac PSRR +16.71 dB/+130.1° vs model +16.82/+128.1° (err +0.11/−2.0°), both peak +17.2 dB@1.059 MHz, emitted .lib +16.80.
- GT vrip linearity: 9.66 dB@10mV → 16.36@3mV → 16.75@1mV; auto-calib recalibrated v4 to 1.296 mV, cg_hi to 5.567 mV; v4 forced 10 mV → gain shifts 6.95 dB at vrip/4 (LARGE_SIGNAL). Knobs: LIN_FRAC_RIPPLE=0.01, KLIN=4, LIN_TOL_DB=1, LIN_MIN_SNR=8. v1 carrier under-predict 2.2 dB (GT 27.5 vs model 21.3 Ω = high-ESR floor).
- B-cover GHz: base_ghz hf_stop=10e9, f_c=6 GHz; Zout(6GHz) GT 0.487 vs model 0.4869 Ω; Cout/ESR 997 pF/0.5 Ω; composite 3.879~base 3.881. Ideal-cap stable; high-ESR breaks if ceiling naively bumped (v1 419pF→1.2pF).

## 10. Target-B real 5.8 GHz cap-less LDO (earlier air-gap loop)

- composite 268 → 2.3 (local replica) → 1.81 (red box). C_ft=174.4 fF vin→vout feedthrough; noise = HYBRID series bank 4 sections. Closed grades Zout 0.5 dB/2.4°, PSRR 0.38 dB/2.2°, noise 0.4 dB. Ghost-cap: 14 nF "extracted" vs 681 Ω peak @10 MHz = a cap-less part. Digest 51 pts/corner, +~12 pts around each |Z| peak, DC recovered ≤0.0005 mV, WARN <4 pts/dec & <3 pts above half-power.

## 11. Noise modeling

- noisefile/noise_table value = V²/Hz POWER (current src A²/Hz), NOT amplitude; .noise out = V/√Hz; proven flat 1e-14→1e-7 (=√). Supply-noise→output accuracy 1–3% typical, ~12% worst (8 MHz harmonic on PSRR-notch). Capless PSRR rolloff 40 dB@100kHz → 16.8@2MHz → 3.5@6MHz.
- base noise psd_rms 5.91→0.32 dB (energy-weighted); cross-engine noise lock 9e-6; weights W[sspur]=0.5, W[npk]=0.1, W[nir_lf]=0.01, caps NPK_CAP=6 dB, NIR_LF_CAP=50%. R4 off-corner noise 0.22–0.45 dB == in-corner 0.27–0.43 (interp holds); R5 temp noise 0.46–1.4 dB, LF bias ±1–6% over −40..125°C; v3_miller@125C=24.8 dB = DEGENERATE GT (regulation breaks hot). Generalization: 6 excellent(<5%), 9 good(5–10%), 2 marginal(~11–12%); 2 FAIL by construction v8_dlc 28%/5.9 dB, v10_3lc 285%/30 dB.
- **Spur sampling:** spur HWHM ≈ f0/(2Q) (~833 Hz for 2 MHz/Q1200); bracketed f0 353 nV vs coarse neighbor 6.5 nV = 54× miss; bracket 0.05…4×HWHM.

## 12. RF PSS acceptance

- Linear 304 MHz PSRR: model 57.7 vs GT 57.2 dB (~0.5 dB). 2nd harmonic GT −49 dBc@2mV → −29@20mV → −14@100mV (square-law); model floored −88 dBc (LTI, no upconversion). Standalone PSS 0.61 s vs 0.17 s (model ~18 nodes vs 8 FETs).

## 13. Guardrail / cross-engine golden numbers

- GUARDRAIL-3: dropout slope dVout/dIload = −20.000 Ω (=DUT Rout). GUARDRAIL-1: AC Zout slew_en∈{0,1} identical = 23.2301 Ω (rel 4.3e-8, =fit R_a); @6mA slew_en=1 → 187 mV dropout, Vout collapses −0.215 V (capless).
- Cross-engine LF Zout: Spectre-VA 23.230100 Ω == fit R_a (rel 2e-6) == ngspice 23.2301 Ω (rel 9e-13). binpsf local round-trip: AC 78 pts worst rel 6.3e-7; noise PSD worst rel 5.8e-17; cross-engine noise worst rel 9e-6 (10 Hz–1 MHz). Firewall gate reproduces pmu_standin.npz to 0.00e+00. Binary-PSF cross-val: AC 51 pts×34 traces worst 3.9e-6 (psfascii 6-sigfig floor); config-view path 14 arrays BIT-IDENTICAL.

## 14. Local-replay data fidelity

- RDP-800 0.43 mV, avg-bin ~9 mV, uniform-800 3.5 mV, 400 pts ~8–12 mV; rail V-dip is ~GHz-driven (can't average out); 2000-pt replay 1.43 mV vs box (400-pt gave 19 mV); 800 pt plain 20 KB, gz+base64 4.7 KB, 2000-pt plain 17 KB.

## 15. Grouped-PSF binary layout (reverse-engineered)

- nz.noise 192 MB, PSF groups=1, traces=19988; `out` = last decl id=20009, type=2 (V/√Hz), scalar. Constant stride: off_acc==stride=1363384 B; npoints×stride == VALUE_len−4; entries/pt 2+6997+75+3+12912=19989; struct widths 3/4/7/10; read wanted col at `v0 + i*stride + off + 8`. Oracle out[0]=8.638555e-05 @10 Hz → out[140]=4.581145e-09 @100 MHz (141-pt smooth rolloff).

## 16. Deploy / smoke / cluster

- Deploy: glibc 2.17/manylinux2014; AUDIT 15/15 wheels; numpy 1.26.4, scipy 1.15.3, matplotlib 3.9.4, PyQt5 5.15.10, PyQt5-Qt5 5.15.2, pillow 12.2.0; bundle 145.9 MB, incremental ~80 KB. Phase-1 zero-change gate: 0.00 composite delta all 14, .lib byte-identical, predict()<1e-9.
- Smoke: non-converging noise least_squares burns max_nfev=30000 (~20 s, status=0) vs 40–60 nfev converging; LDO_NOISE_FAST caps to 2000; smoke 21.5→10.3 s (also 50.8→12.6 s); residual ~10 s = matplotlib screenshots.
- Donau queues: short (32G/3h, prio75), normal (32G/24h), middle (64G/7d), long (128G/1mo), bigmem (512G). Standard LDO tuple `-q short -A ug_rfic.rfSClass -R "cpu=8;mem=8000"`, -mt 8 == cpu=8. Designer OP flo=5G, VDD=3, VDD1P0=1.05. PMU corner jobs: stand-in 2rail/2sink→8 groups/14 meas; real 3rail/3sink→10 groups/21 meas.
