# HANDOFF — Coverage-Driven Modeling: BUILT + locally validated

**Status (2026-06-19):** the full coverage-driven modeling feature from `HANDOFF_MODELING_COVERAGE.md`
(the locked plan, commit `75e2188`) is **BUILT, committed, and pushed to `main`**, and **validated on
the LOCAL Spectre 18.1 + ngspice** this session. `267` backend tests + GUI selftest green.

---

## 1. What was built (6 commits on main, each = 1 ultracode workflow + adversarial verify, all PASS)

| commit | stage | content |
|---|---|---|
| `dcac502` | ①a measurement contract | manifest `coverage` section (tier T0–T4 + `enable` + per-rail `loads`/`transient`/`iv`/`dropout`/`temps`/`lin_gate`/`slew_en`); coverage-gated `measurements()` appends iv/dropout-dc/tran/z2 points; `run.groups` groups them. **INVARIANT: no coverage params ⇒ byte-identical 8/10 groups.** |
| `637274d` | ①b primitives | `netlist_augment` emits dc/tran/2× netlists + `op_loads`/`temp` factory knobs; `importmp` iv/dropout/trans derives + **GUARDRAIL-3** `check_zout_dc_consistency` (compares magnitudes) + **GUARDRAIL-2** cPSRR-sign test |
| `59b3912` | ①b orchestration | `run_pmu_coverage_sweep` (load×temp; AC/noise per load, dc/tran/2× once at OP) → one `<name>_sweep.npz` w/ `meta_iload_<o>`/`meta_temp`. `run_pmu_corner` **byte-untouched** (only `step_run` gained optional `groups=`) |
| `dd76c3c` | ②a anti-footgun | **deleted the flat-DC footgun** (`export_single_port_refs`); `fit_model` emit/build_pwl gracefully skip missing DC (no fabrication, no KeyError); `.va` `// COVERAGE=<tier> OP=@ VALID_LOAD` banner |
| `c2d3e48` | ②b modeling depth | real per-load iload (meta); **bias I-V large-signal core** (reuse `fit_isrc` → `idc55/didt/gdd`-signed/`vknee` → `_current_block_largesignal`); **ln(iload) scheduling** in `emit_pmu_va` (corner-exact); **additive `slew_en`** (GUARDRAIL-1: `I=V/R_a + slew_en·(pwl−OP_tangent)`, 0 value+slope at OP) |
| `14b6b09` | ③ GUI | coverage controls (tier/temps/slew_en/lin_gate box + v_out `iload sweep`/`trans` cols + wired i_out `iv_sweep`), overlay-preserving round-trip (no-coverage stays byte-clean); + 2 UI fixes (Scan reads main-window netlist path; Tab-0 QScrollArea vertical scroll). **Visually confirmed via offscreen screenshots.** |

All four guardrails landed: additive slew_en (1), cPSRR sign (2), Zout(0)↔DC consistency (3), 2× lin-gate point (4).

## 2. Local validation done THIS session (Spectre 18.1 + ngspice — NOT yet captured as repo tests)

Spectre is local: `cadence/spectre_run.py` (`SPECTRE_HOME=/home/yusheng/Program/eda/cadence/SPECTRE181`,
`sr.run(scs_text, tag)` → `spectre -64 -format psfascii`). Validation run (a tiny behavioral PMU TB →
the REAL offline netlister → real spectre):

- **dc/tran/iv coverage netlists CONVERGE in real spectre.** dropout: Vout = 0.8−20·Iload exact;
  I-V: 1µA at 0.8V on an 800k sink; transient PWL step runs.
- **PSF axis names CONFIRMED** (these were flagged "box-pending"): the dc-sweep axis is **`'dc'`**, the
  transient axis is **`'time'`** — exactly what `importmp._sweep_axis`/`_time_axis` try FIRST, so the
  fallbacks were never needed. `importmp._derive('dropout'/'iv'/'trans')` returns correct
  `[Iload,Vout]`/`[Vsweep,I]`/`[t,V]` on real spectre PSF.
- **`_covtemp options temp=55` accepted by spectre** (no fatal) — `netlist_augment.COVTEMP_NAME` validated.
- **GUARDRAIL-3 on real spectre data**: `dVout/dIload = −20.000 Ω` (= the DUT Rout) ✓.
- **GUARDRAIL-1 in ngspice** (capless `v2_capless` model, `~/.local/bin/ngspice`): at the OP load the
  AC small-signal Zout is **identical** for `slew_en=0` and `slew_en=1` (`23.2301 Ω`, 0% mismatch);
  `slew_en=0` Rout = 23.230 = the fit `R_a` exactly; `slew_en=1` adds the real dropout (Vout collapses
  to −0.215 V by 6 mA on the capless rail). Additive correction has 0 value AND 0 slope at OP, confirmed.

## 3. Test state
`python3 -m pytest harness cadence -q` → **267 passed** (3 benign scipy AAA warnings).
`QT_QPA_PLATFORM=offscreen python3 gui/ldo_modeler.py --selftest --require-qt` → **GUI selftest PASS**.

## 4. NEXT (next build session — small, lock-it-in)
1. **Fold the local-spectre findings into the code** (remove the now-resolved "box-pending" hedges):
   `importmp._sweep_axis`/`_time_axis` comments note "confirmed on local Spectre 18.1: dc axis `'dc'`,
   tran axis `'time'`"; `netlist_augment.COVTEMP_NAME` notes "spectre accepts `options temp=`".
2. **Add spectre-gated + ngspice-gated regression tests** (skip cleanly when the engine is absent, like
   the existing `spectre_cli` fixtures): (a) coverage dc/tran/iv → real spectre → `importmp` derives +
   guardrail-3 on a tiny behavioral PMU TB; (b) the additive-`slew_en` ngspice check (OP `Zout(slew0)==
   Zout(slew1)`, dropout at high load) so a future emit change can't silently break guardrail-1.
3. (Optional) a `HANDOFF_MODELING_COVERAGE.md` "BUILT" banner pointing here.

## 5. Genuinely box-only (red zone) remaining
A REAL Donau+ALPS sweep of the actual wur 2v+3i silicon DUT (vs my behavioral TB), the ALPS engine
(vs local spectre), and the generated `.va` compiling+running in the Cadence flow. Plus `bash apply`
to deploy to the red zone. The methodology + netlist generation + PSF reading + derives + guardrails
are now simulator-validated locally.

Related: `HANDOFF_MODELING_COVERAGE.md` (the plan/spec), memory `[[next-coverage-modeling-build]]`,
`[[ldo-unified-source-reuse]]`, `[[ngspice-built-from-source]]`, `[[redzone-install-prefix]]`.
