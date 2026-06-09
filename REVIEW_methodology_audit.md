# LDO 行为级建模 —— 只读架构/方法论评审报告

> **范围**:只读审计(read-only design & methodology audit),零代码改动 —— 本报告是唯一产出。
> **日期**:2026-06-08 · **审计对象**:`harness/*`、`cadence/import_cadence.py` 及配套文档
> **方法**:三视角并行 fan-out(A 模拟/LDO 物理、B 第一性原理建模哲学、C 数学/过拟合),
> 再由 D 综合裁决并**逐条复核 file:line 证据 + 重跑 C 的可执行声明**。所有结论附 file:line 或具体数值。
> 增量建立在 `DEFERRED_REFACTORS.md` 的已知缺口 R1–R6 之上;凡与 R1–R6 定性/优先级有出入处均显式标注。

---

## 执行摘要(结论先行)

**三个视角从三个高度指向同一个根因:模型只在"样本内 + 块级"被验证过,从未"样本外 + 系统级"验证过。**
- **物理(A)**:线性二端口抽象的*形式*是对的(扰动在环路 UGB 之上时,vout 由开环无源输出网络决定,LTI 成立);
  但 VDD、温度、载波频带(GHz)、负载间的谐振迁移都是单点/超界使用,反馈路径只验块、不验系统。
- **架构(B)**:四个块乘的是**同一个** `Zout(s)` —— 它是不可约内核;问题是内核被验证得**最少**(无系统测试),
  外壳被验证得最多;"解耦噪声"只是代数往返,并未在物理上解耦。
- **统计(C)**:**过拟合已被坐实(非怀疑)**。留一负载角(LOCO)交叉验证显示样本内/样本外误差有 10×–100× 的鸿沟。

**一句话锁定根因**:*phase-accurate、按 OP 参数化的 `Zout(s)`* 是不可约内核,而它恰恰是全系统中最缺乏独立/系统级验证的对象。

---

## 三条裁决(对用户三个问题的直接回答)

| # | 问题 | 裁决 | 一句话理由 |
|---|---|---|---|
| 1 | 建模合理性 & 反馈路径合理性 | **有条件成立(CONDITIONAL)** | 抽象*形式*物理正确(在声明的包络内);反馈路径**必要但不充分** —— 只做了样本内、块级验证,从未验证真正的交付物(载波边带保真) |
| 2 | 是否太贪心 / 该不该按用途拆分 | **分层(LAYERED),不是拆分也不是朴素统一** | 部署端必须是**一个**模型(系统 PSS/HB 同时要 Zout/PSRR/噪声/spur 共节点);表征端**已经**是按块拟合。缺的是:内核 `Zout` 的相位/可辨识性验收门 + 按用途解析的打分卡 |
| 3 | 是否有过拟合 | **有(YES,已复现坐实)** | LOCO:样本内 Zout 0.1–1.6 dB / PSRR 0.07–0.27 dB,**留出角** Zout 1.6–13 dB / PSRR 1.8–8.6 dB;根因是 3 角二次插值(0 残差自由度)+ 秩亏拟合(cond=∞)+ 把开关参数当连续插值 |

> **证据可信度说明(orchestrator + D 复核)**:所有架构级 file:line 引用已逐条读文件核实通过。C 视角**实际运行了**(只读分析、未改任何源码)LOCO 交叉验证与 Zout 雅可比条件数计算;D **重跑**确认了定性结论,但**修正了 C 的两个具体量级**——C 报告 PSRR 样本外误差高达 66 dB(v4)/33 dB(v3),而干净的 2 点 LOCO 只得到 **7.5 dB / 1.8 dB**。**过拟合结论不变;但应引用可复现的 7–13 dB 鸿沟,而非 66 dB。**

---

# Perspective A — Analog IC / LDO physics

**VERDICT: CONDITIONAL — physically defensible *only inside a narrow, self-declared envelope* (disturbances above the loop UGB, small-signal, nominal VDD, nominal temperature, carrier ≤ 500 MHz), and the feedback path does NOT prove the actual deliverable.** The 2-port linear abstraction is the *correct* abstraction for the stated Target-A regime (8/16/24 MHz spurs sitting above a 1.78 MHz loop UGB, where the loop is open and the passive output cap dominates — PROJECT.md:14-28). Block-level fidelity (Zout/PSRR/noise/spur) is *necessary* but provably *not sufficient* for the system deliverable (LDO + RF buffer → vout sidebands at the carrier), and three hard physical boundaries are characterized but never validated: (1) the carrier-band impedance for a GHz target is outside the 500 MHz extraction ceiling; (2) VDD and temperature are single-point; (3) the system loop is never closed. The real-LDO symptoms in R6 are *physical consequences of the abstraction's limits, not code bugs* — confirming the envelope is being exceeded in real use.

### A1. Where the linear-2-port abstraction HOLDS and where it BREAKS

**HOLDS (and is the right call):** For disturbances *above the loop UGB*, the loop gain is rolled off, the regulator no longer fights the disturbance, and vout is shaped by the *open-loop* passive output network (Cout + ESR + parasitic L). PROJECT.md:14-28 verifies this empirically: an 8 MHz tone driven to an extreme 500 µA produces 16 MHz at −62 dBc, 24 MHz at −94 dBc — the part is linear to <−62 dBc in-band. With the spurs of interest (8/16/24 MHz) all ≥4.5× above the 1.78 MHz UGB, Zout(s)+PSRR(s) is genuinely *sufficient* for the Target-A spur math. **This is a correct physical insight, not a convenience.** Severity: observation. NECESSARY+SUFFICIENT *within the stated band*.

**BREAKS — and each break is a real working-domain condition the user named:**

- **Disturbances at/below the UGB → nonlinear loop.** PROJECT.md:30-33 admits it: energy <2 MHz engages loop slew. The model is LTI; any aggressor or load step with content inside the loop band is mismodeled. The −62 dBc floor is the *best case at the chosen drive*; it is not a guarantee at arbitrary amplitude. Severity: major. NECESSARY-not-SUFFICIENT (the abstraction silently drops a whole class of disturbances).

- **gm compression / slew / dropout.** Verified linearity edge (PROJECT.md:40-41): <5% to 50 µA, ~18% off at 1 mA (gm compression, edge-rate-independent), collapse at 5 mA. The model's *only* nonlinear handle is the `slew_en=1` PWL branch-A dropout table (fit_model.py:755, build_pwl:629). This is a static DC-curve clamp — it captures dropout/current-limit but **not dynamic gm compression** (a small-signal-parameter shift with bias, not a static I–V saturation). So at mA-class steps the model is wrong in a way the table cannot fix. Severity: major (for any LDO whose real load swings into compression). NECESSARY-not-SUFFICIENT.

- **Resonance MIGRATION with load is only partially captured → spur-fidelity hazard.** This is the sharpest physics finding. The output-pole/resonance frequency moves with bias (GT itself: 0.944 MHz @20µ → 1.778 @121µ → 2.113 @250µ, PROJECT.md:78-80). The model interpolates `L_a` quadratically in ln(iload) through exactly 3 corners (fit_model.py:598-602, 0 residual DOF), so the *resonance trajectory* is a parabola forced through 3 points — and on V3 it is **mislocated by 5× at the nominal corner** (GENERALIZATION_REPORT.md:69-72,135-136: pkf 5.0). **Physical consequence for spur fidelity:** vout ripple = I_disturbance × |Zout(f)|. Near a resonance, |Zout| has a sharp Q-peak; if the modeled peak sits at 0.94 MHz but the true peak is at ~1.78 MHz (a 5× error implies exactly this kind of gross misplacement), then a spur or sideband landing on the *true* peak sees a large |Zout| the model reports as small (or vice-versa) — a multi-dB ripple-amplitude error precisely where the deliverable is most sensitive. For Target A the resonance (~1.78 MHz) sits *below* the 8 MHz spur band so the error is partly masked; but for **V2 capless the resonance is deliberately pushed INTO the 4–8 MHz spur band** (variants.py:50-52). On any such part, a mislocated resonance directly corrupts the spur spectrum. Severity: **blocker for resonance-in-band architectures**, major generally. NECESSARY-not-SUFFICIENT; mostly OUT-OF-SAMPLE (the interpolated load is not a fitted corner).

- **Non-minimum-phase PSRR** is handled but imperfectly. The one signed complex-conjugate section (fit_model.py:167-184, 335-383) brought V4 pphase 121°→25° (GENERALIZATION_REPORT.md:72-74) — but **25° residual phase remains** and is reported as the residual boundary. A 25° PSRR phase error rotates the supply-coupled sideband phasor; when that sideband must add coherently with the Zout-coupled and intrinsic-spur contributions at the carrier, 25° is a real vector error in the final sideband. Severity: major. NECESSARY-not-SUFFICIENT.

### A2. Is the characterization recipe a measurement set a real designer would TRUST? — NO, several physically-fundamental things are missing.

A senior LDO designer signing off a supply model would *not* accept this set as complete:

- **No loop phase-margin / stability information, anywhere.** The recipe extracts driving-point Zout and the vin→vout transfer, never the loop gain T(s) or its phase margin. The model *reconstructs* a passive-RLC Zout that is minimum-phase by construction (fit_model.py:85-96), so it cannot represent a *peaking* Zout caused by low phase margin except through Q in the RLC fit — and it has no way to know if the real part is marginally stable. A regulated LDO can have **actively non-passive Zout** (Re(Z)<0 in band); score.py:55-70,138-141 explicitly observes this on V3 (GT minRe −0.23) and then notes the *passive model cannot match it* — that is a fundamental representational gap, not a tuning issue. Severity: major. NECESSARY-not-SUFFICIENT.

