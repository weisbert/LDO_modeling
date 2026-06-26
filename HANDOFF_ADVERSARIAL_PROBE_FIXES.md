# Handoff — adversarial overfit-probe FIXES + extensions (next ultracode session)

**State (DONE, committed `e32ce06`, pushed):** 8 adversarial GT DUTs built+run+exposed; meta-finding
confirmed (4/8 — A4 classab / B2 double_cascode / B3 bias_flip / B4 tempload — fail where the existing
harness is blind, only the 5 new gates catch them); bonus baseline finding (v8_wilson B4 139 mV).
5 new gates shipped, all OBSERVATIONAL + synthetic-lock-tested (`harness/test_adv_probe_gates.py`, 14).
No-regression: `crossval_isrc` 8/8 baselines, `score.py` composite byte-neutral, full `pytest harness/`
= 145 passed. Full write-up: `ADVERSARIAL_OVERFIT_PROBE_RESULTS.md`. Spec: `HANDOFF_ADVERSARIAL_OVERFIT_PROBE.md`.

The two items below are the only genuine IMPERFECTIONS (caveats). Everything else worked. The two
EXT items are optional coverage/productionization, not fixes.

---

## FIX-1 — B1 clean blind-spot  *(highest-value real fix; effort: medium-high, uncertain)*

**Problem.** `isrc_inflect_ctat_ptat` (B1) is monotonic-convex Idc(T) (0.59/0.92/1.56 µA at -40/55/125),
so it ALSO trips the EXISTING scalar gate (idc_err 16%, ptat_err 0.158) — it is caught by both, not a
*clean* blind-spot. The whole point of B1 is a defect the existing endpoint/TNOM gate is BLIND to.

**Goal.** A B1 DUT where the existing `crossval_isrc` PASS gate FULLY passes (idc_err<2% AND ptat_err<0.03)
yet the new `gate_heldout_idc` fires (interior 25/85 °C miss >5%).

**Why the current one fails it (math).** A pure-convex curve sampled at 3 temps has its middle point
BELOW the -40↔125 chord → the linear fit misses TNOM=55 → idc_err fires. To pass the existing gate the
3 fit-temps (-40, 55, 125) must be ~COLLINEAR in Idc(T) (so idc55 + ptat-ratio match) WHILE 25 and 85
deviate from that line. That needs an Idc(T) with an INFLECTION (S-shape or off-center-U) crossing a
line at the 3 fit temps — NOT a monotonic-convex sum.

**Mechanism to try.** Needs a genuine CTAT branch to balance the PTAT so the curve bends back (cross
near ~40 °C so the dip sits between fit temps). Plain-MOS CTAT (Vgs/R) self-bias loops latched for the
build agents — explore: a diode Vgs (CTAT, ideal-current-biased) buffered onto a resistor via a source
follower; or two PTAT/CTAT branches tuned so { Idc(-40)=Idc(125) within ~3%, Idc(55) on the chord,
Idc(25)&Idc(85) off by >5% }. Iterate with `harness/adv_probe_char.py isrc` (prints all 5 temps +
the line-miss) AND `crossval_isrc.crossval(name)` (must show ok=True AND gate B1 exposed=True).

**Acceptance:** `crossval_isrc` row `ok==True` (existing gate fooled) AND `gates.heldout_idc.exposed==True`.
If a robust collinear-fit-temps + curved-interior current is NOT achievable in this PDK after honest
iteration, KEEP the current monotonic-convex DUT and document it (the B1 gate is already validated).

---

## FIX-2 — qbow strict A1 spec  *(judgment call; effort: ~0 doc, or small for the Q45 path)*

**Problem.** `ldo_qbow` exposes (composite 18.96, pkdb 15.8 dB, offgrid FAIL, structloco PSRR-cpx FLIP)
but the literal A1 peak-ratio spec (peak|Z|@121µ ≥3× BOTH the 20µ AND 250µ sides) was only half-met
(250µ side 1.89×). The build agent PROVED a real tension: the only config that hits 3.33×/3.15× (lm=1u
long-mirror) forces mid-corner Q≈45, which trips the bringup HIQ gate (Q>30 → FAIL). Sharp-Q-bow vs
Q≤30-stability are jointly infeasible on this 5T-OTA/PMOS-pass topology.

**Options (DECISION NEEDED from user):**
- **(a) ACCEPT + document** — qbow already exposes via structural-representation (the 2-branch RLC can't
  hold the non-monotonic-Q bow); the falsifiable A1 finding is met by a different channel. *Recommended,
  ~0 effort.*
- **(b) Per-probe HIQ override** — add a per-variant `bringup` HIQ threshold (raise to ~50 for qbow),
  re-tune qbow to the lm=1u config → hits the literal 3×/3× + Q45. Accepts a genuinely marginal (Q45) GT.
  Small effort. Only do this if the literal peak-ratio spec must be satisfied.
- **(c) Relax the A1 acceptance** to "non-monotonic Q + one-sided 3×" (which IS met) and document.

---

## EXT-1 — §E alternate DUTs  *(optional coverage; ~1 ultracode round per 2-4)*

Specced in `HANDOFF_ADVERSARIAL_OVERFIT_PROBE.md` §E: `ldo_ghostcap`, `ldo_rhpz`, `isrc_softknee`,
`isrc_vbias_noise`. Highest marginal value = **`isrc_vbias_noise`** (flicker corner walks with compliance
Vo) because `crossval_isrc` has NO current-noise gate at all — it would add a 5th current-path gate
(a real coverage gap). Each follows the same build→register→gate→lock pattern as round 3.

## EXT-2 — productionize the gates  *(the deferred phase-2; effort: medium + 1 model decision)*

All 5 gates are currently OBSERVATIONAL (per the chosen "observe-first" policy). Phase 2 = fold the
validated gates INTO the verdict (`crossval_isrc.ok` / `score` composite) so they protect future fits.
BLOCKER to decide first: **v8_wilson's real 139 mV B4 knee shift would then FAIL a baseline** — choose
(i) extend the model with a T-dependent knee/g0 DOF (real fix, more work), or (ii) accept v8 as a
documented known-limit / raise the B4 bar above 139 mV. Do NOT productionize until that is settled.

---

## Recommended order
1. **FIX-2 (a)/(c)** — a doc decision, settle it first (zero/low effort).
2. **FIX-1** — the one real engineering fix worth attempting (makes the meta-finding 4/4 clean). Time-box
   the CTAT search; fall back to documenting if the PDK won't cooperate.
3. **EXT-1 `isrc_vbias_noise`** if you want the 5th gate (noise-vs-Vo coverage gap).
4. **EXT-2** only after deciding the v8_wilson model question.

Reproduce / verify any change: `python3 harness/crossval_isrc.py`, `python3 harness/run_matrix.py <ids>`,
`python3 harness/crossval.py --variant <id>`, `python3 -m pytest harness/test_adv_probe_gates.py -q`.
No-regression bar: `crossval_isrc` stays 8/8 on baselines, `pytest harness/` stays green, the 14 voltage
+ 8 current baselines unchanged (add variants/gates ADDITIVELY only).
