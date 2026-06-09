# Compiled-VA end-to-end: does the emitted stimulus .va build the model?

The productionized trans-ID, proven through the REAL toolchain: the multitone stimulus `.va` (trans_id.emit_stim_va) is COMPILED with OpenVAF, run in ngspice via OSDI to drive each GT DUT, imported with trans_import.py, then fit+scored vs the AC ground truth. A `d_path ~ 0` means the compiled-VA fixture reproduces the validated B-source recipe; `d_AC` is the model-quality gap vs the AC-built model (same as validate_trans_id).

**Toolchain:** OpenVAF `C:\code\LDO_modeling\tools\openvaf\openvaf.exe` + linker `C:\Users\Yusheng\anaconda3\envs\vatools\Library\bin` + MSVC libs `xwin-splat`.

| variant | composite AC | VA-trans | d_AC | d_path (vs B-source) | GUI import | Cout VA/true pF | compile s | run s |
|---|---|---|---|---|---|---|---|---|
| base | 3.88 | 3.94 | +0.06 | -0.00 | OK | 999/1000 | 0 | 12 |
| v3_miller | 6.32 | 8.46 | +2.14 | -0.04 | OK | 997/1000 | 0 | 13 |
| v2_capless | 6.81 | 9.41 | +2.60 | -0.00 | OK | 130/100 | 0 | 13 |
| v1_nmos | 8.85 | 8.18 | -0.68 | -0.00 | OK | 474/1000 | 0 | 12 |

## Level 1 -- per-frequency recovery (compiled-VA trans vs AC), worst corner

| variant | corner | Zout dB med/max | PSRR dB med/max | n(z,p) | leak dB |
|---|---|---|---|---|---|
| base | 250u | 0.01/0.39 | 0.02/0.22 | 60,63 | -122 |
| v3_miller | 20u | 0.09/0.37 | 0.03/0.23 | 60,63 | -100 |
| v2_capless | 20u | 0.18/0.45 | 0.18/18.99 | 60,63 | -73 |
| v1_nmos | 250u | 0.00/0.27 | 0.00/0.08 | 60,63 | -131 |

## Verdict

- **All 4 stimulus .va files COMPILED** with OpenVAF and RAN in ngspice via OSDI (the '.va must compile, not just be written' constraint -- satisfied through the real toolchain).
- **max |d_path| = 0.04** vs the validated B-source dev path -- the compiled-VA fixture reproduces the recipe (the residual is FFT/timestep numerical noise, not a method difference).
- **GUI import path consumes the importer CSVs on all variants** (import_cadence.assemble -> npz with z/p/noise per corner).
- Noise stays a separate .noise (a deterministic .tran has no device noise); the trans-derived ref reuses the AC noise verbatim.