# HANDOFF — Manifest editing UX + schema clarity (next conversation)

Date: 2026-06-17. Branch `main`. HEAD at handoff: `69066c3`.
This session's actual deliverable was the **deploy chain** (skillbridge + one-command `apply`
+ CRLF / self-overwrite fixes); see "Deploy state" below. The NEXT conversation tackles the
6 manifest-UX items the user raised. **Do not start building these without the user's go**
(working mode: plan in normal session, build in a fresh ultracode conversation).

---

## Deploy state carried in from this session (context, mostly DONE)

Commits this session: `c9da094` skillbridge==1.8.0 into offline venv · `0306884` `deploy/apply`
one-command updater · `dc01333` `.gitattributes` LF for `deploy/apply` (Windows CRLF'd it →
`set -euo pipefail\r` "invalid option name") · `69066c3` install launchers atomically (temp+`mv`)
to stop the running script self-overwrite (`line N: syntax error near '('` AFTER work succeeded).

- **Confirmed on the box:** `bash apply` of the full bundle installed `skillbridge 1.8.0` and the
  GUI selftest PASSED. skillbridge works.
- **Going-forward update command (single, for code OR deps): `bash apply`** at the install root
  `/data/RFIC3/Hi1108V100_Pilot_C1Xplus/w84368867/workarea/LDO_modeling`. Auto-detects bundle
  `mode` (incremental→update.sh, full→bootstrap.sh). `bash update` still works (incremental only).
- **ONE pending action** (last repackage to flush the deploy fixes): yellow zone
  `git pull` → `del deploy\apply` → `git checkout -- deploy\apply` (re-materialize LF) →
  `.\deploy\package.ps1` (full) → red zone `sed -i 's/\r$//' apply && bash apply`. After this the
  deployed launchers are clean LF + atomically installed; no more CRLF/syntax-error tails ever.

---

## The 6 manifest-UX items (next task) — grounded in the current code

Ground truth read this session:
- Manifest schema is built in `cadence/insitu/build_manifest.py` (docstring lines 32–45; DUT block
  lines 224–240). Required fields (raise if missing, line 187): **`tb_lib`, `tb_cell`, `dut_lib`,
  `dut_cell`**. Everything else is optional/auto-defaulted.
- Current in-GUI editor: `gui/ldo_modeler.py` `_ManifestEditorDialog` (line 592) — a **raw JSON
  `QTextEdit`** with Validate/Save/Save As, opened by `_open_manifest_editor` (1390), reached from
  `b_edit` (1044). Validate runs `insitu.manifest._fill_defaults` + `validate` + `summary`.
  Template `_MANIFEST_TEMPLATE`; help `_MANIFEST_ROLE_HELP`. Selftest `_selftest_manifest_editor`
  (2403) — keep it green through any redesign.

### 1. Manifest must be FORM fill-in (not a text/document editor)
User wants fill-in-the-blanks fields, not editing raw JSON. A new page/dialog is fine if one
screen is too small.
→ Build a structured form over the schema: grouped sections (DUT block · Testbench block ·
Supplies(N) · v_out rails · i_out sinks(table: pin / compliance vdc / iv_sweep) · temps/tnom).
Keep JSON as the on-disk format; form ↔ JSON round-trip; reuse `manifest.validate` for the live
check. Likely a new dialog/tab paralleling the pin-form already in Tab 0. The raw-JSON editor can
stay as an "advanced/raw" fallback (see #3).

### 2. PDK model path defaults
Default the model file by engine: **alps → `$MODEL_ROOT/alps/toplevel.scs`**, **spectre →
`$MODEL_ROOT/spectre/toplevel.scs`**. Wire as the default value of the model-dir/model-file field
(Mode B / engine panel), overridable. (Check where the model dir is consumed: `alps_cli.build_sim_cmd`
`model_dir`/`_engine_model_tree`, and the Tab-0 engine fields `xb_*`/`x_backend`.)

### 3. Find/replace (Ctrl+F) even in the kept text-marker manifest editor
Add a find + find/replace bar (QShortcut Ctrl+F / Ctrl+H) over the `QTextEdit` in
`_ManifestEditorDialog`. Cheap, independent of #1.

### 4. `cell` vs `tb_cell` — they ARE different objects (clarify in UI)
- `dut.cell` (gui `dut_cell`) = the **DUT** cell — the actual LDO/PMU being modeled.
- `dut.tb_cell` (gui `tb_cell`) = the **testbench** cell — the schematic that instantiates the DUT
  plus sources/probes; this is what ADE actually simulates.
→ Fix: label them "DUT cell" vs "Testbench cell", with tooltips; never show bare "cell".

### 5. `tb_lib` vs `lib` — separate by design, often the same in practice
- `dut.lib` (gui `dut_lib`) = library holding the **DUT** cell.
- `dut.tb_lib` (gui `tb_lib`) = library holding the **testbench** cell.
They're kept distinct so the TB can live in a different library than the DUT; in many flows they're
the same library. → Fix: label "DUT library" vs "Testbench library"; consider defaulting `tb_lib`
to `dut_lib` with an "override" affordance so the user only fills it when they differ.

### 6. `extract_cell` and other fields the user doesn't recognize — mostly OPTIONAL
- `dut.extract_cell` (gui `extract_cell`) = the TB **variant used for the extraction sweep**;
  auto-defaults to `f"{tb_cell}_extract"` (build_manifest.py:232). **Optional** — user need not fill.
- Also optional/auto: `tb_view`/`extract_view`, `tb_inst`(dut_inst), `name`, `ade_src_test`,
  `analysis`, plus the per-pin overrides `iload`/`vdc`/`iv_sweep`/`biases`/`temps`/`tnom_c`.
→ Fix: split the form into **Required** (tb_lib, tb_cell, dut_lib, dut_cell, supply, v_out/i_out,
  ground) vs **Optional/Advanced** (collapsed by default). Hide/auto-derive `extract_cell` unless
  the user opens Advanced. This directly answers "是不是必须的" — for most fields, no.

---

## Cross-cutting design notes for next session
- Single source of truth stays the JSON manifest + `insitu.manifest.validate`; the form is a
  typed front-end, not a second schema. Round-trip must be lossless (preserve unknown keys).
- Respect the npz firewall / thin-shell rule: the form writes the same manifest dict
  `build_manifest`/`manifest.validate` already consume — wire, don't reimplement the contract.
- Keep `--selftest` green (extend `_selftest_manifest_editor` for the new form).