- **PSRR vs VDD is never measured.** PSRR is extracted at one vin (1.05 V, bench.py:60, CADENCE_EXTRACTION.md:51). PSRR is strongly VDD-dependent (pass-device headroom, near dropout it collapses). The user *explicitly states they will run high/nom/low VDD corners* (DEFERRED_REFACTORS.md:99-101). The model bakes in VREF=1.05 (fit_model.py:29, 896) and only the small-signal *deviation* couples (R3, DEFERRED_REFACTORS.md:102-108). So a VDD sweep produces no correct DC line-reg (the `dc_linereg` array is characterized but **read nowhere** — fit_model has no consumer, DEFERRED_REFACTORS.md:108-112) and no PSRR-vs-VDD. Severity: **blocker for the user's stated VDD-corner use**. NECESSARY-not-SUFFICIENT; OUT-OF-SAMPLE.

- **Single temperature (300 K).** bench.py noise/AC all at 300K (KT4 uses 300.0, fit_model.py:163). Real LDO PSRR, Zout LF (Rout∝1/gm∝temperature dependence), noise, and dropout all move with temperature. No temperature axis exists. Severity: major (any real sign-off needs corners). NECESSARY-not-SUFFICIENT.

- **The carrier-band impedance ceiling is below the carrier for the real target.** Off-nominal corners are characterized only to 100 MHz (bench.py:18) and the *nominal* corner only to 500 MHz (bench.py:30). The synthetic carrier is 304 MHz — inside 500 MHz only at the nominal corner; **off-nominal loads are extrapolated from 100 MHz to 304 MHz**, which for an inductive/capacitive tail is a fabrication. For the **real GHz-class target** (memory: Target B ~5.8 GHz), 500 MHz is ~12× below the carrier — the model has *no measured Zout/PSRR at the carrier at all*. Since vout ripple ∝ |Zout(f_carrier)|, a model with no valid carrier-band impedance produces essentially arbitrary (here, ~zero) ripple. Severity: **blocker for the GHz target**. NECESSARY-not-SUFFICIENT; OUT-OF-SAMPLE. (This directly corroborates R6#3 and R4's "*_hf ceiling ≠ system max freq".)

- **3 load corners with 0 residual DOF.** Every parameter is a parabola through exactly 3 points (fit_model.py:598-602). There is *no way to detect a bad fit between corners* — the interpolation overshoot was real enough to require a hard clamp (fit_model.py:611-626, "+15.7 dB verified overshoot"). A clamp is a band-aid that bounds the *symptom*; it does not give the *physically correct* mid-range value. Three corners cannot resolve a non-monotonic OP dependence. Severity: major. NECESSARY-not-SUFFICIENT.

### A3. Is the feedback path SOUND? — Block fidelity is NECESSARY but NOT SUFFICIENT, and the gate is unrepresentative.

This is the crux. The score (score.py:173-203) is a weighted sum of per-block, per-corner errors (Zout, PSRR, noise, transient, discrete-spur). **It never instantiates the actual use case.** The deliverable is: an RF buffer drawing periodic current at the carrier on vout → vout ripple + sidebands. That scenario is *never simulated for either GT or model*. R4 (DEFERRED_REFACTORS.md:149-174) states this and I confirm it independently:

- **No system test exists.** score.py contains Zout/PSRR/noise/trans/spur and one spur sanity gate — nothing closes the LDO+buffer loop. HANDOFF lists the system PSS/pnoise TB as still-to-build (DEFERRED_REFACTORS.md:163-166). Severity: **blocker**. The whole project goal (PSS/HB sideband fidelity) is asserted via building blocks, never measured end-to-end.

- **The "sanity gate" is not representative and not a model-vs-GT test.** score.py:162-166,248-250: a single 8 MHz *load* tone, pass/fail at −45 dBc — (a) one tone, (b) at 8 MHz not the 304 MHz/GHz carrier, (c) pass/fail, not a ripple/sideband *diff*, (d) a load tone, not a buffer at the carrier. It tests "is the model still linear," which the abstraction *assumes* — it is circular, not validating the deliverable. Severity: major.

- **Why block-sufficiency fails physically:** the final sideband at carrier±Δ is a *coherent vector sum* of three model paths — Zout×I_buffer, PSRR×V_supply-ripple, and intrinsic spur — each with its own phase. The score weights phase very lightly (W: zphase=0.04, pphase=0.03 vs zband=3.0, score.py:29-30), so a model can score well while carrying 10–25° phase errors (V4: 25°) that *cancel-or-add wrongly* in the sum. Good per-block magnitude does not imply correct coherent sideband. Furthermore, the resonance-migration (A1) and carrier-ceiling (A2) errors live exactly in |Zout(f_carrier±Δ)| — the dominant ripple term — and are *outside* what the score interrogates (score.py:114 even searches the resonance peak only below 10 MHz). **Block fidelity is necessary but demonstrably not sufficient.** Severity: blocker (the validation does not validate the deliverable).

- **500 MHz ceiling for a GHz carrier is insufficient** as established in A2; the system test, when built, must extend extraction to cover the carrier ± the widest sideband, or the system result is meaningless.

### A4. Can the real-LDO symptoms (R6) be explained by the abstraction's PHYSICAL limits? — YES, all three are physics, not (merely) bugs.

- **R6#1 poor fit:** consistent with A1 (resonance migration only fit to 3 corners; V3 pkf 5.0) and A2 (0-DOF interpolation, single-VDD/temp). A real LDO with a load-migrating multi-pole Zout or active (non-passive) regulation *cannot* be matched by a minimum-phase passive RLC — score.py:138-141 already documents this as a *floor*, not a bug. Physical limit.

- **R6#2 slow rail droop under transient:** partly the `idt()` Verilog-A inductor branches drifting without settled IC (fit_model.py:904,909,938) — that *is* a numerical/IC issue. **But** the deeper cause is physical/architectural: DC output is pinned by `VREG121` (fit_model.py:736,748) and `dc_linereg` is unused, so the model has *no correct DC operating point that tracks the supply* (R3). A model whose DC node is not anchored to a real regulated value *will* wander on a long transient. Mixed (bug + abstraction gap), but the abstraction gap (no VDD-tracking DC) is the load-bearing one.

- **R6#3 no buffer-induced ripple:** this is *pure physics of the envelope being exceeded.* Ripple = I_buffer × |Zout(f_carrier)|. If the carrier > 500 MHz, |Zout| there was never characterized; the synthesized passive network rolls off to ~0; ripple ≈ 0. This is exactly what A2's carrier-ceiling finding predicts — **not a coding error, a measurement-coverage error baked into the recipe** (bench.py:30).

**Implication for soundness:** the symptoms are the abstraction (and recipe) telling the user, correctly, that the real part is operating *outside the validated envelope* — resonance migration unmodeled, DC not VDD-anchored, carrier above the extraction ceiling. The method's *honesty* (passivity floor reporting, clamp, R1–R6 ledger) is a strength; the *gap* is that nothing automatically detects-and-refuses to extrapolate, and nothing closes the system loop.

### A's one-line answer

**CONDITIONAL.** The linear-2-port + additive-noise + discrete-spur abstraction is *physically correct and even elegant for the regime it was derived in* (disturbances above the loop UGB, small-signal, nominal VDD/temp, carrier ≤ 500 MHz), but it is **not yet defensible as delivered for the user's real working domain** because (i) the resonance migrates with load and is only fit through 3 corners — mislocated 5× on V3, a direct spur-fidelity hazard when a tone lands near a peak; (ii) VDD, temperature, and the carrier-band (GHz) impedance are single-point/below-ceiling, i.e. the model is used out-of-sample exactly where the user says they operate; and (iii) the feedback path validates blocks, not the system — block fidelity is *necessary but not sufficient* for the coherent carrier-sideband deliverable, and the observed real-LDO symptoms (R6) are physical consequences of these envelope violations, not bugs to be patched away.

Key file:line evidence: bench.py:18,30 (100/500 MHz ceilings); bench.py:16 (3 corners); fit_model.py:598-602,611-626 (0-DOF quadratic + clamp band-aid); fit_model.py:29,736,896 + DEFERRED_REFACTORS.md:99-112 (single VDD, dc_linereg unused); fit_model.py:163 (300 K only); score.py:29-30 (phase de-weighted), :114 (peak search <10 MHz), :138-141 (passive model cannot match active-non-passive GT — admitted floor), :162-166,248-250 (unrepresentative single-tone gate); GENERALIZATION_REPORT.md:69-74,135-136 (V3 pkf 5.0, V4 pphase 25°); PROJECT.md:14-41,78-80 (UGB/linearity-edge/resonance-migration); R4/R6 (DEFERRED_REFACTORS.md:149-174,230-253 — no system test; physical symptoms).

---

# Perspective B — First-principles modeling philosophy

### Verdict (conclusion-first)

**The four blocks do NOT demand mutually conflicting structures — they demand exactly ONE shared object: a phase-accurate, OP-parameterized `Zout(s)`. That is the irreducible kernel. PSRR, noise, transient and spur are all the SAME `Zout` driven by four different sources.** So the architecture's instinct — one linear 2-port — is *physically correct*, not greedy. **But the implementation under-commits to that insight in one place and over-commits in another:**

1. The "decoupled Norton" noise framing is **nominal, not real**: `In = Sv/|Zout|` then `Sv_emit = In·|Zout|` (fit_model.py:405, 519) is an algebraic round-trip — the *only* thing it decouples is the *synthesis branch*, not the *physics*. Noise rides the same fitted `Zout` (report.py:221 admits this verbatim). That is **correct physics** (a real LDO's output noise *is* shaped by `Zout`), so the round-trip is fine — but the marketing word "decoupled" is misleading and hides that **every Zout error propagates into noise, spur, AND transient identically** (NECESSARY consequence, not a bug). Severity: **observation** (mislabel), but it changes the risk picture in B3.

2. The kernel `Zout(s)` is characterized to a ceiling **below the carrier** (`AC_HF` stops at 500 MHz, bench.py:30; synthetic carrier ~304 MHz is *inside* but real GHz carrier is *outside*). Since *all four* observables = source × `Zout(fc)`, a wrong/absent `Zout` at the carrier zeroes ALL of them simultaneously (R6#3 "no buffer ripple"). This is the **single highest-severity structural issue** and it is a kernel issue, not a per-block one. Severity: **blocker** for the real (Target B) use case; SUFFICIENT to invalidate the deliverable.

3. The "one model to rule them all" question (B3): **for DEPLOYMENT, unified is mandatory** (the system PSS/HB run needs Zout+PSRR+noise+spur live simultaneously on one instance); **for CHARACTERIZATION/FIT, split is already the de-facto reality** (per-block fitters with independent selectors, GENERALIZATION_REPORT). The correct answer is therefore **layered: one shared kernel `Zout(s)` + per-use shells**, which is *almost* what the code is — the defect is that the shells were tuned and validated *per-block in isolation*, while the only thing that matters (the kernel) is the least independently validated (no system test, R4).

### B1. Minimal physical description → unavoidable observables

A linear-regime LDO is a **single-input-single-output regulated node** with one feedback loop:

```
  vin --[PSRR path: supply ripple → vout]--.
                                           v
  vref --[+]--> A(s) --> pass device gm --> vout node --- Zout_open(s) --- load
          ^                                   |
          '------- β feedback ---------------'
```

The *closed-loop* output node presents one impedance `Zout(s) = Zout_open(s) / (1 + T(s))`, where `T(s)=A(s)·gm·β` is the loop gain. From this **single node equation** `vout = Zout(s)·[ i_load + i_PSRR(vin,s) + i_noise(s) + i_spur(s) ]`, the observables fall out *mechanically*:

| Use-case | Driving source at the vout node | Observable you cannot omit |
|---|---|---|
| **(a) PSRR / disturbance / spur** | `i_PSRR = Y_couple(s)·vin` (supply→node admittance, set by `1/(1+T)` and pass-device `g_ds`/`C_gd`) | `PSRR(s) = vout/vin = Y_couple(s)·Zout(s)` — **needs `Zout(s)` AND the coupling admittance, both phase-accurate**. Spurs are the *same* path at discrete tones. |
| **(b) noise** | `i_n(s)` = sum of device current-noise referred to the node, **already loop-shaped by `1/(1+T)`** | `Sv(f) = \|Zout(s)\|²·S_in(f)` — output PSD is `Zout`-shaped by physics, NOT white-through-Z. (Memory: "noise is loop-shaped" — correct.) |
| **(c) load/line transient** | `i_load(t)` step | `vout(t)` = inverse-transform of `Zout(s)·i_load` → droop=∫, ring=`Zout` resonance, settle=loop bandwidth. **Transient carries NO new information beyond `Zout(s)` in the linear regime** (Memory: "transient governed by Zout(s)" — correct). |
| **(d) DC reg / dropout** | DC operating point | `Vout(I_load,Vin)` = load-reg slope (`= Zout(0)`) + line-reg + dropout knee. **Large-signal only**; the only *nonlinear* observable. |

**Key derivation:** (a), (b), (c) are **the same `Zout(s)` with three different sources**. Only (d) escapes — it is the nonlinear DC/dropout surface, genuinely separate. This is why the code's split (`slew_en=1` PWL/dropout table, fit_model.py:755,913) is *physically* the right seam: the linear kernel and the nonlinear DC core are the **only two truly distinct physical objects**.

### B2. Sufficient minimal model per use-case — do they conflict?

| Use-case | Sufficient minimal model | Shares `Zout`? |
|---|---|---|
| (a) disturbance/spur | `Y_couple(s) × Zout(s)`, both with **non-minimum-phase** capability | YES |
| (b) noise | shaped Norton `S_in(f)` × `\|Zout(s)\|²` | YES |
| (c) transient | `Zout(s)` only (+ DC bias) | YES (literally nothing else) |
| (d) DC/dropout | nonlinear `I(Vdrop)` table | NO (separate, correctly) |

**Do they fight over `Zout`? No — they SHARE it, and that is physically mandatory, not a compromise.** A real regulated node has ONE impedance; if the noise saw a different `Zout` than the PSRR path, the model would be *unphysical*. So the prompt's worry ("do they SHARE Zout or FIGHT") resolves cleanly: **sharing is correct; the only danger is that a single `Zout` fit error is now a common-mode error across (a),(b),(c).**

**On the "decoupling" claim (fit_model.py:386-405, 519; report.py:221):** The code computes `In_target = Sv_meas/|Zout_fitted|` then emits `Sv = In·|Zout_fitted|`. Examine what cancels:
- If `Zout_fitted == Zout_GT`: `Sv_emit == Sv_meas` exactly. Round-trip is lossless. Good.
- If `Zout_fitted ≠ Zout_GT` (e.g. V1 zrms 1.94 dB, GENERALIZATION_REPORT:69; V3 pkf 5.0 resonance mislocated 5×, line 20): the emitted model re-multiplies the *wrong* `|Zout|`, so the noise PSD inherits the **same** Zout error. The fit residual `_noise_resid` (fit_model.py:587) hides this because it *also* uses `Zout_fitted` on both sides — it measures `In`-bank fit quality, **not** end-to-end `Sv` vs GT. The real `Sv` error against GT = `In`-bank-error ⊕ `Zout`-error.

**Verdict on decoupling:** It is **real for synthesis robustness** (the genuine win, fit_model.py:392-394: branch-B no longer re-shapes the noise) but **nominal for accuracy** (Zout error still leaks, by physics). Severity: **minor / observation**; NECESSARY consequence of correct physics — do not "fix" it, but **stop calling it decoupled** and **score `Sv` vs GT directly** (score.py:127 `_noise_metrics` does compare model-resim vs GT — good — but the *fit-time* metric at fit_model.py:587 is in-sample and self-consistent, so it under-reports).

### B3. "One model to rule them all" — greedy or right? Both sides, then a call.

**The case that it IS too greedy (split into per-use models):**
- **Identifiability dilution.** A single composite fit (score.py:29 weights `zband=3.0` dominant, `zphase=0.04` near-zero) lets a fitter trade phase fidelity for magnitude. A disturbance-only model would weight PSRR phase at the carrier *heavily*; a noise-only model would weight the LF flicker/resonance band. The unified composite **averages away** what each use-case cares about. Evidence: V4 PSRR `pphase` sat at 25° (GENERALIZATION_REPORT:74) precisely because phase carried weight 0.03–0.04 — a disturbance-only model would never have tolerated that.
- **The clamp band-aid is a symptom of greed.** `_pexpr` clamps interpolated params to `[min/1.5, max·1.5]` (fit_model.py:611-626) after a verified **+15.7 dB spur-band overshoot** from forcing a quadratic through exactly 3 corners with 0 residual DOF (fit_model.py:598-602). A purpose-built spur model would characterize amplitude vs load on its *own* grid, not borrow the load-interp scheme designed for `Zout`.
- **Common-mode failure.** Because everything multiplies `Zout` (fit_model.py:184, 519; spur at 581), one `Zout` defect corrupts all four outputs at once — the *opposite* of robustness.

**The case that unified is RIGHT (keep one model):**
- **Physics is unified.** There is *one* node, *one* `Zout`. Splitting into 4 models would let them disagree about `Zout`, producing a model that is internally inconsistent (a noise-only `Zout` that contradicts the PSRR-only `Zout` is unphysical and will mis-predict any cross-term).
- **Deployment reality is decisive.** The user drops **ONE instance** into a system PSS/HB run that must *simultaneously* present `Zout` (for buffer ripple), `PSRR` (for supply-tone sidebands), `noise` (for pnoise), and `spurs`. You **cannot** swap models mid-analysis — the buffer load current and the supply aggressor and the device noise all interact through the *same* node in the *same* simulation. A split set is **un-deployable** for the actual goal. The per-block *selection* that already exists (GENERALIZATION_REPORT: per-variant Zout branch-B / PSRR section count) is a **characterization-phase** convenience that *collapses into one netlist* at emit — which is exactly right.

**Recommendation: LAYERED — one shared kernel + per-use shells, with the fit weighting made use-case-aware.**
- Keep the **single emitted instance** (deployment demands it). This is non-negotiable for the stated goal.
- Keep **per-block fitters** (already present) — this *is* the "split during characterization."
- **Change two things:**
  1. Fit the **kernel `Zout(s)` to a use-case-agnostic, phase-first criterion** *before* the shells, not as a weighted term that PSRR-mag can outvote (today `fit_zout` runs first, fit_model.py:462 — good — but it's graded by mag-RMS, and the composite under-weights its phase). Make `Zout` phase a *kernel acceptance gate*, because all four shells inherit it.
  2. At **score time**, add a **use-case-resolved scorecard** (disturbance-mode, noise-mode, transient-mode) so a user analyzing *only* PSRR sees the metric that matters to them — without changing the single emitted model. This gives the benefits of "specialized models" (focused error reporting) with the consistency of unified physics.

Severity of NOT doing this: **major** — the current single composite (score.py:29) is a deployment-phase metric masquerading as a per-use acceptance metric, and R4 confirms the *actual* use-case (carrier sideband fidelity) is never scored at all.

**Does deployment vs characterization change the answer? Yes, and it's the crux:** In *characterization* you want maximum freedom to fit each block well (split is good, and exists). In *deployment* you have one node, one analysis, all sources live (unified is mandatory). The layered architecture is the *only* one that satisfies both — and it is what the code nearly is. The gap is not structural; it is that the **kernel is validated least** (no system test, R4) while the **shells are validated most** (per-block residual tables).

### B4. The irreducible kernel (one sentence)

**The irreducible kernel that no LDO behavioral model for this use-case can omit is a phase-accurate, OP-parameterized `Zout(s)` (the closed-loop output impedance), with the supply-coupling admittance `Y_couple(s)` (→ `PSRR = Y_couple·Zout`) as the inseparable second pillar — because transient (`Zout·i_load`), noise (`Zout·i_n`), spur (`Zout·i_spur`), and PSRR (`Zout·Y_couple·vin`) are all the SAME `Zout` driven by four sources;** everything else is optional per use-case: the noise Norton bank (drop for disturbance-only), the discrete-spur tones (drop if none), and the nonlinear DC/dropout table (the lone genuinely-separate, large-signal-only object).

### Severity-tagged finding list

| # | Finding | Severity | In/Out-sample | Nec/Suff |
|---|---|---|---|---|
| B-1 | `Zout(s)` is the shared kernel; all 4 blocks multiply it (fit_model.py:184,405,519,581) — architecture is physically correct, not greedy | observation | — | the kernel is NECESSARY; everything else is per-use |
| B-2 | "Decoupled" noise is algebraic round-trip `In=Sv/\|Z\|` → `Sv=In·\|Z\|` (fit_model.py:405,519; report.py:221) — decouples *synthesis*, not *physics*; Zout error leaks identically into noise/spur/transient | observation (mislabel) | fit-time `_noise_resid` is IN-SAMPLE (fit_model.py:587) | NECESSARY consequence of correct physics — don't fix, relabel + score Sv-vs-GT |
| B-3 | Kernel `Zout` ceiling 500 MHz (bench.py:30) < real carrier → all 4 outputs → 0 at carrier (R6#3); the deliverable (carrier sideband fidelity) rides entirely on `Zout(fc)` | **blocker** (Target B) | OUT-OF-SAMPLE (carrier never characterized) | SUFFICIENT to invalidate deliverable |
| B-4 | Single composite (score.py:29, `zphase=0.04`) is a deployment-phase blend that lets mag outvote the phase the shells inherit; no use-case-resolved scorecard; carrier-sideband never scored (R4) | **major** | the composite is IN-SAMPLE on characterization stimuli | composite is NECESSARY but NOT SUFFICIENT for the use-case |
| B-5 | 3-corner quadratic with 0 residual DOF (fit_model.py:598-602) + `_pexpr` clamp band-aid (fit_model.py:611-626, +15.7 dB overshoot) = load-interp designed for `Zout` borrowed by spur/noise amplitude | major | — | SUFFICIENT to cause off-corner error; the clamp is a symptom of forcing one interp scheme on all blocks |
| B-6 | Recommendation: keep ONE emitted instance (deployment), keep per-block fitters (characterization), add kernel-`Zout` phase-first acceptance gate + use-case-resolved scorecard = LAYERED (shared kernel + per-use shells) | — | — | the layered form is the unique one satisfying both phases |

**Disagreement with a stated severity:** R4 (DEFERRED_REFACTORS.md:149) is labeled "likely the single most important item" — from first principles I **agree and sharpen it**: R4 + R6#3 (B-3 above) are *the same root cause* — the kernel `Zout(s)` is the only object that matters for the deliverable, yet it is the least validated (no carrier characterization, no system test). I would **promote R6#3 from "concrete bug" to blocker** and bind it to R4: they are one issue (kernel `Zout` at the carrier), not two.

Files cited: `harness/bench.py` (16,18,25-30), `harness/fit_model.py` (184,386-405,461-462,497-520,587-595,598-602,611-626,755,913), `harness/score.py` (29-30,114,127,159,248-250), `harness/report.py` (221), `DEFERRED_REFACTORS.md` (149-174,230-253), `GENERALIZATION_REPORT.md` (20,69,74).

---

# Perspective C — Math / statistics (overfitting audit)

> **复核备注(D + orchestrator)**:C 视角实际运行了只读分析(LOCO、SVD 条件数),未改动任何源码。其**定性结论(过拟合坐实、10×–100× 鸿沟)经 D 重跑确认**;但 C 报告的最大 PSRR 样本外量级(66.2 dB / 33.2 dB)**未能在干净的 2 点 LOCO 中复现**(D 得 7.5 dB / 1.8 dB)。**应引用可复现的 7–13 dB 鸿沟。**

### Conclusion-first verdict

**Overfitting is confirmed, not merely suspected — quantitatively.** Two independent, executable tests settle it:

1. **Leave-one-corner-out (LOCO) cross-validation** (I ran it; numbers below): the model fits each *characterized* corner to 0.06–0.5 dB but predicts a *held-out* corner with 1.4–7 dB Zout error and **6.6–66 dB PSRR error**. A 100×–1000× in-sample-vs-out-of-sample gap is the textbook signature of overfitting. Critically, the held-out **121µA corner is genuinely bracketed** by 20µA/250µA in ln(i) (ln values −10.82, −9.02, −8.29), so this is *interpolation* error, not extrapolation — and it is still 4.4–9.5 dB on PSRR vs 0.08–0.24 dB in-sample. *[D correction: cite the reproducible 7–13 dB Zout / 1.8–8.6 dB PSRR gap; the 66 dB figure did not reproduce under a clean LOCO.]*

2. **Per-corner fit quality (in-sample) is excellent and therefore proves nothing about generalization.** Each transfer function is fit per-corner with far more parameters than the data can constrain (condition numbers up to ∞), and the inter-corner law is a quadratic through exactly 3 points (0 residual DOF). Both layers are saturated: in-corner residual is driven to a floor that *cannot reveal* overfit.

The model is **necessary-but-not-sufficient**: the 2-port architecture (Zout‖PSRR‖Norton-noise‖spurs) is the right *form*, and within each measured corner it reproduces the GT. It is **not validated to generalize** to any operating point, frequency, load, or VDD it was not directly handed. The scoring loop (`score.py`) re-simulates on the **same** stimuli/corners/freqs it fitted (`bench.py:16,18,29`), so it is *semi-independent* (catches netlist/realization bugs) but is **blind to generalization by construction**.

### C1 — Parameter count vs independent data volume: over-parameterized per corner, starved between corners — **major, in-sample**

Data per corner (from `bench.py:18,29` + verified array shapes in `base.npz`):
- Zout/PSRR AC: `ac dec 40 10 100meg` = 281 complex points = **562 real constraints** each (z_121u shape (281,3) confirmed).
- Noise: `noise … dec 20 10 100meg` = 141 points = **141 real constraints** (noise_121u shape (141,2) confirmed).

Free parameters per corner (`fit_model.py`):

| Block | Params/corner | Source |
|---|---|---|
| Zout | up to 5 (R_a,L_a,R_pl,R_b,L_b) | `fit_model.py:85` |
| PSRR real bank | 7 (G0,G1,w1,G2,w2,G3,w3) | `fit_model.py:152,170` |
| PSRR complex section | 4 (b0,b1,w0,Qf) | `fit_model.py:153,170` |
| Noise | 1 white + 6 amps (corner freqs shared) | `fit_model.py:159,386` |

**Per-corner the data:param ratio looks healthy (562:16 for AC).** The over-parameterization is *not* the headline; the headline is the **inter-corner DOF starvation**: every param is a quadratic in ln(iload) fit through **exactly 3 corners** (`fit_model.py:602` `np.polyfit(u,y,2)`). A degree-2 polynomial has 3 coefficients fit to 3 points → **0 residual degrees of freedom**. The interpolant is *forced* through all corners; the in-corner reconstruction error along the load axis is identically zero and **carries no information about correctness between corners**. That is the structurally over-parameterized layer.

Verdict: **per-corner fit is well-posed in count but the cross-load model is saturated (0 DOF) and cannot self-diagnose.**

### C2 — Identifiability / degeneracy: rank-deficient Zout, boundary-pinned params, mode-switching across corners — **blocker, in-sample**

I computed the SVD condition number of the Zout fit Jacobian (∂ln Z/∂ln p) at the nominal corner:

| Variant | cond(J) | singular values | symptom |
|---|---|---|---|
| base | **∞** | [13.9, 10.6, 1.2e-6, 3e-10, 0] | R_pl=1e9, R_b=1e9 → 2 params have **zero sensitivity** |
| v2_capless | **∞** | [14.0, 8.5, 2e-6, 4e-10, 0] | same |
| v1_nmos | **∞** | [14.1, 2.67, 2.25, 4e-10, 0] | R_b=1e9 dead |
| v3_miller | **2.3e8** | [9.6, 9.0, 6.8, 2.0, 4e-8] | w1=0.002 MHz degenerate + L_b/R_b near-cancel |
| v4_ffpsrr | 36 | [9.9, 6.6, 4.0, 0.56, 0.28] | only well-conditioned case |

This confirms and **strengthens** the code's own admissions:
- **Cout/ESR weakly identifiable** (`fit_model.py:53-57`): v1_nmos reads **381 pF for true 1000 pF** (−62% error) and ESR 28.2 vs 30 — verified live. Code admits a joint LS "sent V2's invisible cap to 1pF/1e269F." This is a textbook unidentifiable-parameter blowup.
- **R_pl runs to the 1e9 bound** (`fit_model.py:128-132`) on base/v2/v4-20u — i.e. R_pl→∞ is the "off" state. It is a **switch masquerading as a continuous parameter**: v1_nmos has R_pl = [7.8e6, 54.5, 53.7] across corners — a 5-order-of-magnitude jump (plateau OFF→ON). You cannot quadratically interpolate a switch.
- **The model ORDER changes across corners** — newly found and worse than the known items: **v2_capless uses nsec=4/cpx=1 at 20µA but nsec=1/cpx=0 at 121µA & 250µA** (`fit_model.py` selector `fit_psrr:380-383`). v4_ffpsrr R_b is active at 20µA but the topology differs corner-to-corner. Interpolating params of a 4-section complex PSRR against a 1-section real PSRR is **interpolating between two different models** — physically meaningless, and a direct cause of the LOCO PSRR blow-ups.

Verdict: **multiple parameters are non-identifiable (rank-deficient Jacobian) or are discrete switches treated as continuous. The "5-param Zout / 11-param PSRR" is effectively 2–3 + a variable number of active modes.**

### C3 — Regularization / model-order selection: ad-hoc band-aids, not principled regularizers — **major**

There is **no global regularization** (no L1/L2 penalty, no AIC/BIC/MDL order selection, no parsimony criterion). The gates are local heuristics:
- **Branch-B 40% gate** (`fit_model.py:147` `e2 < 0.6*e1`): a fixed relative-improvement threshold, not an information criterion. A 40% in-sample SSE drop from adding 2 free params (R_b,L_b) is almost always achievable by fitting structure that may be measurement/numeric artifact; there is no penalty for the added complexity and no out-of-sample check.
- **"Prefer complex within 2×" PSRR selector** (`fit_model.py:374-383`): chooses the complex section when its residual is "within 2× of best AND ≤0.15." The code itself calls this "not a pure data fit." It is a manual tie-breaker; combined with the shelf-trigger `e_shelf<0.05 AND phase<2.5°` (`fit_model.py:353`) it produces the **corner-dependent order switching** in C2.
- **MIN_LOG_GAP separation penalty** (`fit_model.py:415,428-429`): a *genuine* regularizer in spirit (anti-degeneracy on noise poles), but it is a band-aid for a known failure — its docstring says it exists to prevent "the +15.7 dB inter-corner interpolation overshoot."
- **_pexpr clamp** (`fit_model.py:611-626`): explicitly a "band-aid after a verified +15.7 dB spur-band overshoot." It clamps the interpolated magnitude to [min/1.5, max×1.5] of corner values. **This is not regularization — it is censoring a symptom.** I verified it **fails to catch the worst overshoots** because (a) it only applies to log-space params and (b) the ±1.5× window is too loose: e.g. v3_miller **pcw0 interpolates to 1.245e8 Hz at the 174µA between-corner load** (corners [60.4, 8.23e7, 8.33e7]) and **sails through the clamp** (max×1.5 = 1.25e8). v1_nmos R_pl interpolates to 38.4 Ω (corners [7.8e6, 54.5, 53.7]) — also un-clamped (min/1.5=35.8). The clamp keeps magnitudes "inside the envelope" while the model is still wrong because the *envelope itself* spans a switch.

Verdict: **the fitter goes to high order by default (4 PSRR sections on every B-variant) with only local, in-sample, ad-hoc gates. The clamp and gap-penalty are documented patches over verified interpolation pathologies, not principled model selection.**

### C4 — Validation independence: there is none — **blocker**

Precisely, as the prompt frames it and as I verified:
- **`report.py` predict()** uses `zmodel`/`psrr_model`/`In·|Zout|` — the *identical* transfer functions `fit_all` optimizes (`fit_model.py:497-520`). **In-sample by construction.** Its docstring even states "the GUI's before/after overlay IS the fit quality itself."
- **`_selftest`** (`fit_model.py:951-980`) asserts `predict()==fitter analytic` to 1e-9. **Tautology** — it tests numpy reproducibility, not model validity.
- **`score.py`** re-sims via ngspice but on `bench.LOADS=["20u","121u","250u"]`, `AC="ac dec 40 10 100meg"`, same noise/transient stimuli (`score.py:103,110,119,126`). **Semi-independent**: it catches `.lib`/`.va` realization bugs and numeric mismatch (valuable — and necessary), but every frequency, every corner, every load it scores is one the fitter already saw. **It cannot measure generalization.**
- The noise block is *not* even decoupled in validation: `report.py:221` notes "Sv=In·|Zout|, so Zout errors leak in here too" — the noise residual rides the fitted Zout, so a Zout error double-counts and a noise "pass" can mask it.

**There is no held-out frequency, corner, load, or VDD anywhere in the harness.** Combined with R4 (`DEFERRED_REFACTORS.md:149-178` — no system-level LDO+buffer@carrier test), the project's actual deliverable (sideband fidelity at the carrier) is **asserted from block metrics, never measured**, and even the block metrics are in-sample.

### C5 — ln(iload) quadratic interpolation: overfitting of 3 corners, demonstrated — **blocker, out-of-sample**

The quadratic-through-3-points (`fit_model.py:598-602`) is the core overfitting mechanism. I ran the definitive test — **leave-one-corner-out** — predicting each held-out corner from the other two and scoring against that corner's GT:

| Variant | held-out | Zrms OOS (in-sample) | PSRR rms OOS (in-sample) |
|---|---|---|---|
| base | 121µA (interp) | 1.46 (0.47) | 6.60 (0.16) |
| base | 250µA | 2.81 (0.19) | 7.45 (0.13) |
| v3_miller | 250µA | 2.54 (0.96) | 33.2 *(→ D: 1.80)* (0.27) |
| v1_nmos | 20µA | 1.97 (1.64) | 19.4 (0.27) |
| v4_ffpsrr | 20µA | 5.13 (0.10) | 66.2 *(→ D: 7.46)* (0.07) |
| v2_capless | 121µA (interp) | 1.39 (0.52) | 6.94 (0.24) |

Even the *bracketed, genuine-interpolation* case (121µA held out) gives 6.6–6.9 dB PSRR error — far above any spec the score weights imply (PSRR `pband` weight = 2.0, `score.py:29`). The mechanism is exactly as predicted:
- **0 residual DOF** → in-corner error identically 0 → the GENERALIZATION_REPORT residuals (V1 zrms 1.94, V3 pkf 5.0, V4 pphase 25°) are all *in-sample* and the load axis is invisible to them.
- **Between corners**: a quadratic through a non-monotonic 3-point pattern overshoots. Verified live: v3_miller pcw0 → 1.245e8 (above envelope, escapes clamp); v4_ffpsrr gn6 → 8.99e-3 (corners [7e-7, 5.8e-3, 8.7e-3], non-monotonic); v1_nmos R_pl → 38.4 (a switch interpolated to a nonsense value).
- **Beyond corners**: the `.param ic = min(max(iload,20u),250u)` clamp (`fit_model.py:734`) *freezes* every param at the 20µA or 250µA value — the model is **flat outside [20µA, 250µA]**, i.e. it has no valid extrapolation at all. For a "general" tool this is a hard validity boundary that is silently imposed.

Verdict: **the ln(iload) quadratic is overfitting these 3 corners, demonstrated by a >40× in-vs-out PSRR error gap even under pure interpolation.**

### Concrete, executable out-of-sample validation plan

All of these are runnable today against the existing GT generators (`gen_reference.py`) with no model edits:

1. **Leave-one-corner-out (LOCO) — already implemented above, promote it into `score.py`.** For each corner, build the ln(i) interpolant from the other 2 (linear for 2 pts) and score the held-out corner against its GT z/p/noise. Acceptance gate: held-out PSRR/Zout RMS within, say, 2× of in-sample. The model **fails this today**.
2. **Off-grid (4th) load corner.** Generate GT at a load *between* corners (e.g. 49µA and 174µA — the geometric mids) via `bench.measure_*`, then score the *emitted* `.lib` (which interpolates) at that load. This is true held-out load through the realized model, and exercises the clamp/overshoot in ngspice, not just numpy.
3. **Held-out frequency band.** Re-fit using only `f < 30 MHz` of the AC sweep, then score the fit on `f > 30 MHz` (the spur band 8/16/24 MHz partly held out). Tests whether the rational fits extrapolate in frequency or just track the dense grid.
4. **Different VDD.** Generate GT at a non-1.05 V supply (`measure_dc_linereg` exists; extend z/p to a 2nd vin) and score the model (whose PSRR/DC is baked at VREF=1.05, `fit_model.py:29`, R3). Quantifies the single-vin assumption.
5. **Residual whiteness test.** For each in-corner fit, autocorrelate the complex log-residual over frequency; structured (non-white) residuals indicate unmodeled poles being absorbed into measurement-tracking. (Cheap; flags artifact-fitting in the SK/AAA bank, `fit_model.py:215-383`.)
6. **Parameter sensitivity / condition number gate.** Emit cond(J) per block per corner (I computed it above); refuse to interpolate any param whose singular value is < 1e-3·σ_max (it is unidentified — interpolating noise). base/v1/v2 Zout would flag immediately (cond=∞).
7. **System acceptance (closes R4).** LDO+buffer at the carrier, tran + Xyce `.HB`, GT vs model sideband spectrum. This is the only test that measures the actual deliverable; all of the above are necessary precursors.

### C's one-line verdict

**Overfitting: YES (confirmed).** Evidence: leave-one-corner-out cross-validation shows a large in-sample-vs-out-of-sample error gap (PSRR 0.06–0.27 dB in-sample → **7.5–13 dB held-out** *[corrected from C's 66 dB by D's clean reproduction]*; Zout 0.1–0.5 → 1.4–13 dB), driven by (a) a 0-residual-DOF quadratic-through-3-corners interpolant that overshoots and even escapes the `_pexpr` clamp (v3_miller pcw0→1.245e8), (b) rank-deficient Zout fits (cond=∞ on base/v1/v2; 2.3e8 on v3) with boundary-pinned switch-parameters (R_pl, R_b) interpolated as if continuous, and (c) corner-dependent model-order switching (v2_capless 4-section→1-section). The test that **settles** it — and which the harness currently lacks entirely — is the leave-one-corner-out / off-grid-load cross-validation in plan items 1–2; the model **fails it today**.

---

# Perspective D — Synthesis verdict

I spot-checked the load-bearing citations from A/B/C by reading the actual files and re-running C's two executable claims (LOCO cross-validation and Zout-Jacobian condition numbers). The architecture-level citations all verified. C's qualitative overfitting conclusion reproduced robustly; two of C's specific magnitudes did not and are corrected below. Net: the three perspectives are converging on **one** root cause from three directions, with one genuine disagreement on severity that I adjudicate in D1.

### D1. Are the three doubts the same root cause? Where do they disagree?

**Yes — A, B, and C are three projections of a single defect: the model is only ever validated in-sample and block-level, never out-of-sample and never at the system level.** State it precisely as two coupled gaps:

- **Gap-1 (validation is in-sample):** every metric the project trusts is computed on stimuli/corners/frequencies the fitter already saw. `report.py` `predict()` uses the *identical* transfer functions the fitter optimizes (fit_model.py:497–520, verified — `zmodel`/`psrr_model`/`In·|Zout|` reused verbatim), so the GUI overlay *is* the fit residual, not an independent check. `score.py` re-sims via ngspice but only on `LOADS=["20u","121u","250u"]` / `AC="ac dec 40 10 100meg"` (bench.py:16,18, verified) — semi-independent (it catches `.lib`/`.va` realization bugs, which is real value) but blind to generalization. `_selftest` asserts `predict()==fitter analytic` (a tautology). **C proves this is not theoretical:** holding out one load corner and predicting it from the other two yields a 10×–100× error blow-up (my reproduction below).
- **Gap-2 (validation is block-level, not system-level):** the deliverable is coherent carrier-sideband fidelity (Zout×I_buffer ⊕ PSRR×V_ripple ⊕ intrinsic spur, vector-summed at the carrier). That scenario is never simulated for GT or model (R4). The "spur sanity gate" (score.py:161–166,248–250, verified) is a single 8 MHz *load* tone at a pass/fail −45 dBc threshold — it tests "is the model still linear" (which the abstraction *assumes*), not the deliverable.

These two gaps share a single physical locus, which B names correctly: **the irreducible kernel is one phase-accurate, OP-parameterized `Zout(s)`**, and it is the *least*-validated object in the whole system. All four blocks multiply `Zout` (fit_model.py:184/405/519/581 — verified at 511,514,519: `Z=zmodel(...)`, `H=psrr_model(...,zf,...)`, `Sv=√In²·|Z|`), so:
- a `Zout` error is **common-mode** across PSRR/noise/spur/transient (B's sharpest insight, and it directly refutes the word "decoupled" in fit_model.py:386 / report.py:221);
- the carrier-band `Zout` ceiling (500 MHz nominal-only, bench.py:30, verified; off-nominal 100 MHz, bench.py:18) zeroes **all four** outputs at a GHz carrier (R6#3);
- the load-interpolation overshoot (the 0-DOF quadratic, fit_model.py:598–602, verified) corrupts `Zout` between corners, which then leaks into the other three.

**So: A's "envelope violations," B's "under-validated kernel," and C's "overfitting" are the same statement at three altitudes** — physics (A: out-of-envelope conditions are unmodeled), architecture (B: the shared kernel is unguarded), and statistics (C: the interpolant generalizes terribly). They reinforce, they do not contradict, on the central claim.

**Genuine disagreements, adjudicated:**

1. **Severity of the noise "decoupling" framing.** B and C call it `observation`/`minor` (a mislabel; the round-trip `In=Sv/|Z|`→`Sv=In·|Z|` is correct physics, just not "decoupled"). A doesn't weigh in. **I side with B/C: observation, not a defect.** The physics is right (output noise *is* `Zout`-shaped); the only fix is to (a) stop calling it decoupled and (b) score `Sv` vs GT end-to-end rather than via the in-sample `_noise_resid` (fit_model.py:587), which uses `Zout_fitted` on both sides and therefore *cannot* surface a `Zout` error. This is a reporting-honesty issue, not a modeling error.

2. **Is the abstraction "conditional" (A) or does the deliverable "fail" (B/C)?** A says CONDITIONAL (physically correct inside a narrow envelope); B says blocker for Target B; C says overfitting confirmed/fails OOS. **These are not actually in conflict once you separate FORM from VALIDATION.** The 2-port linear *form* is correct (A and B agree, and A's UGB argument from PROJECT.md is sound — disturbances above the 1.78 MHz loop UGB see open-loop passive `Zout`, so LTI is the right call). The *validation* fails. My verdict: **the FORM holds (conditional on the stated envelope); the VALIDATION fails (unconditional).** Do not let A's "elegant abstraction" framing soften C's "it does not generalize as built" — both are true and they are about different things.

3. **R4 vs R6#3 priority.** B argues they are *one* root cause (kernel `Zout` at the carrier) and would promote R6#3 to blocker bound to R4. A treats them as related-but-distinct. **I side with B and sharpen it:** R6#3 ("no buffer ripple") is not an independent bug — it is the *symptom* you observe when the deliverable (R4's missing system test) is finally run on a part whose carrier exceeds the 500 MHz ceiling. They should be tracked as one item. C's plan-item-7 (LDO+buffer @carrier, GT vs model sideband) is the single test that closes both.

4. **C's specific OOS magnitudes — I CORRECT these.** C reported PSRR OOS up to **66.2 dB (v4)** and **33.2 dB (v3)**. My clean leave-one-corner-out reproduction (linear 2-point interpolant from the two retained corners — the only defensible interpolant when one of three corners is removed) gives **PSRR OOS 7.46 dB (v4), 1.80 dB (v3)**, and Zout OOS up to **13.0 dB (v1_nmos @20µ)**. The *qualitative* conclusion — a 10×–100× in-sample/out-of-sample gap on every variant — **reproduces and is decisive**; but C's largest figures (66/33 dB) are **not reproducible** with a clean LOCO and are overstated (likely an artifact of a quadratic-from-2-points or a corner-order-switch degeneracy in C's harness). **Adjudication: overfitting is CONFIRMED, but cite the reproducible 7–13 dB OOS gap, not 66 dB.**

### D2. Severity-ranked findings (corrected, with non-code recommendations + R-mapping)

**BLOCKER**

- **D2-1. No system-level (LDO+buffer @carrier) test — the deliverable is never measured.** Evidence: score.py:161–166,248–250 (only an 8 MHz single-tone pass/fail gate); confirmed no closed-loop TB anywhere. *Recommendation:* build a GT-vs-model sideband test — RF buffer drawing periodic carrier current on `vout`, run tran+`.HB`, compare the carrier±Δ sideband **complex** spectrum (magnitude AND phase, because the sideband is a coherent vector sum). Acceptance = sideband error within spec at the *actual* carrier, not 8 MHz. **COVERED-by-R4** (correctly the top item). *I agree R4 is the single most important gap.*

- **D2-2. Kernel `Zout(s)` is uncharacterized at the carrier for the GHz target (and extrapolated 100→304 MHz off-nominal).** Evidence: bench.py:30 (`AC_HF` 500 MHz, nominal only — verified), bench.py:18 (100 MHz off-nominal — verified); all four outputs ∝ `|Zout(fc)|` (fit_model.py:511–519 — verified). *Recommendation:* extend extraction to cover carrier ± widest sideband at *every* corner used in the system run; emit a hard validity-envelope and make the model **refuse to extrapolate** above the characterized ceiling rather than silently rolling to ~0. **EXTENDS-R6 (#3)** and **binds to R4.** *Mis-prioritization call: R6#3 is filed as a "concrete real-LDO bug"; it is actually a blocker-class kernel/coverage gap that is the same root cause as R4 — promote and merge.*

- **D2-3. Zout fit is rank-deficient with boundary-pinned switch-parameters interpolated as continuous.** Evidence (I re-ran it): cond(J) at nominal = **∞ for base, v1_nmos, v2_capless** (R_pl and/or R_b pinned at 1e9, smallest singular value 0.0), **2.7e8 for v3_miller**, **46 for v4_ffpsrr**. R_pl is a switch (v1_nmos: 7.8e6→54.5→53.7 across corners) yet is fed to a quadratic ln(i) interpolant (fit_model.py:598–602). *Recommendation:* gate interpolation on identifiability — compute cond(J)/singular values per param per corner; for any param whose singular value < ~1e-3·σ_max, **freeze it (do not interpolate noise)** and report it as unidentified rather than carrying a fictitious load-trajectory. **NEW** (beyond R1–R6; the condition-number gate is not in the ledger). *Caveat I add:* an unidentifiable param that barely affects `Zout` (an invisible high-ESR cap, fit_model.py:53–57) is low-harm; the *harmful* case is a switch (R_pl ON/OFF) interpolated through its transition — distinguish the two.

**MAJOR**

- **D2-4. Overfitting confirmed: 0-residual-DOF quadratic through exactly 3 load corners; 10×–100× in-/out-of-sample gap.** Evidence (re-run, corrected magnitudes): LOCO Zout OOS 1.6–13.0 dB vs in-sample 0.1–1.6 dB; PSRR OOS 1.8–8.6 dB vs in-sample 0.07–0.27 dB, on base/v1/v2/v3/v4. The `_pexpr` clamp (fit_model.py:611–626 — verified, with the verbatim "+15.7 dB" overshoot comment) bounds the *symptom* magnitude, not correctness. *Recommendation:* add LOCO cross-validation to the scorecard (build the interpolant from N−1 corners, score the held-out corner vs its GT) and add at least a 4th off-grid load corner (e.g. geometric mids 49µ/174µ) generated from the GT and scored through the *emitted* `.lib`. Acceptance gate: held-out RMS within ~2× of in-sample. **The model fails this today.** **NEW** (R1 covers step-size hardcoding, not held-out validation). *Correction to C: cite the reproducible 7–13 dB gap, not C's 66 dB.*

- **D2-5. Composite score under-weights phase, which the shared kernel propagates into every sideband.** Evidence: score.py:29–30 (verified) `zphase=0.04, pphase=0.03` vs `zband=3.0, pband=2.0`; resonance peak searched only `fz<1e7` (score.py:114 — verified). A model can score well while carrying 25° PSRR phase (V4) that vector-sums wrongly at the carrier. *Recommendation:* add a use-case-resolved scorecard (disturbance-mode weights PSRR phase at the carrier heavily; noise-mode weights LF/resonance band) — same single emitted model, multiple acceptance views; and make kernel-`Zout` phase a *pre-shell acceptance gate*, not a term mag can outvote. **EXTENDS-R4** (the missing system test is where phase actually bites).

- **D2-6. No VDD axis: PSRR/DC baked at single vin; `dc_linereg` characterized but consumed nowhere.** Evidence: fit_model.py:29 `VREF=1.05` (verified at 510–519 the DC node is pinned, not VDD-tracking); the user explicitly runs high/nom/low VDD (DEFERRED_REFACTORS.md:99–112). *Recommendation:* characterize PSRR and Zout at ≥2 vin and at least line-reg slope; wire `dc_linereg` into the DC anchor so the rail tracks supply. **COVERED-by-R2/R3** — but I flag **R3 as under-prioritized**: it is also the load-bearing cause of R6#2 (rail droop), not merely a small-signal nicety. The `idt()` inductor branches (fit_model.py:904,909,938) are a *secondary* contributor; the primary is the un-anchored DC node.

- **D2-7. Resonance migrates with load; only fit through 3 corners; mislocated where it matters.** Evidence: GENERALIZATION_REPORT pkf 5.0 on V3; for V2-capless the resonance is deliberately pushed into the 4–8 MHz spur band (variants.py:50–52 per A). Since spur ripple = I×|Zout(f)| and |Zout| has a sharp Q-peak, a mislocated peak is a direct multi-dB spur error. *Recommendation:* validate the resonance *trajectory* (peak f and Q vs load) against GT at an off-grid load, not just at corners. **EXTENDS-R1** (step-size/profile) into the resonance-location domain. *Note:* A calls this "blocker for resonance-in-band architectures." I agree for those architectures specifically; **major** in general (for Target A the resonance sits below the 8 MHz band and is partly masked).

- **D2-8. Passive minimum-phase `Zout` cannot represent actively-non-passive GT (Re Z<0).** Evidence: score.py:55–70,138–141 (verified — passivity reported as a diagnostic; GT may be non-passive); v3 GT minRe −0.23 per memory. *Recommendation:* this is a representational floor, not a bug — document it as a validity boundary and detect/flag when GT non-passivity exceeds what the passive RLC can absorb. **COVERED** (the code already reports it honestly; this is a strength, keep it). NECESSARY-not-SUFFICIENT, but correctly handled.

**MINOR / OBSERVATION**

- **D2-9. "Decoupled" noise is an algebraic round-trip, not physical decoupling.** Evidence: fit_model.py:519 `Sv=√In²·|Z|`, report.py:221 verbatim "Zout errors leak in here too." *Recommendation:* relabel; score `Sv` vs GT end-to-end (score.py:127 `_noise_metrics` already does this — good — but the fit-time `_noise_resid` at fit_model.py:587 is in-sample and self-consistent, so it under-reports). **NEW/observation.** Do NOT "fix" the round-trip — the physics is correct.

- **D2-10. Branch-B 40% gate and "prefer-complex-within-2×" PSRR selector are local heuristics, not information criteria; model ORDER switches across corners.** Evidence: fit_model.py:147 (`e2<0.6*e1`), 374–383 (verified — "within 2× of best AND ≤0.15," code self-describes as "not a pure data fit"); v2_capless uses different section counts at different corners per C. *Recommendation:* enforce a single model order per variant across all corners (interpolating params of a 4-section fit against a 1-section fit is interpolating between two different models), or use AIC/BIC for order selection. **NEW/minor** (order-switching), the heuristic-selector observation is **OBSERVATION** (it works, it's just not principled).

### D3. Direct verdicts on the user's three questions

**(1) Is the modeling abstraction + feedback path reasonable? — CONDITIONAL.**
The *abstraction* (linear 2-port `Zout`+`PSRR` + shaped Norton noise + discrete spurs + a separate nonlinear DC/dropout table) is **physically correct and the right form** for the stated Target-A regime — disturbances above the ~1.78 MHz loop UGB see the open-loop passive output network, so LTI is justified (PROJECT.md, A's argument, B's node-equation derivation, both verified-consistent). The *feedback path* is **not sufficient**: it validates blocks in-sample, never the system deliverable. Verdict: **abstraction holds (within a declared envelope); feedback path conditional — necessary but provably not sufficient** until a system-level, out-of-sample test exists (D2-1, D2-4).

**(2) Is it too greedy — split per use-case? — LAYERED (not split, not naively-unified).**
Splitting into independent per-use models is **wrong** for deployment: the system PSS/HB run drops *one* instance that must present `Zout`, `PSRR`, `noise`, and `spurs` *simultaneously* on the same node in the same analysis — you cannot swap models mid-simulation, and separate fits would let them disagree about the one physical `Zout` (unphysical). Naively-unified is **also wrong**: the single composite (score.py:29) lets magnitude outvote the phase that all shells inherit. The correct form is **layered: one shared, phase-first, identifiability-gated kernel `Zout(s)` + per-use shells, fit per-block (already the case) but emitted as one instance (already the case), with a use-case-resolved scorecard added.** B's analysis is correct and I adopt it: the code is *nearly* this already — the gap is that the shells are validated most and the kernel least. **Recommendation: keep unified deployment + per-block characterization; add a kernel-`Zout` phase/identifiability acceptance gate and a per-use scorecard.**

**(3) Is there overfitting? — YES (confirmed, reproduced).**
The settling test is leave-one-corner-out / off-grid-load cross-validation, which the harness lacks entirely. I re-ran it: in-sample 0.1–1.6 dB (Zout) / 0.07–0.27 dB (PSRR) vs held-out **1.6–13.0 dB (Zout) / 1.8–8.6 dB (PSRR)** — a 10×–100× gap on every variant, driven by (a) a 0-residual-DOF quadratic through 3 corners (fit_model.py:598–602), (b) rank-deficient `Zout` fits (cond=∞ on base/v1/v2, 2.7e8 on v3 — I reproduced these), and (c) boundary-pinned switch-params (R_pl/R_b) interpolated as continuous. **Correction:** C's headline 66 dB / 33 dB OOS PSRR figures did **not** reproduce under a clean LOCO (I get 7.5 / 1.8 dB); cite the reproducible 7–13 dB gap. The conclusion is unchanged: **overfitting is real; the model is not validated to generalize to any load, frequency, or VDD it was not handed.**

### D4. Prioritized "what to do next" (non-code, ordered)

1. **[NEW + COVERED-by-R4] Build the system test (LDO+buffer @ the real carrier), GT vs model, sideband complex spectrum.** This is the only test that measures the deliverable and the highest-leverage single action. Closes R4 and exposes R6#2/#3. *Merge R4 and R6#3 into one tracked item — same root cause (kernel `Zout` at the carrier).*
2. **[EXTENDS-R6#3] Extend `Zout`/`PSRR` characterization to cover carrier ± widest sideband at every corner used in the system run; emit a hard validity-envelope and refuse to extrapolate above the ceiling.** Without this, step 1 is meaningless for Target B.
3. **[NEW] Add LOCO + a 4th off-grid load corner to the scorecard, with an acceptance gate (held-out ≤ ~2× in-sample).** Promotes the overfitting test from "I ran it once" to a standing guardrail. The model fails it today.
4. **[NEW] Add an identifiability gate (cond(J)/singular values per param per corner); freeze unidentified params instead of interpolating noise; distinguish low-harm invisible params from harmful switch-params.** base/v1/v2 flag immediately (cond=∞).
5. **[COVERED-by-R2/R3 — re-prioritize R3 up] Add a VDD axis (≥2 vin for PSRR/Zout, line-reg slope) and wire `dc_linereg` into the DC anchor.** R3 is the load-bearing cause of R6#2 rail droop, not just a small-signal nicety — bump its priority.
6. **[EXTENDS-R4] Add a use-case-resolved scorecard (disturbance/noise/transient views) and make kernel-`Zout` phase a pre-shell acceptance gate.** Same single emitted model; fixes the mag-outvotes-phase defect (score.py:29).
7. **[NEW/minor] Enforce one PSRR/Zout model order per variant across corners (or AIC/BIC selection); relabel "decoupled" noise and score `Sv` vs GT end-to-end.** Cleanups, not blockers.

**Already covered by R1–R6 (do not re-report as new):** the system-test gap (R4), real-LDO symptoms poor-fit/droop/no-ripple (R6), step-size hardcoding (R1), `.va`/VDD interface (R2/R3). **Genuinely NEW from this audit:** LOCO/off-grid out-of-sample validation, the identifiability/condition-number gate, the per-use scorecard, and the model-order-consistency requirement. **Mis-prioritizations I assert:** R6#3 should be a blocker merged with R4 (not a standalone "concrete bug"); R3 should be raised (it causes R6#2); the noise "decoupling" mislabel is harmless and should NOT be "fixed."

### Citation spot-check

Verified by reading the file:
- **bench.py:16,18,30** — `LOADS=["20u","121u","250u"]`, `AC="ac dec 40 10 100meg"`, `AC_HF="ac dec 40 10 500meg"`. **Correct** (A/B/C all accurate).
- **score.py:29–30** — composite weights `zband=3.0, zphase=0.04, pband=2.0, pphase=0.03`. **Correct.**
- **score.py:114** — `lo = fz < 1e7` (resonance peak searched <10 MHz). **Correct.**
- **score.py:138–141, 55–70** — passivity guardrail; GT may be actively non-passive, passive model floor reported as diagnostic. **Correct** (A's "admitted floor" framing accurate).
- **score.py:159** — big/slew re-sim hardcodes `iload=121e-6`. **Correct.**
- **score.py:161–166, 248–250** — single 8 MHz load tone, pass/fail at −45 dBc. **Correct** (A's "unrepresentative gate" accurate). One nuance: the GT tone uses `spur_500u` (500µA drive), confirming it is a linearity sanity check, not the deliverable.
- **fit_model.py:85–96** — `zmodel` = `(R_a+sL_a||R_pl)||(R_b+sL_b)||(ESR+1/sC)`, up to 5 params. **Correct.**
- **fit_model.py:497–520** — `predict()` reuses `zmodel`/`psrr_model`/`In·|Zout|`; docstring says "the GUI's before/after overlay IS the fit quality itself." **Correct** (C's in-sample claim accurate, verbatim).
- **fit_model.py:519** — `Sv=√In²·|Z|` (noise rides fitted `Zout`). **Correct** (B/C "round-trip" accurate).
- **fit_model.py:598–602** — `np.polyfit(u,y,2)` through 3 corners, 0 residual DOF. **Correct.**
- **fit_model.py:611–626** — `_pexpr` clamp to `[min/1.5, max*1.5]`, verbatim "+15.7 dB" overshoot comment. **Correct.**
- **fit_model.py:374–383** — PSRR "prefer complex within 2× of best AND ≤0.15," self-described "not a pure data fit." **Correct.**
- **fit_model.py:53–57** — Cout/ESR weak identifiability admission ("V1 reads ~381pF for true 1nF"). **Correct.**

Re-ran (executable claims):
- **C2 condition numbers — CONFIRMED.** cond(J) at nominal: base/v1_nmos/v2_capless = **∞** (σ_min=0, R_pl/R_b pinned at 1e9), v3_miller = **2.7e8**, v4_ffpsrr = **46**. Matches C. Minor note: at nominal, v1_nmos has R_pl=54.5 (not boundary) and R_b=1e9 (the dead param); C's "R_pl switch" is still correct across corners (7.8e6→54.5→53.7).
- **C5 LOCO gap — CONFIRMED qualitatively, magnitudes CORRECTED.** Reproduced 10×–100× in-/out-of-sample gap on all of base/v1/v2/v3/v4 (Zout OOS 1.6–13.0 dB, PSRR OOS 1.8–8.6 dB). **C OVERSTATED** the largest PSRR figures: C reported 66.2 dB (v4) and 33.2 dB (v3); clean 2-point LOCO gives **7.5 dB and 1.8 dB**. The overfitting conclusion stands; the specific 66/33 dB numbers should not be cited.
- Not independently re-derived (cited from A/B/C as plausible, reading-verified where files exist): GENERALIZATION_REPORT pkf 5.0 / pphase 25° (in-sample residuals — consistent with the in-sample numbers I reproduced); variants.py:50–52 resonance-in-band for V2 (A's claim, not re-run); R4/R6 line numbers in DEFERRED_REFACTORS.md (not opened — but the substance, no system test and the three symptoms, is consistent with the verified score.py/bench.py state).

---

## 下一步该做什么(非代码,按优先级排序)

> 全部为**方法/测试层面**建议,不含任何对现有实现代码的改动指令。

1. **[最高杠杆 · 对应 R4]** 搭建**系统级验收测试**:LDO + 在载波处取周期电流的 buffer,GT 与模型各跑一遍 tran + `.HB`,对比 **载波±Δ 边带的复数频谱(幅度 + 相位)**。这是唯一直接测量"真正交付物"的测试。把 **R4 与 R6#3 合并为同一条目**(同根因 = 载波处的内核 `Zout`)。
2. **[对应 R6#3 的延伸]** 把 `Zout`/`PSRR` 表征**扩展到 载波±最宽边带**,且在系统运行用到的**每一个**负载角都要覆盖;发出一份**硬性 validity-envelope**,让模型在超出已表征上限时**拒绝外推**(而非静默滚降到 ~0)。否则第 1 步对 Target B 无意义。
3. **[本次审计新增]** 把 **LOCO(留一角)交叉验证 + 第 4 个离网格负载角(如几何中点 49µ/174µ)** 纳入打分卡,设验收门(留出误差 ≤ 样本内 ~2×)。模型**今天就过不了**这一关。
4. **[本次审计新增]** 增加**可辨识性门**(每块每角算 cond(J)/奇异值);奇异值 < ~1e-3·σmax 的参数**冻结、不插值**(否则是在插值噪声);区分"低危的不可见参数(高 ESR 隐形电容)"与"有害的开关参数(R_pl 的 ON/OFF 被当连续插值)"。base/v1/v2 会立刻报警(cond=∞)。
5. **[对应 R2/R3 · 建议提升 R3 优先级]** 加 **VDD 轴**(≥2 个 vin 表征 PSRR/Zout + line-reg 斜率);把当前**未被任何代码消费的 `dc_linereg`** 接入 DC 锚定,使输出轨随供电变化。R3 是 **R6#2(轨道缓降)的主因**,不只是小信号细节。
6. **[对应 R4 的延伸]** 增加**按用途解析的打分卡**(扰动模式/噪声模式/瞬态模式各自的视图),并把内核 `Zout` 的**相位**设为**外壳之前的验收门**(而非可被幅度项盖过的低权重项)。同一个被部署的模型,多个验收视角。
7. **[新增/次要]** 强制每个变体**跨角使用同一模型阶数**(或用 AIC/BIC 做阶数选择)——把 4 节复数 PSRR 的参数与 1 节实数 PSRR 的参数插值在一起,等于在两个不同模型之间插值;同时**给"解耦噪声"改名**并**端到端用 `Sv` vs GT 打分**(物理是对的,不要去"修"那个代数往返)。

**已被 R1–R6 覆盖(勿当新发现重报)**:系统测试缺失(R4)、真实 LDO 症状 拟合差/轨道缓降/无 ripple(R6)、step-size 硬编码(R1)、`.va`/VDD 接口(R2/R3)。
**本次审计真正新增**:LOCO/离网格 样本外验证、可辨识性/条件数门、按用途打分卡、模型阶数一致性要求。
**主张的优先级修正**:R6#3 应升为 blocker 并与 R4 合并;R3 应提升(它导致 R6#2);噪声"解耦"误称无害、**不应**去"修"。

---

*报告生成方式:三视角并行子代理 fan-out(A/B/C 独立成文)→ D 综合并逐条复核 file:line 证据、重跑 C 的可执行声明(LOCO、SVD 条件数)→ orchestrator 已独立通读 fit_model/score/bench/gen_reference/spur_char/variants/report/import_cadence 全部源文件,可为架构级引用背书。本次审计全程零源码改动。*
