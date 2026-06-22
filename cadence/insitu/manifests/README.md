# Manifests — which file is which

A *manifest* is the single JSON that describes one DUT for the extraction flow (its supplies,
voltage/current outputs, bias ports, the AC/noise analyses, and the coverage sweeps). Several
files live here for different purposes — this note says which to use.

## Use this one (the real circuit)

- **`REAL_wur_pmu_top.json`** — the real Hi1108 WuR PMU (`Hi1108_WuR_PMU / WuR_PMU_TOP`, TB
  `sim_LDO`). Ground `VSS_PLL`. Carries the prefilled coverage (per-rail load points, load-step
  slew, per-sink I-V sweeps). **This is the one to load in the GUI and run.** Nets are still
  `<net:PIN>` placeholders — run *Scan netlist* (or Mode A) to resolve them against your TB.

## Reference / test files — do **not** run these as your design

- `wur_pmu_top.json` — the SAME circuit but kept **coverage-free** as the shared unit-test
  fixture (5 test files load it and assert the clean T0 matrix). Don't add coverage here or the
  suite breaks; edit `REAL_wur_pmu_top.json` instead.
- `pmu_top.json` — an old development sample (`sim_yusheng / PMU_top`). Used by a couple of
  tests. Not your circuit.
- `pmu_real.json` — the **golden template** that `build_manifest` emits for a fresh DUT: every
  field is a placeholder (`<dut_lib>`, `<net:PIN>`, ground `VSS`). It exists so the builder
  output never drifts from the shipped template (`test_build_manifest`). It is a skeleton, not a
  runnable design.

## Making your own

Build a fresh manifest from the GUI (it produces the `pmu_real.json`-shaped skeleton with your
pins), then fill the nets via *Scan netlist*. Bare names resolve here, e.g.
`python -m insitu doctor --manifest REAL_wur_pmu_top`.
