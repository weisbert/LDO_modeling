"""P3 -- run-drive: trigger the in-situ extraction and return a {tag: PSF} map.

Two backends share the SAME measurement matrix and feed the SAME importmp firewall:

  spectre_cli  (default on the dev box; DETERMINISTIC, reproduces the trusted gold)
      Re-uses the validated cadence/extract_pmu.py runs (standalone `spectre -64`), whose
      per-measurement PSF lands in cadence/work_pmu/<tag>/raw. This is the dev/CI fixture
      for the METHOD -- it proves the manifest->importmp->fit path end to end without a
      live session.

  ade  (production; visible in Maestro, rides Job Setup -> cluster Monday)
      Builds/uses the augmented Test_PMU_extract test and drives the simkit-proven
      axlRunAllTests Submit->poll->Rename sequence via cadence/skill/insitu_run.il. Per
      measurement GROUP it sets exactly one acm_* design variable to 1 (axlPutVar), runs,
      and collects the PSF (trying psf/ [ALPS] and netlist/ [Spectre]).

Grouping: measurements that share one (analysis, one-hot) pattern are read from ONE run
(AC superposition -> every saved port is an identifiable transfer), so 14 measurements
need only ~7 runs.
"""
import pathlib

from . import CADENCE, SKILL_DIR, manifest as _manifest, adestate as _adestate

WORK_CLI = CADENCE / "work_pmu"          # extract_pmu.py's PSF lands here
# the CLI fixture (extract_pmu.py) names its sink probes Vb500/Vb1u; the manifest names
# them Vprobe_* -- map for the read side when consuming the CLI PSF.
_CLI_PROBE_ALIASES = {"i500n": "Vb500:p", "i1u": "Vb1u:p"}


# the extra per-point keys the coverage kinds carry (1b's netlister reads these off the group);
# copied verbatim from the single member onto its (always size-1) group dict when present.
_CARRY_KEYS = ("sweep", "step", "edge", "tstop", "tstep", "amp")


def groups(m):
    """Group the measurement matrix into the minimal set of runs. AC measurements merge by
    (analysis, one-hot stimulus) -- AC superposition lets one run feed every saved port. A
    spectre NOISE analysis measures ONE oprobe, so noise NEVER merges across outputs: each
    v_out is its own group (key includes the output net), carrying that output's oprobe.

    The coverage kinds each get their OWN group (no merge): the 2x lin-gate ac point keys on a
    distinct ('ac2', hot) so it never folds into the 1x Zout group; every dc (I-V / dropout) and
    tran (slew) point keys on its unique tag (a DC/transient sweep is one run, not superposable).
    The per-point sweep/step/edge/... are copied onto the group so 1b's netlister reads them off
    the group dict (every coverage group has exactly one member)."""
    out = {}
    for pt in _manifest.measurements(m):
        a = pt["analysis"]
        hot = tuple(sorted(tuple(h) for h in pt["hot"]))
        if a == "noise":
            onet = pt["reads"][0][1]
            key, oprobe = ("noise", ("oprobe", onet)), onet
        elif a == "ac" and pt.get("amp"):
            key, oprobe = ("ac2", hot), None       # 2x lin-gate -> own group (never merges 1x ac)
        elif a in ("dc", "tran"):
            key, oprobe = (a, hot, pt["tag"]), None  # one group per dc/tran point (per unique tag)
        else:
            key, oprobe = (a, hot), None             # 1x ac merges by (analysis, one-hot)
        g = out.setdefault(key, dict(analysis=a, hot=pt["hot"], oprobe=oprobe,
                                     tag=_group_tag(pt), members=[]))
        g["members"].append(pt)
        for k in _CARRY_KEYS:                         # surface the coverage params on the group
            if pt.get(k) is not None:
                g[k] = pt[k]
    return list(out.values())


def _group_tag(pt):
    a = pt["analysis"]
    if a == "noise":
        return "g_" + pt["tag"]                 # one group per noise output (n_pll -> g_n_pll)
    # the coverage kinds (2x lin-gate ac, dc I-V/dropout, tran slew) are 1:1 with their point
    if (a == "ac" and pt.get("amp")) or a in ("dc", "tran"):
        return "g_" + pt["tag"]
    if not pt["hot"]:
        return f"g_{pt['analysis']}"
    return "g_" + "_".join(f"{k}_{v}" for k, v in pt["hot"])


# --------------------------------------------------------------- coverage sweep axes
# The two pure helpers the STAGE-1b coverage SWEEP driver (pmu_corner.run_pmu_coverage_sweep)
# uses to split the load x temp grid. They live here -- beside groups() -- so the "which group
# repeats per load" rule sits next to the grouping it keys off, and so a stage-3 GUI can import
# them without pulling in the orchestrator.

