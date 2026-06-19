# HANDOFF — Coverage-Driven Modeling: BUILT + locally validated + LOCKED IN

**Status (2026-06-19):** the full coverage-driven modeling feature from `HANDOFF_MODELING_COVERAGE.md`
(the locked plan, commit `75e2188`) is **BUILT, committed, and pushed to `main`**, **validated on the
LOCAL Spectre 18.1 + ngspice**, and now **LOCKED IN as regression tests** (`bd36b39`). `275` backend
tests + GUI selftest green. **The whole local-validatable scope is DONE — only red-zone box items
(§5) remain.**

> **LOCK-IT-IN DONE (`bd36b39`, main, pushed):** §4 below is complete. (1) Folded the local-spectre
> findings into code comments — the "box-pending" hedges in `importmp._sweep_axis`/`_time_axis` and
> `netlist_augment.COVTEMP_NAME` are gone (confirmed: Spectre dc axis `"dc"`, tran axis `"time"`,
> `options temp=` accepted). (2) Two engine-gated regression tests (skip cleanly when the engine is
> absent via an env-override guard; never silently pass):
> - `cadence/cluster/test_coverage_spectre.py` (5 tests) — inline behavioral PMU DUT (v_out = 0.8 V
>   behind Rout=20, i_out = 800k sink) → the REAL offline netlister (dropout/iv/transient + temp=55)
>   → REAL spectre. Asserts dc/tran/iv converge; `_sweep == "dc"`/`"time"`; importmp iv/dropout/trans
>   derives correct; GUARDRAIL-3 dropout slope = −20.000 (= DUT Rout) AND `check_zout_dc_consistency`
>   fires on a mismatched Zout; the `_covtemp options temp=55` line is accepted.
> - `harness/test_coverage_ngspice.py` (3 tests) — `fit_variant('v2_capless')` → emit additive
>   slew_en → REAL ngspice. GUARDRAIL-1: at OP the AC Zout is identical for slew_en in {0,1}
>   (23.2301 Ω, rel 4.3e-8); at 6 mA slew_en=1 shows a real 187 mV dropout while slew_en=0 stays on
>   the linear R_a extrapolation.
>
> Suite `267 → 275` (8 additive). Built + adversarially verified via workflow (both verdicts
> pass / no issues). Re-checked by hand: full suite 275 green; new files 8 green on real engines;
> spectre hidden → 5 skipped, ngspice hidden → 3 skipped.

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

## 4. Lock-it-in — DONE (`bd36b39`)
Both items below shipped this session (see the banner at the top for details):
1. ✅ **Folded the local-spectre findings into the code** — `importmp._sweep_axis`/`_time_axis` and
   `netlist_augment.COVTEMP_NAME` no longer say "box-pending"; they record the confirmed local-Spectre
   axis names (`"dc"`/`"time"`) and the accepted `options temp=`.
2. ✅ **Added spectre-gated + ngspice-gated regression tests** — `cadence/cluster/test_coverage_spectre.py`
   (5) and `harness/test_coverage_ngspice.py` (3), engine-gated with an env-overridable skip guard.
3. (Skipped — optional) a `HANDOFF_MODELING_COVERAGE.md` "BUILT" banner; not needed, this doc is the
   live status.

## 4b. VA-compiles-in-Cadence — also done locally (`cadence/test_va_compile_spectre.py`)
The emitted **Verilog-A deliverable** (`fit_model.emit_va`, the additive-slew_en `module ldo_model`)
now **compiles through the LOCAL Cadence Spectre's `ahdlcmi -64` and simulates** — this was wrongly
shelved as box-only before. A 3rd SPECTRE-gated test (skip-guarded, env-overridable) pins it:
- (1) `ahdlcmi -64` compiles the 26 KB `.va` and the AC op converges to a finite LF Zout;
- (2) GUARDRAIL-1 holds **in the Cadence engine** too — at the OP `|Zout|` is identical for
  `slew_en ∈ {0,1}` (rel `0.00e+00`);
- (3) **cross-engine**: Spectre-VA LF Zout `23.230100 Ω` == the fit's `R_a` (rel `2e-6`) ==
  the ngspice SPICE-subckt measurement `23.2301 Ω` (rel `9e-13`). Same physics, two engines.

The mechanism is exactly `cadence/isrc_spectre.py`'s (`ahdl_include` + ahdlcmi -64). A 4th case in
the same file pins **GUARDRAIL-1b in Spectre**: at 6 mA `slew_en=0` stays the linear R_a value
(`0.7648 V`) while `slew_en=1` collapses to the real capless-rail dropout (`-0.2153 V`) — matching
the ngspice `-0.215 V` cross-engine.

## 4c. Binary-PSF reader — also validated on a LOCAL producer (`cadence/test_binpsf_local_spectre.py`)
`cadence/binpsf.py` (the cluster/ALPS binary-PSF read path) was validated only against ONE box-captured
Maestro fixture (`cadence/test_binpsf.py`). Local Spectre 18.1 emits binary PSF too (`-format psfbin`),
so a 2nd SPECTRE-gated test now round-trips a **locally-produced** binary PSF against `-format psfascii`
of the same deck: AC sweep (78 pts, worst rel `6.3e-7`) and noise output PSD (worst rel `5.8e-17`).
A second independent producer proves the binary grammar is read, not memorized. (Per-contributor
struct-noise traces + ALPS quirks stay covered by the box fixture — complementary, not a replacement.)
Suite `278 → 281`.

## 5. Genuinely box-only (red zone) remaining
A REAL Donau+ALPS sweep of the actual wur 2v+3i silicon DUT (vs my behavioral TB), and the **ALPS
engine's OWN Verilog-A compiler** accepting the `.va` (Spectre's ahdlcmi has now accepted it locally
— §4b — but Empyrean ALPS has a separate VA compiler, still unverified). Plus `bash apply` to deploy
to the red zone. The methodology + netlist generation + PSF reading + derives + guardrails + the VA
compiling/simulating in Cadence Spectre are all simulator-validated locally.

Related: `HANDOFF_MODELING_COVERAGE.md` (the plan/spec), memory `[[next-coverage-modeling-build]]`,
`[[ldo-unified-source-reuse]]`, `[[ngspice-built-from-source]]`, `[[redzone-install-prefix]]`.
