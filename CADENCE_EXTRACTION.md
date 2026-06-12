# Cadence extraction → data contract (Target B handoff)

> This is the **v1** contract (consumed by `cadence/import_cadence.py` today). Proposed v2
> extensions — bias-current output pins + the PVT matrix — and the auto-collection brief for
> the Cadence-VM agent live in `CADENCE_AUTOCOLLECT.md`.

This is the **interface** between Cadence-side extraction of a *real* transistor-level LDO and the
behavioral-model harness in this repo. If the Cadence side emits exactly the arrays below, bringing
the real LDO in is just: `cd harness && python fit_model.py --variant <name> && python score.py --variant <name>`.

The harness consumes **one file**: `results/ref/<name>.npz` (numpy `savez`). CSVs are also fine — the
`import_cadence.py` converter (CSV → npz) will be written at integration time, once the actual export
column layout is known. The schema below is the source of truth (it mirrors `harness/gen_reference.py`).

## Setup
- Everything at the LDO's **nominal vin** and **exactly 3 load corners** that bracket the operating
  load (the model interpolates each parameter quadratically in `ln(iload)`, so 3 corners — low / nom / high).
- Array names embed the corner key from `loads`, e.g. `z_121u`, `noise_20u`.
- Spectre is the right tool (real `pac`/`pxf`/`pnoise`/`pss`); just emit the arrays.

## Arrays (`results/ref/<name>.npz`)

| array | shape | content / units |
|---|---|---|
| `loads` | `[3]` str | corner keys, e.g. `["20u","121u","250u"]` |
| `z_{il}` | `[N,3]` | Zout: `f[Hz], Re, Im`. **Z = V(vout)/I**, 1 A AC into vout, vin = ideal DC. AC 10 Hz–100 MHz |
| `p_{il}` | `[N,3]` | PSRR **transfer** H = V(vout)/V(vin): `f, Re, Im` (store COMPLEX, not dB; 1 V AC on vin) |
| `noise_{il}` | `[M,2]` | output noise PSD: `f, Sv[V/√Hz]`. vin = ideal DC ⇒ intrinsic LDO noise only (sqrt if Spectre gives V²/Hz) |
| `z_121u_hf` | `[N,3]` | Zout for the **nominal** corner extended to **500 MHz** (bounds the RF carrier; ALSO drives Cout/ESR auto-extraction — required) |
| `p_121u_hf` | `[N,3]` | PSRR for the nominal corner extended to 500 MHz |
| `trans_lin_{il}` | `[T,2]` | small load step `t[s], vout[V]`: +0.3·bias, 1 ns edge up @5 µs / down @15 µs, tstop 25 µs |
| `trans_big_121u` | `[T,2]` | 1 mA step at nominal (gm-compression onset) |
| `trans_slew_121u` | `[T,2]` | 5 mA step at nominal (slew/dropout diagnostic) |
| `dc_loadreg` | `[L,2]` | `iload[A], vout[V]` load-regulation sweep |
| `dc_linereg` | `[V,2]` | `vin[V], vout[V]` line-regulation sweep |
| `dc_dropout` | `[D,2]` | `iload[A], vout[V]` swept into dropout (~to 6 mA) |
| `spurs_{il}` | `[K,3]` | intrinsic spurs at vout: `f[Hz], amp[V], phase[rad]` — **no external stimulus** (PSS / transient-FFT) |
| `spur_F` | `[K]` | intrinsic fundamental freqs [Hz] (on-chip osc/charge-pump/clock); **empty if none** |
| `spur_twin0` | scalar | FFT window-start time [s] (the phase reference for `spurs_*` phases) |
| `spur_binhz` | scalar | FFT bin width [Hz] |
| `spurs_raw_{il}` | `[T,2]` | **alternative to `spurs_*`**: a RAW intrinsic transient `t[s], vout[V]` (no stimulus), one per corner. If you give these *instead of* the `spurs_*` tables, `import_cadence` auto-FFTs them (coherent window → peak-pick → fundamental classification) and fills `spurs_*` / `spur_F` / `spur_twin0` / `spur_binhz` for you. Export the plain waveform — no calculator FFT in Cadence. Window via `--spur_tstart`, band via `--spur_fmax` (default skips first 20%, scans to 30 MHz). |
| `meta_cout`, `meta_esr` | scalar | design Cout / ESR (self-consistency check; `nan` ok) |
| `ibp_xfer_{il}` | `[N,3]` | *optional* bias-port → vout transimpedance `f, Re, Im`; omit if no bias port |
| `spur_500u` | `[F,2]` | *optional* 8 MHz load-tone FFT `f, amp` (linearity sanity gate) |

Frequency grids need not match the harness exactly (the scorer interpolates), but cover 10 Hz–100 MHz
(and the two `*_hf` arrays to 500 MHz).

## Conventions that cause silent mismatches
- **PSRR is the complex transfer** vout/vin (the harness takes −20·log₁₀|H| itself), NOT attenuation-in-dB.
- **Zout** = driving-point V/I at vout with vin held by an ideal source.
- **Noise**: input held ideal so only the LDO's own noise appears; amplitude PSD `V/√Hz` (not V²/Hz).
- **Spurs are intrinsic only.** External supply/bias spurs are NOT extracted: in the system testbench
  you inject the aggressor tone at the model's `vin` and the PSRR path carries it to vout.
- All extractions at the same nominal vin as the OP the model represents.

## Three things to report at handoff (not derivable from the netlist)
1. **Nominal vin** — the model currently hardcodes 1.05 V as the PSRR reference (`Vrf`); if different,
   it gets parameterized.
2. **The 3 load corners** (and which is nominal).
3. **Design Cout / ESR.**

## Equivalent Spectre analyses (mapping)
`ac` (1 A into vout) → `z_*`; `ac` / `pxf` (vin → vout) → `p_*`; `pnoise` (out=vout) → `noise_*`;
`tran` PWL load steps → `trans_*`; `dc` sweeps → `dc_*`; `pss`+`pac`/FFT of the free-running output →
`spurs_*` / `spur_F`. (See `harness/gen_reference.py` for the ngspice equivalents we used for Target A.)

## After handoff (done in this repo)
Write `import_cadence.py` (CSV→npz) if needed → `fit_model.py` fits Zout (RLC) + PSRR (real bank +
complex-conjugate non-min-phase section) + decoupled Norton noise + discrete spur tones, emits
`.lib` + `.va`. Validate beyond ngspice (no PSS/HB locally): **Xyce** multi-tone `.HB` for 304 MHz
sideband asymmetry (hot-S), **OpenVAF→.osdi** + **VACASK** to HB-check the `.va`.
