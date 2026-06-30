# Next batch — GUI UX (sub-tabs + per-group menu) + grouped-PSF bug + import-finished-sim

> **SCOPE FOR THIS ULTRACODE SESSION: build ALL of #2–#5 in one go.** #1 (reverse-engineer the
> grouped PSF) is DONE — see the SPEC NAILED block below. This doc is the single source of truth;
> the Tasks list was session-scoped and cleared, so RE-CREATE tasks from the five items here.
>
> **Build order: #2 → #3 → #4 → #5.** #2+#3 first (fix the import bug + recover the stuck 192MB
> run without re-simulating; strongly linked). Then the GUI UX: #4 (sub-tabs) before #5 (the
> per-group menu lives on the Run sub-tab #4 creates).
>
> **Per item:** implement → validate locally (keep the `groups=0` binpsf regression green + add the
> new synthetic `groups=1` fixture; GUI `--selftest` green for #4/#5) → then ONE batched
> commit/push at the end (the box pulls via git). The real-file binpsf check is the user re-running
> `probe_binpsf2.py` on the box and diffing against its (freq,out) oracle.
>
> _Captured 2026-06-22 (planning); spec for #1/#2 nailed 2026-06-23 via committed read-only probes._

## Priority order (most urgent first)

### 1+2. BUG — grouped PSF (`PSF groups=1`) crashes import  *(blocker; cost a finished run)*
A real Donau+ALPS sweep **completed** and wrote PSF to disk, but **import** crashed:

```
NotImplementedError: .../tt_25c/psf/g_n_pll/nz.noise: grouped PSF (PSF groups=1) not supported
  run_cluster_sweep -> PC.step_import (pmu_corner.py:457)
  -> importmp.from_psf_multiport (importmp.py:215) -> psf.read_psf -> binpsf.read_binpsf (binpsf.py:255)
```

- **Root cause:** `cadence/binpsf.py:254-256` hard-raises when the PSF header `PSF groups != 0`.
  The real ALPS/Spectre **noise** output (`nz.noise`) is saved as a *grouped* PSF (groups=1), a
  trace-section layout our reader never handled (our own saves were groups=0).
- **Key fact:** the expensive cluster work is DONE; the PSF is on disk. Only the local read fails.
  So this is recoverable by re-import once binpsf reads grouped PSF (see item 3).
- **Task #1 (DONE 2026-06-23):** the 192MB file can't leave the box, so we reverse-engineered it
  *in place* with two committed READ-ONLY probes (`cadence/probe_binpsf.py` fa06b3a,
  `cadence/probe_binpsf2.py` 42304ed). The user ran them on the real `g_n_pll/nz.noise`. Full spec
  + a 141-point validation oracle obtained; the file itself is NOT needed in-repo.

**SPEC NAILED (2026-06-23) — the guard was a false wall, the value layout is unchanged:**
- `PSF groups=1` here just means ALPS saved **every device's** noise contribution
  (`PSF traces=19988`). The VALUE section is the SAME flat per-point layout as groups=0: each
  point = `0x10 sweepId <1 double freq>` then per trace `0x10 traceId <width doubles>` in
  trace-decl order. **Constant stride** across points — validated: `off_acc==stride=1363384 B`;
  next-point sweep marker `0x10 id=21` at the predicted offset; `npoints×stride == VALUE_len−4`
  (trailer); entries/pt `2+6997+75+3+12912 = 19989 = sweep + 19988`.
- **`out` = last decl (id=20009), type=2 (`V/√Hz`), width=1 double = real SCALAR.** importmp needs
  only `d["out"]`+`d["freq"]` (`importmp.py:176`). The ~20k device structs (widths 3/4/7/10) drop.
- **Oracle:** probe2 printed the exact (freq,out) series — `out[0]=8.638555e-05 @10Hz` →
  `out[140]=4.581145e-09 @100MHz` (smooth rolloff, sane). The fix must reproduce it.

- **Task #2 (UNBLOCKED, fully specced):** (1) relax the `binpsf.py:254` guard — the flat layout is
  readable with groups≥1; (2) make `_read_values` FAST for big grouped files — measure each entry's
  (id→width, offset-within-point) from POINT 1 (constant across points), then read each WANTED
  column directly at `v0 + i*stride + off + 8` for `i in 0..npoints-1` (no full-file scan; the
  current `_scan_to_marker` would crawl all 192MB); structs without a column are skipped by their
  measured width; (3) `out` stays a scalar real read. **Test:** keep the groups=0 fixture green +
  add a small SYNTHETIC groups=1 fixture (hand-built to this byte layout — the 192MB file can't be
  a repo fixture) exercising struct-drop + scalar-out; confirm on the real file via probe2/oracle.

### 3. FEATURE — import an already-finished simulation (skip the sweep)  *(also the bug's recovery)*
User: *"if the user already has a finished simulation, read it directly, don't re-run."* The seam
exists: `importmp.from_psf_multiport(psf_map={tag: file|dir})` (importmp.py:196) and `step_import`
already takes a `psf_map` (pmu_corner.py:432). Build a GUI entry (a 3rd Mode, or on the new Run
sub-tab) that takes an existing **PSF root** (the per-corner `psf/` dir with per-group subdirs
`.../<corner>/psf/<group.tag>/`) + the manifest → assemble `psf_map` by tag (same BY-TAG mapping
`step_run` produces) → `step_import` → fit → Create model cell. No netlist/dsub/ALPS. Recovers the
current stuck run AND serves users who simulated outside the tool.

### 4. GUI UX — split Tab 0 "Extract" into sub-tabs (the user's main ask)
`_tab_extract` (`gui/ldo_modeler.py:3001-3369`) crams the whole 3-stage pipeline into one scroll,
squeezing the Build&Run status table + log. Make the body a nested `QTabWidget` along the existing
`1·/1b·/2·/3·` numbering:
- **Setup** = mode selector + pin form (`x_grp_pinform`) + manifest row + Engine/Run-on/Session +
  Mode-B import (`x_grp_modeb`) + cluster settings (`x_grp_donau`)
- **Run** = Build&Run + dry-run + gate + progress + per-group status table (`x_status`) + report
  log (`x_report`) — finally full height
- **Model cell** = model-cell group + send-to-Fit
Plus a thin **persistent status strip** above the sub-tabs (skillbridge indicator + current
manifest + gate + progress bar); **auto-switch to Run** on `_x_run`. **LOW RISK** — pure
reparenting; widgets keep identity so worker wiring (`:4745`) + live-button logic (`:5398`) stay
intact. Pattern already in file: `self.subtabs` Form/Raw-JSON at `:788`.

### 5. GUI UX — per-group right-click context menu on the run-status table
Right-click a row in `x_status` (`:3315`, one row = one group/tag) → that group's artifacts/actions.
- **Plumbing:** table is `NoSelection` + holds only strings today. Enable `Qt.CustomContextMenu`;
  stash per-row data (netlist dir, psf dir, log path, `job_id`, `dsub_cmd`) as UserRole data when
  rows are built (`_x_status_init` `:3678`). Cluster layer already returns `job_id`+`dsub_cmd` per
  group (`run_corner.py:140`); Donau verbs exist — `djob` (status), `dpeek`/`peek_tail` (tail),
  `dkill` (kill).
- **Menu (final, ordered):** Open netlist (input.scs) · Open/tail output.log (live follow) · Jump
  to first error · — · Open group folder · Open PSF dir · Open terminal here · Copy folder path ·
  — · Copy JOBID · Copy dsub command · Check job status (`djob`) · Cancel job (`dkill`) · Re-run
  this group.
- **Impl notes:** (a) files/folders via `xdg-open`; **"Open terminal" must detect
  gnome-terminal/konsole/xterm and FALL BACK to copy `cd <dir>`** (remote/X-forward box may have no
  GUI terminal). (b) **State-aware enabling** by the row's State (col 3): log/tail only after
  started, PSF dir/npz only when done, Cancel only while running, Re-run only when failed/done.
  "Re-run this group" closes the resilient-sweep loop (`0d3b9db` isolates failures; this retries
  one group without re-running all).

## Build order suggestion
#1 (get sample) → #2 (binpsf fix) and #3 (import-finished) together recover the stuck run; then the
UX #4 (sub-tabs) then #5 (context menu, lives on the new Run sub-tab). Validate offline + GUI
`--selftest`, then `bash apply` to the red zone.