def load_axis(m):
    """The load-sweep axis: a list of (label, op_loads) where op_loads={v_out_key: iload_at_k}.
    Index k -> each rail's load_points(m,o)[min(k, last)]; axis length = max rail load-point count.
    A rail that declares NO loads is simply absent from op_loads (it keeps its TB-default OP).
    Returns [(None, None)] when NO v_out declares any loads (=> a single OP, today's behavior)."""
    outs = list(m["v_out"])
    per = {o: _manifest.load_points(m, o) for o in outs}
    nmax = max((len(v) for v in per.values()), default=0)
    if nmax == 0:
        return [(None, None)]
    axis = []
    for k in range(nmax):
        # a rail shorter than the axis HOLDS at its last declared point (min(k, last)); a rail
        # with no loads at all is absent from op_loads -> it keeps the base TB OP unchanged.
        op = {o: per[o][min(k, len(per[o]) - 1)] for o in outs if per[o]}
        axis.append((f"L{k}", op))
    return axis


def is_load_swept_group(g):
    """True for a group that REPEATS across the load axis (small-signal at the OP: 1x AC + noise).
    The dc/tran/2x-lin-gate groups are corner-ONCE (they characterize the load / large-signal
    dimension internally) -> they run once at the TB OP, not per load point."""
    a = g["analysis"]
    return a == "noise" or (a == "ac" and not g.get("amp"))


# ---------------------------------------------------------------- spectre_cli backend
def run_spectre_cli(m, regenerate=False):
    """Return a {measurement_tag: PSF-dir} map over extract_pmu.py's CLI runs. With
    regenerate=True, (re)runs extract_pmu first (needs spectre + the VA fixtures). Returns
    dict(psf_map, probe_aliases, backend)."""
    if regenerate:
        _regenerate_cli_psf(m)
    psf_map = {}
    missing = []
    for pt in _manifest.measurements(m):
        d = WORK_CLI / pt["tag"]
        if (d / "raw").is_dir():
            psf_map[pt["tag"]] = d
        else:
            missing.append(pt["tag"])
    if missing:
        raise FileNotFoundError(
            f"spectre_cli: no CLI PSF for {missing} under {WORK_CLI}. Run with "
            f"regenerate=True (or `python cadence/extract_pmu.py`) to produce it.")
    return dict(psf_map=psf_map, probe_aliases=_CLI_PROBE_ALIASES, backend="spectre_cli")


def _regenerate_cli_psf(m):
    """(Re)produce the per-measurement PSF by invoking extract_pmu.py's proven runs. This
    is the dev fixture's 'run' step (standalone spectre); the ADE backend replaces it with
    a Maestro run."""
    import sys
    sys.path.insert(0, str(CADENCE))
    import extract_pmu as ep                                   # noqa: E402
    for o in m["v_out"]:
        ep.measure_z(o); ep.measure_noise(o)
        for o2 in m["v_out"]:
            if o2 != o:
                ep.measure_couple(o, o2)
        for s in m["supplies"]:
            ep.measure_p(o, s)
    for c in m["i_out"]:
        ep.measure_y(c)
        for s in m["current_psrr_supplies"]:
            ep.measure_pi(c, s)


# ----------------------------------------------------------------------- ade backend
def _ws():
    from skillbridge import Workspace
    return Workspace.open()


class CancelledError(RuntimeError):
    """Raised by run_ade when the caller's cancel() returns True mid-run (the GUI Cancel
    button). A clean, catchable signal -- not a crash -- so the worker can report 'cancelled'
    rather than a traceback, and the finally-block still restores the designer's ADE state."""


def _noop_progress(frac, msg):                                # default: no-op progress sink
    pass


def _cur_hist(ws, session):
    """The session's current-history NAME, or None. Defensive: on a fresh/reset session
    axlGetCurrentHistory yields handle 0 whose name lookup raises an errset-uncatchable
    'setup database entry for handle 0' -- swallow it (no history yet)."""
    try:
        return ws["insituCurHist"](session)
    except Exception:                                          # noqa: BLE001
        return None


# axlGetRunStatus returns (completed, total) points -- NOT an idle code. After submit it
# resets completed->0 within ~1-2s; _SETTLE bounds how long we wait for that reset before
# trusting a (N,N) reading as the NEW run's completion (guards the previous run's resting
# (N,N)). Observed reset latency ~1.5s; 5s is a safe margin.
_SETTLE = 5.0


