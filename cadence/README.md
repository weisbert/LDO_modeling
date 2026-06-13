# Target B — Cadence/Spectre bring-up

Spectre-side counterpart of the ngspice harness. Validates the behavioral-LDO pipeline
(characterize → fit → emit `.lib`/`.va` → score) under **Spectre 18.1**, so the emitted
Verilog-A model is proven in the same tool the production PMU sims use. ngspice is NOT
installed on this box; everything here is Spectre-native.

## Quick start
```bash
source cadence/env.sh                      # Spectre 18.1 + IC618 env (mirrors live Virtuoso)

# Phase 1 — VA round-trip (emitted model is the DUT; validates the pipeline vs a known answer)
python cadence/extract_ref.py --dut va --name va_rt   # Spectre-characterize model/ldo_model.va -> results/ref/va_rt.npz
python harness/fit_model.py   --variant va_rt         # fit -> model/ldo_va_rt.{lib,va}
python cadence/score_spectre.py --variant va_rt       # Spectre-score -> composite (~0.2)

# Phase 2 — transistor GT cross-sim (any architecture from harness/variants.py)
python cadence/run_variant.py base                    # extract+fit+score ldo_gt -> JSON {composite,...}
python cadence/run_variant.py v4_ffpsrr               # non-min-phase PSRR variant, etc.
```

## Files
| file | role |
|---|---|
| `env.sh` | Spectre 18.1 + IC618 environment (PATH, CDS_LIC_FILE). `source` before manual runs. |
| `psf.py` | psfascii reader (ac/noise/dc/tran) → `{signal: ndarray}`. |
| `spectre_run.py` | runs `spectre -64 -format psfascii`; sets env itself (no sourcing needed in-process). |
| `spectre_bench.py` | Spectre measurement bench, **NET/PIN-parameterized** (DutSpec). Mirrors `harness/bench.py`'s API + returned arrays. `va_dut()` / `spice_dut()`. |
| `extract_ref.py` | Spectre `gen_reference.py`: writes `results/ref/<name>.npz` per `CADENCE_EXTRACTION.md`. `--dut va` / `--variant <key>`. |
| `bench_spectre.py` | adapter exposing `bench.py`'s `measure_*(lib,subckt,il,xparams)` API, Spectre-backed (so `score.py` is reused verbatim). |
| `score_spectre.py` | reuses `harness/score.py` metrics with the Spectre backend → composite. |
| `run_variant.py` | one-shot extract→fit→score for a variant; emits one `RESULT_JSON` line. |
| `import_cadence.py` | **contract converter**: ADE/OCEAN PSF tree OR manual CSV → `results/ref/<name>.npz`. `assemble()` is the single schema writer; CSV path is the manual-TB fallback (layout in its docstring). |
| `skill_lib.py` | reusable skillbridge driver: `ensure_lib()` (ddCreateLib), `import_va_cellview()` (emit `.va` → Cadence veriloga cellview), `list_cells()`. |
| `skill/ldo_cellview.il` | CIW-loadable SKILL twin of `skill_lib.py` (`ldoEnsureLib` / `ldoImportVA`) for in-Virtuoso use. |
| `cds/LDO_model_lab/` | the Cadence library (created by skillbridge): holds the `ldo_model` veriloga cellview. |

## Phase 3 — skillbridge productionization (DONE)
```bash
source cadence/env.sh
python cadence/skill_lib.py            # ddCreateLib LDO_model_lab + import model/ldo_model.va as a veriloga cellview
# manual / ADE fallback: export the contract quantities to CSV, then
python cadence/import_cadence.py csv <csvdir> --name mychip --cout 1n --esr 0.5
python harness/fit_model.py --variant mychip      # fit the imported data
```
- `LDO_model_lab/ldo_model/veriloga` is a valid, simulatable Cadence cell (verified: characterizing it
  reproduces the Phase-1 round-trip, PSRR sign correct).
- CSV manual fallback verified end-to-end: array-equality vs the source npz, and fit-compatible.
- **OCEAN note:** the standalone `ocean` binary is broken on this box (its `sysname` OS check returns
  "unknown" on this RHEL8/4.18 kernel and aborts — the live Virtuoso tolerates it, standalone ocean
  doesn't). Characterization is therefore driven via the validated spectre path, which uses the **same
  SPECTRE181 engine** ADE/OCEAN would. For Phase 4 in-situ, run the contract analyses in the user's ADE
  session (which works) and feed the exported PSF/CSV through `import_cadence.py`.

## Spectre gotchas baked in (all caught during bring-up)
1. **`spectre -64` is mandatory.** Default 32-bit mode compiles the Verilog-A CMI with `gcc -m32`
   and dies on missing `gnu/stubs-32.h`. 64-bit compiles it clean. (`spectre_run.py` always passes `-64`.)
2. **BSIM3 level map:** ngspice `level=8` (BSIM3v3.3.0) → Spectre's BSIM3v3 is **`level=49`**
   (`level=8` → generic `mos8`, rejects BSIM3 params). `spice_dut` remaps 8→49 inline; committed `.mod` untouched.
3. **Brace subckt params:** Spectre's spice reader does not substitute `{param}`; a bare param name does.
   `spice_dut` strips `{ident}`→`ident` in subckt bodies.
4. **Lang scope:** spice subckts are instanced in spice-lang (`xdut ...`), then stimuli in spectre-lang;
   top-level nets bridge the two.
5. **`.va` PSRR sign:** `I(vout)<+ X` removes current FROM vout while the `.lib` mirror injects INTO it —
   the emitted PSRR was inverted (180°). Fixed in `harness/fit_model.py:emit_va` (negate the PSRR `I(vout)`
   contributions) + the committed `model/ldo_model.va`. Other `model/*.va` still need regenerating.

## Validation status (CLI, this box)
- **Phase 1 (VA round-trip):** composite **0.2** (numerical floor); caught+fixed the `.va` PSRR sign bug.
- **Phase 2 (GT cross-sim):** Spectre composite **3.8** vs ngspice **3.9** (base); BSIM3 Zout/PSRR/noise
  match ngspice to ≤0.006 dB (1/f noise to 0.000 dB). Full 12-architecture matrix: see HANDOFF.
