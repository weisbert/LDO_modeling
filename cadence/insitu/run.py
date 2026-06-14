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


def groups(m):
    """Group the measurement matrix into the minimal set of runs. AC measurements merge by
    (analysis, one-hot stimulus) -- AC superposition lets one run feed every saved port. A
    spectre NOISE analysis measures ONE oprobe, so noise NEVER merges across outputs: each
    v_out is its own group (key includes the output net), carrying that output's oprobe."""
    out = {}
    for pt in _manifest.measurements(m):
        if pt["analysis"] == "noise":
            onet = pt["reads"][0][1]
            key, oprobe = ("noise", ("oprobe", onet)), onet
        else:
            key, oprobe = (pt["analysis"], tuple(sorted(tuple(h) for h in pt["hot"]))), None
        g = out.setdefault(key, dict(analysis=pt["analysis"], hot=pt["hot"], oprobe=oprobe,
                                     tag=_group_tag(pt), members=[]))
        g["members"].append(pt)
    return list(out.values())


def _group_tag(pt):
    if pt["analysis"] == "noise":
        return "g_" + pt["tag"]                 # one group per noise output (n_pll -> g_n_pll)
    if not pt["hot"]:
        return f"g_{pt['analysis']}"
    return "g_" + "_".join(f"{k}_{v}" for k, v in pt["hot"])


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
    if build_first:
        from . import augment
        augment.build(m, ws=ws, verbose=False)
    ws["load"](str(SKILL_DIR / "insitu_run.il"))
    ws["insituEnsureTest"](session, test, d["tb_lib"], d["extract_cell"], "spectre")
    # backfill the designer's ADE state on the bare test: inherit OP design vars (fixes
    # ASSEMBLER-1610) + configure/enable the ac & noise analyses (fixes ASSEMBLER-1707).
    # Both analyses are configured here; the loop enables only the one each group needs.
    _adestate.inherit_state(ws, m, session, test, verbose=False)
    _, noise_fields = _adestate.parse_analysis(m["analysis"].get("noise", "noise"))
    # snapshot the designer's test-enable state and RESTORE it afterwards -- enabling only
    # our extraction test must not silently leave their ADE reconfigured.
    snap = ws["insituSnapshotEnabled"](session)
    psf_map, histories = {}, {}
    try:
        if cancel():
            raise CancelledError("cancelled before run")
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
                _adestate.set_noise_oprobe(ws, session, test, g["oprobe"], noise_fields)
            hot = {_manifest.acm_var(k, v): "1" for k, v in g["hot"]}
            for var in allvars:                               # one-hot: this group's vars=1
                ws["insituPutVar"](session, var, hot.get(var, "0"))
            ws["insituSubmit"](session)
            deadline, started = timeout, False
            while deadline > 0:                               # poll: confirm start, then idle
                if cancel():                                  # Cancel honoured between ticks
                    raise CancelledError(f"cancelled during {g['tag']}")
                st = ws["insituStatus"](session)
                if st and list(st) != [0, 0]:
                    started = True
                if st and list(st) == [0, 0] and started:
                    break
                el = timeout - deadline
                progress(base, f"group {i+1}/{n}: {g['tag']} — "
                         f"{'running' if started else 'starting'}… {el}s")
                time.sleep(2); deadline -= 2
            if deadline <= 0:                                 # surfaced, not silently skipped
                progress(base, f"group {i+1}/{n}: {g['tag']} — TIMEOUT after {timeout}s")
            hname = ws["insituRename"](session, g["tag"])
            histories[g["tag"]] = hname
            pdir = _resolve_psf_dir(ws, session, d, hname, g["tag"])
            for pt in g["members"]:
                if pdir is not None:
                    psf_map[pt["tag"]] = pdir
            progress((i + 1) / n, f"group {i+1}/{n}: {g['tag']} — "
                     f"{'collected' if pdir is not None else 'NO PSF'}")
    finally:
        ws["insituRestoreEnabled"](session, snap)             # leave the designer's ADE as found
    return dict(psf_map=psf_map, backend="ade", histories=histories,
                probe_aliases=None)


def augment_design_vars(m):
    from . import augment
    return augment.design_vars(m)


def _has_psf(d):
    """True iff `d` (or a child) actually holds an .ac/.noise PSF -- guards against a
    resolver returning a stale or empty run dir."""
    p = pathlib.Path(d)
    if not p.exists():
        return False
    for ext in (".ac", ".noise"):
        if any(p.rglob(f"*{ext}")):
            return True
    return False


def _resolve_psf_dir(ws, session, d, hname, tag):
    """Best-effort: locate the PSF tree for a finished run. Tries the ADE analog run dir
    (asiGetAnalogRunDir) and the session's results area, accepting either psf/ (ALPS) or
    netlist/ (Spectre). Only returns a dir that ACTUALLY contains PSF (non-empty), so a
    stale/previous run dir is not silently accepted. Returns a pathlib.Path or None."""
    cands = []
    try:
        rd = ws["asiGetAnalogRunDir"]()
        if rd:
            cands.append(pathlib.Path(str(rd)))
    except Exception:                                          # noqa: BLE001
        pass
    # the renamed history is the most specific anchor -> prefer it over the bare tag
    for base in cands + [CADENCE]:
        for sub in (f"**/{hname}/**/psf", f"**/{hname}/**/netlist", f"**/{tag}/**/psf"):
            for hit in (base.glob(sub) if base.exists() else []):
                if _has_psf(hit.parent):
                    return hit.parent
    for c in cands:
        if _has_psf(c):
            return c
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