def run_ade(m, session="fnxSession0", test="insitu_extract", ws=None,
            poll=None, timeout=600, build_first=True, progress=None, cancel=None):
    """Drive the augmented extraction through Maestro: per group set the acm one-hot,
    axlRunAllTests, poll to idle, rename, collect PSF. Returns dict(psf_map, backend,
    histories). Requires a live ADE-XL session whose extract test carries the ac+noise
    analyses + targeted saves (configured once / captured from the designer's state -- see
    augment); this driver automates the test wiring, the one-hot vars and the run.

    progress(frac, msg) -- optional callback for UI feedback (frac in [0,1]); the poll loop
    reports per-group start/elapsed/done instead of blocking silently up to `timeout`.
    cancel() -> bool -- optional; checked each poll tick. When it returns True the run aborts
    with CancelledError after the current SKILL call (the finally-block restores ADE state).

    NB: the run TRIGGER (axlRunAllTests) is the production-transferable piece -- it inherits
    the session's Job Setup (cluster+ALPS) Monday with no change."""
    import time
    ws = ws or _ws()
    progress = progress or _noop_progress
    cancel = cancel or (lambda: False)
    d = m["dut"]
    extract_view = d.get("extract_view", "schematic")
    if build_first:
        from . import augment
        augment.build(m, ws=ws, verbose=False)
    ws["load"](str(SKILL_DIR / "insitu_run.il"))
    if extract_view == "config":
        # config-view fidelity: mirror the designer's config (viewlist/stoplist/per-cell
        # view bindings -> preserves extracted/parasitic views on the REAL PMU) onto the
        # extract cell, with the top repointed to our augmented schematic. Built here (after
        # the schematic exists, so the config's top cellview resolves). Idempotent.
        ws["insituBuildConfig"](d["tb_lib"], d["tb_cell"], d["extract_cell"], "schematic")
    ws["insituEnsureTest"](session, test, d["tb_lib"], d["extract_cell"], "spectre", extract_view)
    # backfill the designer's ADE state on the bare test: inherit OP design vars (fixes
    # ASSEMBLER-1610) + configure/enable the ac & noise analyses (fixes ASSEMBLER-1707).
    # Both analyses are configured here; the loop enables only the one each group needs.
    _adestate.inherit_state(ws, m, session, test, verbose=False)
    _, noise_fields = _adestate.parse_analysis(m["analysis"].get("noise", "noise"))
    # snapshot the designer's test-enable state and RESTORE it afterwards -- enabling only
    # our extraction test must not silently leave their ADE reconfigured. snap is taken
    # inside the try (with a None guard in finally) so a failure mid-setup never restores
    # against an un-taken snapshot.
    snap = None
    psf_map, histories = {}, {}
    try:
        if cancel():
            raise CancelledError("cancelled before run")
        snap = ws["insituSnapshotEnabled"](session)
        ws["insituEnableOnly"](session, test)
        allvars = list(augment_design_vars(m))
        grps = groups(m)
        n = len(grps) or 1
        for i, g in enumerate(grps):
            base = i / n                                      # this group spans [base, (i+1)/n)
            progress(base, f"group {i+1}/{n}: {g['tag']} — submitting")
            # enable ONLY this group's analysis (ac groups must not drag noise, which needs
            # an oprobe); for a noise group, point the single oprobe at THIS output's net.
            _adestate.enable_only_analysis(ws, session, test, g["analysis"])
            if g["analysis"] == "noise" and g.get("oprobe"):
                _adestate.set_noise_output(ws, session, test, g["oprobe"],
                                           m.get("ground", "gnd!"), noise_fields)
            hot = {_manifest.acm_var(k, v): "1" for k, v in g["hot"]}
            for var in allvars:                               # one-hot: this group's vars=1
                ws["insituPutVar"](session, var, hot.get(var, "0"))
            # Poll the run's OWN history, not the session aggregate. axlGetRunStatus returns
            # (completed,total) points -- DONE when completed>=total (it RESTS at (N,N), never
            # (0,0); the original `== [0,0]` test waited forever). The aggregate form double-
            # counts across histories and, after many renames, errors with "setup database
            # entry for handle 0" -- so we detect the NEW history this submit creates (name
            # != the pre-submit current history) and query THAT history's status. _SETTLE
            # guards a half-initialised read for a sub-poll-interval run.
            h_prev = _cur_hist(ws, session)
            ws["insituSubmit"](session)
            t_submit = time.time()
            deadline, hcur, started = timeout, None, False
            while deadline > 0:                               # poll until completed==total
                if cancel():                                  # Cancel honoured between ticks
                    raise CancelledError(f"cancelled during {g['tag']}")
                name = _cur_hist(ws, session)
                if name and name != h_prev:
                    hcur = name                               # this submit's own history
                comp, total = 0, 0
                if hcur:
                    comp, total = (list(ws["insituHistStatus"](session, hcur) or []) + [0, 0])[:2]
                    if total > 0 and comp < total:
                        started = True                        # observed THIS run in progress
                settled = (time.time() - t_submit) >= _SETTLE
                if hcur and total > 0 and comp >= total and (started or settled):
                    break
                el = timeout - deadline
                progress(base, f"group {i+1}/{n}: {g['tag']} — "
                         f"{'running' if (hcur and started) else 'starting'} {comp}/{total}… {el}s")
                time.sleep(2); deadline -= 2
            if deadline <= 0:
                # a hung / over-long run never reached idle. Do NOT insituRename it
                # (rename is destructive on a non-idle run -> ASSEMBLER-2423) and do NOT
                # submit the next group over a still-running one -- abort cleanly. The
                # finally-block restores the designer's ADE; raise so the user investigates.
                progress(base, f"group {i+1}/{n}: {g['tag']} — TIMEOUT after {timeout}s")
                raise RuntimeError(
                    f"group {i+1}/{n} '{g['tag']}' did not finish within {timeout}s "
                    f"(still running or hung). Raise timeout= or check the ADE/cluster job; "
                    f"the designer's ADE state has been restored.")
            # Anchor on the run's OWN (auto) history name. We deliberately do NOT rename to
            # g[tag] here: on a re-run the tag collides with a prior history and the rename
            # pops an ASSEMBLER-2409 MODAL that wedges the skillbridge channel (and on a
            # degraded session even an errset-wrapped rename escalates to that modal). The PSF
            # resolver works off the real history name regardless; readable Maestro names can
            # be restored later via delete-then-rename on a clean session.
            hname = hcur
            histories[g["tag"]] = hname
            pdir = _resolve_psf_dir(ws, session, hname, g["tag"], g["analysis"])
            for pt in g["members"]:
                if pdir is not None:
                    psf_map[pt["tag"]] = pdir
            progress((i + 1) / n, f"group {i+1}/{n}: {g['tag']} — "
                     f"{'collected' if pdir is not None else 'NO PSF'}")
    finally:
        if snap is not None:
            ws["insituRestoreEnabled"](session, snap)         # leave the designer's ADE as found
    return dict(psf_map=psf_map, backend="ade", histories=histories,
                probe_aliases=None)


