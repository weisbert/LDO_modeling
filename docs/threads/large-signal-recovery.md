# Thread: large-signal load-transient / recovery (WuR PLL rail) — ACTIVE

owner: weisbert · last-touched: 2026-06-30 · supersedes the archived HANDOFF_RECOVERY_STDFLOW / EMIT_BAKE / DECAP_LTI

## Where it stands
Modeling the PLL-rail load transient FROM THE STANDARD FLOW (higher-order LTI Zout), not a hand-tuned recovery net.
- **Zout ladder STEP-2 SHIPPED** (`ab21b83`) — pll `extra=[(4.42µH,155Ω)]`, vco `[(1.23µH,50Ω)]`; |Z| err ≤0.34 dB @1/10/31.6 MHz; byte-identical when `extra` absent.
- **Minimal-emit SHIPPED** (`6833c61`) — all fitted params baked to `localparam`; only `vreg_<rail>` exposed.
- **"30 mV startup drop" RESOLVED + CLOSED** (2026-06-30, 3-expert panel + user fix). It was the DC-OP initial charge of the 22.9 µH/534 ns branch-A fit-inductor (preload 37→405 µA ⇒ drop 55→4.8 mV). **User resolved it TB-side**: output iload redesigned as a STEP 600µA→300µA (the 600µA bakes the steady loading into the DC-OP → pre-charges the inductor → no phantom slew). Caveat to keep in the TB: stimulus-side fix; the big-inductor IC fragility remains — record the 600µA choice.
- **Slew core RETIRED** (2nd expert panel, unanimous): slew is WRONG-SIGN for the coverage dip; manifest knobs removed (`7b76d8b`). real_V (88 mV @25 ns) is a cold-start envelope (corr(I,V)≈0), held-out reference only.

Numbers: DATA.md §5. Verdicts/why: METHODOLOGY §"Large-signal / recovery".

## Next action (BLOCKED on user decisions)
Decide before any build:
1. **Build PART2** (compressive branch-A current-assist `i_assist=f(verr)`, f odd/compressive, f'(0)=0 → AC bit-identical; 2 params fit Spectre-in-loop, held-out fit 2m+4m predict 3m)? This is the right fix for the GT sub-linear stiffening dip (163.6 mV/mA model LINEAR vs GT sub-linear 107/91/81). Caveat: its gain absorbs the T25(z)/T55(step) offset (~×0.65) → re-calibrate via a cheap T55 z re-export.
2. Is the **88 mV startup** required for fast system sim, or excludable? Does the TB apply an EN edge / engage load at t=0 (→ is 88 mV real silicon or a sim-IC artifact)?
3. Confirm temps: real_V=T25? coverage=T55? z=T25? (the 25/55°C confound on the dip).

## Checklist
- [ ] User answers Qs 1–3 above
- [ ] (if PART2 GO) build compressive i_assist, Spectre-in-loop, held-out across amplitudes
- [ ] B5: guarded transient fit on the VCO once PLL works
- [ ] (optional) same-temp 55°C z_pll re-export to kill the T-confound