def augment_design_vars(m):
    from . import augment
    return augment.design_vars(m)


def _resolve_psf_dir(ws, session, hname, tag, analysis):
    """Locate the PSF dir for a finished ADE-XL run. Anchors on the session's results
    location (axlGetResultsLocation, adexl p.111) -- the documented absolute results root
    (.../maestro/results/maestro). The run lands at <root>/<hname>/<pt>/<test>/psf/<x>.<ext>;
    we rglob under the renamed history (then the raw tag as a fallback) for THIS group's
    analysis extension and return the dir that holds the PSF, or None.

    The old impl called asiGetAnalogRunDir() with no args (it needs one) -> always errored
    -> cands empty -> searched only the cadence/ source tree -> never found the PSF. NB:
    axlGetResultsLocation may return a RELATIVE path if adexl.results.saveDir is overridden
    (e.g. on the cluster); here it is absolute. Resolve relatives against cwd best-effort."""
    ext = ".noise" if analysis == "noise" else ".ac"
    try:
        loc = ws["axlGetResultsLocation"](ws["axlGetMainSetupDB"](session))
    except Exception:                                          # noqa: BLE001
        loc = None
    if not loc:
        return None
    root = pathlib.Path(str(loc))
    for anchor in (hname, tag):                                # renamed history first
        base = root / str(anchor)
        if not base.exists():
            continue
        hits = sorted(base.rglob(f"*{ext}"))
        if hits:
            return hits[0].parent                              # the psf/ dir holding the PSF
    return None


# ----------------------------------------------------------------------- dispatch
def run(m, backend="spectre_cli", **kw):
    progress = kw.get("progress")
    if backend == "spectre_cli":
        if progress:                                          # synchronous; a coarse tick
            progress(0.0, "spectre_cli: reading dev-fixture PSF…")
        r = run_spectre_cli(m, regenerate=kw.get("regenerate", False))
        if progress:
            progress(1.0, "spectre_cli: PSF located")
        return r
    if backend == "ade":
        return run_ade(m, session=kw.get("session", "fnxSession0"),
                       build_first=kw.get("build_first", True),
                       timeout=kw.get("timeout", 600),
                       progress=progress, cancel=kw.get("cancel"))
    raise ValueError(f"unknown backend {backend!r} (spectre_cli | ade)")


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description="Run-drive the in-situ extraction (P3)")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--backend", default="spectre_cli", choices=["spectre_cli", "ade"])
    ap.add_argument("--regenerate", action="store_true")
    ap.add_argument("--session", default="fnxSession0")
    a = ap.parse_args()
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from insitu import manifest as M
    m = M.load(a.manifest)
    r = run(m, backend=a.backend, regenerate=a.regenerate, session=a.session)
    print(f"backend={r['backend']}  {len(r['psf_map'])} PSF dirs")
    for tag, p in sorted(r["psf_map"].items()):
        print(f"  {tag:16s} {p}")
