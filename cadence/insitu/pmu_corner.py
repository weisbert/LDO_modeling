"""Top-level ORCHESTRATOR -- run ONE process corner of in-situ PMU LDO modeling
end-to-end via the pure-CLI ALPS path (Path-B-first).

This is the glue that wires together the already-built + unit-tested pieces (READ their
modules; this file only CALLS their pinned interfaces -- it reimplements NONE of them):

   step 1  resolve     B  resolve.resolve_nets   pins -> nets (skillbridge)  OR inject netmap=
   step 2  manifest    C  build_manifest.write_manifest  (gui, netmap) -> JSON in the workarea
   step 3  augment     -  augment.build          build <tb>_extract (skillbridge)  OR skip
   step 4  netlist     -  (HANDOFF) accept a provided netlistdir+ahdllibdir+pdk_model_dir
   step 5  run_corner  A  PER-GROUP SWEEP: run.groups(m) -> N (~7) cluster.run_corner jobs,
                          one per measurement GROUP (one acm_* one-hot + only that group's
                          analysis), each -> its own psf dir; psf_map keyed BY MEASUREMENT TAG
   step 6  import      -  importmp.from_psf_multiport  the BY-TAG psf_map -> npz (workarea)
   step 7  fit         -  fit_multiport.fit_multiport + report
   step 8  emit        D  emit_pmu_model.emit_pmu_va   the ONE combined .va
   step 9  cell build  D  ldo_cellview.il + pmu_top_symbol.il (skillbridge)  OR dry plan

WHY step 5 is a SWEEP, not one run: in-situ extraction identifies each transfer by AC
superposition -- one simulation PER measurement GROUP, each setting exactly ONE acm_* design
variable to 1 (all others 0) and enabling only that group's analysis. run.groups(m) collapses
the measurement points into the minimal run set (stand-in 14->8, real PMU 21->10; AC merges by
(analysis, one-hot stimulus); NOISE is per-output -- it measures one oprobe). So ONE corner = N
cluster jobs (one per analysis group), and the importer maps
EACH group's PSF dir to its member measurement tags (psf_map keyed by TAG, never by corner).

Each step is its OWN function (a clean seam) and is SKIPPABLE -- by a flag, by injecting its
output (netmap=, netlistdir=, npz_in=), or (for the three Virtuoso steps 1/3/9) by passing
session=None, which makes them degrade to a printed PLAN instead of touching Cadence. So the
whole orchestrator is testable with NO Virtuoso / cluster / dsub present.

STORAGE (SHARED CONTRACT, owned here):
    $WORK_ROOT/ldo_modeling/<Lib>__<Cell>/<corner>/{netlist,psf,npz,model}
  <Lib>/<Cell> identify the PMU TB (gui tb_lib/tb_cell); <corner> the process corner. We
  resolve $WORK_ROOT from env WORK_ROOT (fallback: a writable ~/ldo_workarea). We NEVER
  write into the designer's spine $WORK_ROOT/simulation/<Lib>/<Cell>/.

  from insitu.pmu_corner import run_pmu_corner
  res = run_pmu_corner(gui, netmap=..., netlistdir=..., ahdllibdir=..., pdk_model_dir=...,
                       model_lib="LDO_model_lab", model_cell="PMU_model",
                       on_status=print_status)        # real-box one-corner run
  res = run_pmu_corner(gui, dry_run=True)             # pure plan: manifest + dsub + SKILL plan
"""
import json
import os
import pathlib

from . import build_manifest as _bm
from . import augment as _augment
from . import importmp as _imp
from . import manifest as _manifest
from . import run as _run
from . import adestate as _adestate
from . import SKILL_DIR
from .resolve import ResolveUnavailable

# the cluster driver (Component A) + emit (Component D) + multi-port fit (P5). cadence/ and
# harness/ are on sys.path via insitu/__init__, so these resolve by bare name.
import emit_pmu_model as _emit          # harness/emit_pmu_model.py
import fit_multiport as _fit            # harness/fit_multiport.py
import cluster                          # cadence/cluster (package)
from cluster import run_corner as _runc
from cluster import donau as _donau
from cluster.donau import DonauCfg


# the 9 ordered step ids (a `steps=` allow-list selects a subset; default = all)
STEPS = ("resolve", "manifest", "augment", "netlist", "run", "import", "fit", "emit", "cell")


class PmuCornerError(RuntimeError):
    """An orchestration step could not proceed and could not degrade (e.g. step 4 has no
    netlist and no way to make one; a required injection is missing)."""


# --------------------------------------------------------------------------- workarea
def resolve_work_root(work_root=None):
    """Resolve the writable $WORK_ROOT. Priority: explicit arg > env WORK_ROOT > a sensible
    default (~/ldo_workarea). NEVER the designer's simulation spine."""
    wr = work_root or os.environ.get("WORK_ROOT") or str(pathlib.Path.home() / "ldo_workarea")
    return pathlib.Path(wr)


def corner_dir(work_root, gui, corner):
    """$WORK_ROOT/ldo_modeling/<tb_lib>__<tb_cell>/<corner> with {netlist,psf,npz,model}
    created. <Lib>/<Cell> identify the PMU TB; <corner> the process corner."""
    lib, cell = gui["tb_lib"], gui["tb_cell"]
    base = resolve_work_root(work_root) / "ldo_modeling" / f"{lib}__{cell}" / str(corner)
    dirs = {}
    for sub in ("netlist", "psf", "npz", "model"):
        d = base / sub
        d.mkdir(parents=True, exist_ok=True)
        dirs[sub] = d
    return base, dirs


# --------------------------------------------------------------------------- progress
def _progress(cb, step, msg):
    """A per-step progress line. cb(step, msg) if a callback is given, else print."""
    line = f"[pmu_corner] {step:8s} | {msg}"
    if cb:
        cb(step, msg)
    print(line)


# =========================================================================== the steps
def step_resolve(gui, *, netmap=None, session=None, progress=None):
    """1) symbol pins -> TB nets (B resolver, skillbridge) OR accept an injected netmap=.

    Returns {pin: net}. If netmap= is supplied it is used verbatim (offline path). Else we
    drive resolve.resolve_nets over the live session; ResolveUnavailable (no live Virtuoso)
    is caught and re-raised as an ACTIONABLE PmuCornerError (supply netmap= or run on box)."""
    if netmap is not None:
        _progress(progress, "resolve", f"using injected netmap ({len(netmap)} pins)")
        return dict(netmap)
    pins = [*_bm.supply_pins(gui), *gui.get("v_outs", []), *gui.get("i_outs", []),
            *(gui.get("biases") or {}).keys()]
    from .resolve import resolve_nets
    try:
        nm = resolve_nets(gui["tb_lib"], gui["tb_cell"], gui.get("tb_view", "schematic"),
                          gui["dut_inst"], pins, session=session)
    except ResolveUnavailable as e:
        raise PmuCornerError(
            "step 1 (resolve) needs a live Virtuoso/skillbridge session and none is "
            f"available ({e}). Either run on the company box with the skillbridge server up, "
            "or pass netmap={pin: net} to skip resolution (run the B resolver in the CIW "
            "first: insResolveNets(...)).") from e
    _progress(progress, "resolve", f"resolved {len(nm)} pins -> nets")
    return nm


def step_manifest(gui, netmap, *, out_path, progress=None):
    """2) build + WRITE the pin-role manifest JSON (C). Returns (manifest_dict, path).
    Surfaces m['_warnings'] (missing i_out compliance vdc) prominently to the caller."""
    m = _bm.build_manifest(gui, netmap)
    p = pathlib.Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(m, indent=2) + "\n")
    _progress(progress, "manifest",
              f"wrote {p.name}  ({len(m['v_out'])} v_out, {len(m['i_out'])} i_out, "
              f"{len(m['supplies'])} supply)")
    for w in m.get("_warnings", []):
        _progress(progress, "manifest", "WARNING: " + w)
    return m, p


def step_augment(m, *, session=None, dry_run=False, progress=None):
    """3) augment the extraction TB (augment.build over skillbridge) OR, with no live session
    / dry_run, emit the headless PLAN (augment.build_plan). Returns dict(built, plan)."""
    plan = _augment.build_plan(m)               # pure/headless preview, always available
    if session is None or dry_run:
        _progress(progress, "augment",
                  f"DRY -- {len(plan)} planned ops (no live session); skipping the live build")
        for act, det in plan:
            print(f"    {act:12s} {det}")
        return dict(built=False, plan=plan)
    out = _augment.build(m, ws=session)
    _progress(progress, "augment",
              f"built {out['extract_cell']} with {len(out['design_vars'])} acm vars")
    return dict(built=True, plan=plan, **out)


def step_netlist(dirs, *, netlistdir=None, ahdllibdir=None, pdk_model_dir=None, progress=None):
    """4) NETLIST the extract TB -> input.scs + a compiled -ahdllibdir. The HANDOFF point: on
    the company box this is done in ADE (it pre-compiles the VA). For the MVP we ACCEPT a
    provided (netlistdir, ahdllibdir, pdk_model_dir). We do NOT fake a netlister -- a missing
    handoff is a clear error with the documented hook for future ADE-triggered netlisting.

    Returns dict(netlistdir, ahdllibdir, pdk_model_dir). When netlistdir is provided we leave
    it as-is (the ADE work dir); the run writes its PSF to our workarea psf/ (sibling)."""
    if not netlistdir:
        raise PmuCornerError(
            "step 4 (netlist) MVP needs a pre-built netlist handoff: pass netlistdir= (the "
            "ADE work dir holding input.scs). On the company box, netlist the <tb>_extract "
            "cellview in ADE and hand us that path. [future hook: ADE-triggered netlisting "
            "from the augmented TB]")
    # ahdllibdir + pdk_model_dir are OPTIONAL: the netlist's own `ahdl_include`/`include` lines
    # let the simulator auto-compile the VA and resolve models. Provide ahdllibdir only to reuse
    # a pre-compiled cache; provide pdk_model_dir only if the netlist needs an -I model tree.
    extras = [x for x in (("ahdllibdir", ahdllibdir), ("pdk_model_dir", pdk_model_dir)) if x[1]]
    _progress(progress, "netlist",
              f"handoff netlist dir: {netlistdir}"
              + (f"  (+{', '.join(k for k, _ in extras)})" if extras else
                 "  (no ahdllibdir/pdk -- simulator resolves VA+models from the netlist)"))
    return dict(netlistdir=str(netlistdir),
                ahdllibdir=str(ahdllibdir) if ahdllibdir else None,
                pdk_model_dir=str(pdk_model_dir) if pdk_model_dir else None)


def ade_group_netlist(ws, session, test, m, group, outdir):
    """DEFAULT group_netlister: produce the per-GROUP netlist dir on the company box by
    reusing the PROVEN run.run_ade wiring -- but NETLIST instead of run. For this group:

      * enable ONLY this group's analysis (adestate.enable_only_analysis); for a noise group,
        point the single oprobe/output at this group's net (adestate.set_noise_output);
      * set the acm ONE-HOT: every augment.design_vars(m) var -> '0', this group's hot vars
        -> '1' (insituPutVar) -- AC superposition makes each saved port identifiable;
      * NETLIST the ADE test (no run) via the SKILL helper insituNetlistTest (a NEW box-
        research seam in cadence/skill/insitu_run.il), then copy the produced input.scs into
        `outdir` so the pure-CLI dsub+alps job for this group netlists exactly this one-hot.

    This is the BOX-ONLY default (skillbridge); it NEVER runs in the offline tests (those
    inject a fake group_netlister). The netlist-only trigger (insituNetlistTest) is a
    DOCUMENTED stub pending box validation -- see open_issues / the runbook §4."""
    import shutil
    ws["load"](str(SKILL_DIR / "insitu_run.il"))
    # enable only this group's analysis (an ac group must not drag noise; a noise group needs
    # its single oprobe pointed at THIS output's net)
    _adestate.enable_only_analysis(ws, session, test, group["analysis"])
    if group["analysis"] == "noise" and group.get("oprobe"):
        _, noise_fields = _adestate.parse_analysis(m["analysis"].get("noise", "noise"))
        _adestate.set_noise_output(ws, session, test, group["oprobe"],
                                   m.get("ground", "gnd!"), noise_fields)
    # the acm one-hot: every acm_* var to '0', this group's hot vars to '1'
    from . import manifest as _manifest
    hot = {_manifest.acm_var(k, v): "1" for k, v in group["hot"]}
    for var in _augment.design_vars(m):
        ws["insituPutVar"](session, var, hot.get(var, "0"))
    # NETLIST the test (no run); insituNetlistTest returns the dir holding input.scs
    netdir = ws["insituNetlistTest"](session, test)
    out = pathlib.Path(outdir)
    out.mkdir(parents=True, exist_ok=True)
    src = pathlib.Path(str(netdir)) / "input.scs"
    if src.is_file():
        shutil.copyfile(src, out / "input.scs")
    return str(out)


def _live_group_netlister(ws, session_name, test, m, netlist_base):
    """Bind ade_group_netlist to a live session. CRITICAL: it does run.run_ade's ONE-TIME
    test setup ONCE before the first group -- create the bare ADE-XL test (insituEnsureTest),
    backfill the designer OP design vars (inherit_state -> fixes ASSEMBLER-1610) and configure
    the ac+noise analysis objects, then enable-only our test (insituEnableOnly). Without this
    the per-group enable_only_analysis would act on a test with NO analysis objects and netlist
    with EMPTY vars. BOX-ONLY: ade_group_netlist reaches insituNetlistTest, a documented stub
    -- see PMU_CORNER_RUNBOOK §8. Returns callable(group)->netlistdir."""
    d = m["dut"]
    state = {"setup": False}

    def _nl(g):
        if not state["setup"]:
            ws["load"](str(SKILL_DIR / "insitu_run.il"))
            ws["insituEnsureTest"](session_name, test, d["tb_lib"], d["extract_cell"],
                                   "spectre", d.get("extract_view", "schematic"))
            _adestate.inherit_state(ws, m, session_name, test)
            ws["insituEnableOnly"](session_name, test)
            state["setup"] = True
        return ade_group_netlist(ws, session_name, test, m, g,
                                 pathlib.Path(netlist_base) / g["tag"])
    return _nl


def step_run(netinfo, psf_root, m, *, engine="alps", donau=None, runner=None,
             on_status=None, dry_run=False, progress=None, group_netlister=None,
             group_status=None, cancel=None, groups=None,
             poll_interval=5.0, max_parallel=1, poll_timeout=10800.0, sleep=None):
    """5) run ONE corner as a PER-GROUP SWEEP via the pure-CLI Donau+ALPS path (Component A).

    grps = run.groups(m). For each group g (i/N) we get the group's netlist dir from the
    INJECTABLE seam group_netlister(g) -> netlistdir (each holding ONE acm one-hot's
    input.scs), then cluster.run_corner(group_netlistdir, pdk, ahdl, out=<psf_root>/<g.tag>)
    -> the group's PSF dir; psf_map[pt.tag] = that PSF dir for EVERY member pt of g.

    Returns dict(psf_map (BY MEASUREMENT TAG), dsub_cmds (one per group), ran). dry_run
    assembles + returns ALL per-group dsub commands WITHOUT executing. on_status is wrapped
    to prefix 'group i/N {g.tag}'. poll_interval/poll_timeout/sleep are forwarded into
    run_corner so tests drive the status loop with zero real sleeping.

    max_parallel (default 1) caps how many GROUP jobs are in flight on Donau at once. With
    max_parallel==1 the behavior is identical to the old strict-serial loop (submit one, poll
    to done, submit the next). With >1 a single-threaded BOUNDED poll-scheduler launches up to
    the cap, then poll_once's each in-flight job per tick (Donau runs the jobs in parallel; we
    just stop blocking on one before submitting the next). NO Python threads -- step_run already
    runs on the GUI's QThread.

    group_netlister(group) -> netlistdir is the seam: the DEFAULT (ade_group_netlist, bound by
    run_pmu_corner with the live ws/session/test) reuses the run_ade wiring to netlist each
    one-hot on the box; tests inject a fake that writes a stub input.scs per group.

    group_status(i, n, group, state) is an optional STRUCTURED per-group status callback (the
    GUI's per-group table feeds off it): 'pending' -> the Donau states ('pending'/'running'/
    'done'/'failed') -> 'done' once the PSF lands, or 'preview' on a dry_run. cancel() -> bool
    stops launching NEW groups; already-in-flight jobs are drained, then run.CancelledError is
    raised so the GUI Cancel reports cleanly rather than wedging the window.

    groups (default None) is the ONE additive seam for the STAGE-1b coverage SWEEP: pass an
    explicit PRE-SPLIT list of groups to run ONLY that subset (one load x temp cell's groups);
    None reproduces today's behavior EXACTLY (the full run.groups(m) set). Nothing else changes."""
    import time
    cfg = donau or DonauCfg()
    # ADDITIVE seam (default None => identical to today): the coverage SWEEP driver passes a
    # PRE-SPLIT subset of groups (e.g. only the load-swept ac/noise groups for one load cell);
    # every existing caller passes nothing -> we compute the full group set exactly as before.
    grps = groups if groups is not None else _run.groups(m)
    n = len(grps) or 1
    psf_root = pathlib.Path(psf_root)
    psf_map, dsub_cmds = {}, []
    _sleep = sleep or time.sleep

    # ---- dry_run path: assemble each per-group dsub command, submit NOTHING (verbatim) -------
    if dry_run:
        for i, g in enumerate(grps):
            tag = g["tag"]
            if group_status:
                group_status(i, n, g, "pending")
            group_netdir = group_netlister(g)         # the seam -> this group's netlist dir
            dsub_cmd = _runc.run_corner(
                group_netdir, netinfo["pdk_model_dir"], netinfo["ahdllibdir"],
                str(psf_root / tag), engine=engine, donau=cfg, dry_run=True)
            dsub_cmds.append(dsub_cmd)
            _progress(progress, "run",
                      f"DRY -- group {i+1}/{n} {tag}: assembled dsub command, not executing")
            if group_status:
                group_status(i, n, g, "preview")
        return dict(psf_map=psf_map, dsub_cmds=dsub_cmds, ran=False)

    # ---- real path: a single-threaded BOUNDED poll-scheduler --------------------------------
    # default a REAL runner ONCE here: submit_corner self-defaults its own, but poll_once does
    # NOT -- so a None runner (the GUI/default) used to submit fine then crash at the first
    # djob poll ('NoneType' object is not callable). Both halves must share a real runner.
    runner = runner or _donau.SubprocessRunner()
    pending = list(enumerate(grps))                    # FIFO of (i, g)
    inflight = {}                                      # job_id -> ctx dict
    cancelling = False
    while pending or inflight:
        # (1) launch up to the cap (unless cancelling)
        while (not cancelling) and pending and len(inflight) < max(1, max_parallel):
            if cancel and cancel():
                cancelling = True
                break
            i, g = pending.pop(0)
            tag = g["tag"]
            if group_status:
                group_status(i, n, g, "pending")       # emit BEFORE netlist/submit
            netdir = group_netlister(g)                # the seam -> this group's netlist dir
            sub = _runc.submit_corner(
                netdir, netinfo["pdk_model_dir"], netinfo["ahdllibdir"],
                str(psf_root / tag), engine=engine, donau=cfg, runner=runner)
            dsub_cmds.append(sub["dsub_cmd"])          # one per launched group, in group order
            _progress(progress, "run", f"group {i+1}/{n} {tag}: submitting")
            inflight[sub["job_id"]] = dict(i=i, g=g, out_abs=sub["out_abs"],
                                           require_simdone=sub["require_simdone"],
                                           last=None, waited=0.0)
        if not inflight:
            break                                      # nothing in flight (cancel before any launch)
        # (2) poll each in-flight job ONCE; report transitions; collect terminals
        for job_id, ctx in list(inflight.items()):
            state, raw = _donau.poll_once(job_id, runner)
            if state is None:
                state = ctx["last"]
            if state is not None and state != ctx["last"]:
                ctx["last"] = state
                i, g, tag = ctx["i"], ctx["g"], ctx["g"]["tag"]
                if on_status:
                    on_status(state, raw)              # raw Donau stream: ALL transitions
                # the row goes 'pending'/'running' live; the TERMINAL 'done'/'failed' status
                # fires in the handlers below -- 'done' only AFTER finalize verifies the PSF, so
                # the table never shows a group green that actually failed PSF verification (and
                # never double-fires 'done').
                if group_status and state not in ("done", "failed"):
                    group_status(i, n, g, state)
                _progress(progress, "run",
                          f"group {i+1}/{n} {tag}: Donau job {state.upper()}")
            if state == "failed":
                if group_status:
                    group_status(ctx["i"], n, ctx["g"], "failed")
                tail = _donau.peek_tail(job_id, runner)
                raise _runc.RunCornerError(
                    f"group {ctx['g']['tag']} (job {job_id}) FAILED"
                    + (f"\n--- dpeek tail ---\n{tail}" if tail else ""))
            if state == "done":
                out = _runc.finalize_corner(ctx["out_abs"],
                                            require_simdone=ctx["require_simdone"],
                                            job_id=job_id, runner=runner)
                _progress(progress, "run",
                          f"group {ctx['i']+1}/{n} {ctx['g']['tag']}: PSF landed: {out}")
                for pt in ctx["g"]["members"]:         # map EVERY member tag at this group PSF
                    psf_map[pt["tag"]] = str(out)
                if group_status:
                    group_status(ctx["i"], n, ctx["g"], "done")
                del inflight[job_id]
                continue
            ctx["waited"] += poll_interval
            if ctx["waited"] >= poll_timeout:
                raise _runc.RunCornerError(
                    f"job {job_id} (group {ctx['g']['tag']}) did not finish within "
                    f"{poll_timeout:.0f}s. Check djob/dpeek; the cluster job may be hung.")
        # (3) a cancel stops launching NEW groups; the in-flight ones above already drained
        if cancel and cancel():
            cancelling = True
        # (4) pace the loop only while jobs are still in flight
        if inflight:
            _sleep(poll_interval)

    if cancelling and pending:
        raise _run.CancelledError(
            f"cluster sweep cancelled ({len(pending)} group(s) not started)")
    return dict(psf_map=psf_map, dsub_cmds=dsub_cmds, ran=True)


def step_import(m, psf_map, *, npz_dir, npz_in=None, load=None, probe_aliases=None,
                progress=None):
    """6) import PSF -> generalized multi-port npz (importmp -- the read/derive dual of the
    measurement matrix). OR, when npz_in= points at a ready npz (no real PSF available -- the
    offline path), copy it into the workarea and use it. Returns the npz path.

    The real path reads the per-group PSF via importmp.from_psf_multiport with the BY-TAG
    psf_map from step_run (psf_map={measurement_tag: that group's PSF dir} -- NOT keyed by
    corner: each measurement reads ITS group's run). We then np.savez the read arrays into
    the WORKAREA npz/ dir ourselves. We deliberately do NOT use importmp.assemble_multiport
    here: it writes to results/ref/, which would escape the workarea -- this step OWNS the
    workarea npz/ and reuses from_psf_multiport only for the READ. probe_aliases is passed
    when reading a spectre_cli PSF (foreign probe names, run._CLI_PROBE_ALIASES)."""
    import shutil
    import numpy as np
    load = load or [str(x) for x in m["corners"].get("fallback", ["nom"])][0]
    out = pathlib.Path(npz_dir) / f"{m['name']}_{load}.npz"
    if npz_in is not None:
        src = pathlib.Path(npz_in)
        shutil.copyfile(src, out)
        _progress(progress, "import",
                  f"using injected npz {src.name} -> {out.name} (no real PSF read)")
        return out
    # real PSF -> npz via the importmp firewall: the BY-TAG psf_map (each measurement -> its
    # group's PSF dir) is read by tag, then written into the workarea npz/ ourselves.
    reads = _imp.from_psf_multiport(manifest=m, psf_map={k: str(v) for k, v in psf_map.items()},
                                    load=load, probe_aliases=probe_aliases)
    ref = {"loads": np.array([load]), **reads}
    np.savez(out, **ref)
    _progress(progress, "import", f"PSF -> npz: {out.name} ({len(reads)} port arrays)")
    return out


def step_fit(npz_path, m, *, progress=None):
    """7) multi-port fit + report (P5). Returns (fit_result, report_text). Pure-Python; no
    simulator. The report is printed (voltage ports + a SEPARATE current-port table)."""
    res = _fit.fit_multiport(str(npz_path), m)
    txt = _fit.report(res)
    _progress(progress, "fit",
              f"fit {len(res['voltage'])} voltage + "
              f"{len({r['sink'] for r in res['current']})} current ports")
    print(txt)
    return res, txt


def step_emit(fit_result, *, cell_name, va_path, supply, ground, provenance=None,
              progress=None):
    """8) emit the ONE combined PMU Verilog-A module (Component D). Ports are the GUI symbol
    PIN names (propagated by fit_multiport as each port's 'pin'). Returns the .va path.

    `provenance` (optional) := {tier, op_iload, op_temp, valid_load} for the .va COVERAGE
    banner; when None, emit_pmu_va sources it from fit_result['meta'] (set by fit_multiport),
    so this step needs no new caller args. The existing signature stays kwarg-compatible."""
    p = _emit.emit_pmu_va(fit_result, cell_name, va_path, supply=supply, ground=ground,
                          provenance=provenance)
    _progress(progress, "emit", f"wrote {pathlib.Path(p).name} "
              f"(1 module, {len(fit_result['voltage'])} V-rails + "
              f"{len({r['sink'] for r in fit_result['current']})} I-biases)")
    return p


def step_cell(fit_result, va_path, *, model_lib, model_cell, model_path,
              supply, ground, session=None, dry_run=False, progress=None):
    """9) build the model cell in Cadence (Component D SKILL: ldoEnsureLib + ldoImportVA +
    ldoCompileVA + pmuBuildModelCell over skillbridge) at the user's target lib/cell/path.
    With no live session / dry_run, emit the .va (already done in step 8) + a printed SKILL
    CALL PLAN instead of touching Cadence. Returns dict(built, plan, pinspec)."""
    # the per-pin symbol spec pmuBuildModelCell consumes: inputs LEFT, outputs RIGHT, VSS BOTTOM
    v_pins = [fit_result["voltage"][rk].get("pin", rk) for rk in fit_result["voltage"]]
    seen, i_pins = set(), []
    for r in fit_result["current"]:
        pin = r.get("pin", r["sink"])
        if pin not in seen:
            i_pins.append(pin)
            seen.add(pin)
    pinspec = ([[supply, "input", "left"]]
               + [[p, "output", "right"] for p in v_pins + i_pins]
               + [[ground, "inputOutput", "bottom"]])

    plan = [
        ("ldoEnsureLib", f'("{model_lib}" "{model_path}")'),
        ("ldoImportVA", f'("{model_lib}" "{model_cell}" "{va_path}")'),
        ("ldoCompileVA", f'("{model_lib}" "{model_cell}" "veriloga")'),
        ("pmuBuildModelCell", f'("{model_lib}" "{model_cell}" '
                              + "(" + " ".join(f'("{n}" "{d}" "{s}")' for n, d, s in pinspec)
                              + "))"),
    ]
    if session is None or dry_run:
        _progress(progress, "cell",
                  f"DRY -- {model_lib}/{model_cell} NOT built (no live session); SKILL plan:")
        for fn, args in plan:
            print(f"    {fn} {args}")
        return dict(built=False, plan=plan, pinspec=pinspec)

    # live build over skillbridge: source the .il helpers, then drive them in order.
    from . import SKILL_DIR
    ws = session
    ws["load"](str(SKILL_DIR / "ldo_cellview.il"))
    ws["load"](str(SKILL_DIR / "pmu_top_symbol.il"))
    ws["ldoEnsureLib"](model_lib, str(model_path))
    ws["ldoImportVA"](model_lib, model_cell, str(va_path))
    ws["ldoCompileVA"](model_lib, model_cell, "veriloga")
    ws["pmuBuildModelCell"](model_lib, model_cell, pinspec)
    _progress(progress, "cell", f"built {model_lib}/{model_cell} (cellview + symbol)")
    return dict(built=True, plan=plan, pinspec=pinspec)


# =========================================================================== orchestrator
def _gui_from_manifest(m):
    """Synthesize the MINIMAL gui fields run_pmu_corner still needs from a loaded manifest
    (the manifest-driven offline-sweep path). corner_dir needs tb_lib/tb_cell; the supply/
    ground/model defaults are read off the manifest too. We do NOT reconstruct the full GUI
    schema -- only the keys the orchestrator reads when manifest= is given (resolve + manifest
    steps are skipped, so build_manifest's gui keys are never consumed)."""
    d = m["dut"]
    sups = m.get("supplies") or {}
    first_sup = next(iter(sups.values()), {}) if sups else {}
    return {
        "tb_lib": d["tb_lib"], "tb_cell": d["tb_cell"],
        "dut_lib": d.get("lib", ""), "dut_cell": d.get("cell", ""),
        "ground": m.get("ground"),
        # supply_pins() reads gui['supply']={pin,..}; carry the first supply's pin
        "supply": {"pin": first_sup.get("pin", ""), "dc": first_sup.get("dc")},
        "corner": m.get("corner") or (m.get("corners", {}).get("fallback") or [None])[0],
        "model_cell": (m.get("dut", {}).get("cell") or "model"),
    }


def run_pmu_corner(gui=None, work_root=None, corner=None, engine="alps",
                   session=None, netmap=None, netlistdir=None, ahdllibdir=None,
                   pdk_model_dir=None, model_lib=None, model_cell=None, model_path=None,
                   runner=None, on_status=None, dry_run=False, steps=None,
                   donau=None, npz_in=None, fit_manifest=None, supply_pin=None,
                   ground=None, progress=None, group_netlister=None, manifest=None,
                   extract_test="insitu_extract", session_name="fnxSession0",
                   poll_interval=5.0, max_parallel=1, poll_timeout=10800.0, sleep=None):
    """Run ONE process corner of in-situ PMU LDO modeling end-to-end (the 9-step flow).

    gui            the designer's GUI inputs (build_manifest schema: tb_lib/tb_cell/tb_view,
                   dut_inst/dut_lib/dut_cell, supply={pin,dc}, v_outs=[pin], i_outs=[pin],
                   ground, corner, optional biases/iload/vdc). OPTIONAL when manifest= is given.
    manifest       a loaded manifest dict to drive the run VERBATIM (the manifest-driven
                   offline-sweep path -- the run-sweep CLI). When given we use it as m, SKIP
                   the 'resolve' and 'manifest' steps (we already have m + resolved nets), and
                   synthesize the minimal gui fields the remaining steps need from it. The
                   gui-driven path (manifest=None) is 100% unchanged.
    work_root      writable $WORK_ROOT (else env WORK_ROOT, else ~/ldo_workarea).
    corner         process-corner label (else gui['corner'], else 'nom').
    engine         'alps' (default) | 'spectre'.
    session        a live skillbridge ws for steps 1/3/9, or None -> those steps degrade to a
                   PLAN (with netmap=/dry_run for the others).
    netmap         inject {pin: net} to SKIP step 1 (offline).
    netlistdir/ahdllibdir/pdk_model_dir   the step-4 HANDOFF (ADE-prepared netlist + compiled
                   VA DB + PDK root). Required to actually run (step 5); omit only for a pure
                   plan that stops before the run.
    model_lib/model_cell/model_path       the target model cell (step 9). Defaults derived.
    runner         injected cluster command executor (a fake in tests; real subprocess on box).
    on_status      callback(state, raw) for the Donau pending/running/done transitions.
    dry_run        no Cadence + no cluster execution: write the manifest + the augment/SKILL
                   plans, assemble the dsub command, and (if npz_in given) still fit+emit.
    steps          an allow-list subset of STEPS (default = all 9, gated by available inputs).
    npz_in         a ready npz to inject at step 6 (bypass the real PSF read -- the offline
                   path; the orchestrator copies it into the workarea npz/ dir).
    fit_manifest   the manifest that PAIRS with npz_in for steps 6/7/8 (its 'pin' fields drive
                   the .va ports). Defaults to the GUI-built manifest (the real-box path, where
                   the npz from the real run matches the real manifest). Inject a different one
                   only when the injected npz has a different port topology than the GUI (e.g.
                   the stand-in npz used for offline tests).
    supply_pin/ground   override the .va supply/ground port names (default: gui supply pin /
                   gui ground).
    group_netlister  callable(group)->netlistdir for the step-5 PER-GROUP SWEEP. Each group's
                   netlist dir holds an input.scs with exactly that group's acm one-hot + only
                   that group's analysis. DEFAULT (live session): ade_group_netlist bound to
                   ws/session/extract_test (box-only). OFFLINE default (no session): reuse the
                   handoff netlistdir for every group (so the dsub commands still assemble).
                   Tests inject a fake that writes a stub input.scs per group.
    extract_test   the ADE-XL extract test name the default group_netlister netlists per group.
    poll_interval/poll_timeout/sleep  forwarded into the step-5 status loop (so tests drive
                   pending->running->done with zero real sleeping).
    max_parallel   cap on how many GROUP jobs run on Donau at once (default 1 = strict serial,
                   identical to the old behavior); >1 launches up to the cap and polls them
                   together (Donau runs them in parallel).

    Returns a result dict with ALL artifact paths (manifest, netlist used, psf root, npz, va,
    model cell) + the fit report + the BY-TAG psf_map + per-group dsub commands (dsub_cmd is
    the first group's; dsub_cmds is the full list) -- ALWAYS, even in dry_run."""
    want = set(steps) if steps is not None else set(STEPS)
    # manifest-driven offline-sweep path: drive the run VERBATIM from a loaded manifest.
    # We already have m + RESOLVED nets, so steps 'resolve' and 'manifest' are skipped, and
    # the minimal gui fields the remaining steps read are synthesized from the manifest. The
    # gui-driven path (manifest=None) is untouched.
    manifest_driven = manifest is not None
    if manifest_driven:
        if gui is None:
            gui = _gui_from_manifest(manifest)
        want -= {"resolve", "manifest"}
    elif gui is None:
        raise PmuCornerError("run_pmu_corner needs either gui= (the GUI-driven path) or "
                             "manifest= (the manifest-driven offline-sweep path)")
    corner = corner or gui.get("corner") or "nom"
    base, dirs = corner_dir(work_root, gui, corner)
    supply_pin = supply_pin or (_bm.supply_pins(gui) or [""])[0]   # model symbol's LEFT input = first supply
    ground = ground or gui.get("ground") or "VSS"
    model_lib = model_lib or "LDO_model_lab"
    model_cell = model_cell or (gui.get("model_cell") or f"{gui.get('dut_cell', 'model')}_model")
    model_path = model_path or gui.get("model_path") or str(base / "model" / "cds" / model_lib)

    res = {
        "corner": corner, "work_root": str(resolve_work_root(work_root)),
        "corner_dir": str(base), "dirs": {k: str(v) for k, v in dirs.items()},
        "manifest": None, "netlistdir": None, "psf_dir": None, "psf_map": None, "npz": None,
        "va": None, "model_lib": model_lib, "model_cell": model_cell,
        "model_path": model_path, "model_built": False,
        "dsub_cmd": None, "dsub_cmds": None, "fit_report": None,
        "warnings": [], "steps_run": [],
    }

    def _ran(s):
        res["steps_run"].append(s)

    # 1) resolve ----------------------------------------------------------------
    if "resolve" in want:
        netmap = step_resolve(gui, netmap=netmap, session=session, progress=progress)
        _ran("resolve")
    # the manifest-driven path already carries resolved nets (m verbatim) -> no netmap needed
    if netmap is None and not manifest_driven:
        raise PmuCornerError("no netmap: include 'resolve' in steps= or pass netmap=")

    # 2) manifest ---------------------------------------------------------------
    man_path = dirs["netlist"].parent / f"{gui['tb_cell']}_{corner}.manifest.json"
    if manifest_driven:
        # the manifest is the source of truth -- use it verbatim, persist a copy in the
        # workarea for traceability, and surface its warnings (no build_manifest re-derive).
        m = manifest
        man_path.parent.mkdir(parents=True, exist_ok=True)
        man_path.write_text(json.dumps(m, indent=2) + "\n")
        res["manifest"] = str(man_path)
        res["warnings"] = list(m.get("_warnings", []))
    elif "manifest" in want:
        m, man_path = step_manifest(gui, netmap, out_path=man_path, progress=progress)
        res["manifest"] = str(man_path)
        res["warnings"] = list(m.get("_warnings", []))
        _ran("manifest")
    else:
        m = _bm.build_manifest(gui, netmap)

    # 3) augment ----------------------------------------------------------------
    if "augment" in want and not netlistdir:
        # a provided netlistdir means the extract TB was already built + netlisted -> skip
        step_augment(m, session=session, dry_run=dry_run, progress=progress)
        _ran("augment")
    elif "augment" in want:
        _progress(progress, "augment", "skipped (netlistdir provided -> TB already augmented)")

    # 4) netlist (HANDOFF) ------------------------------------------------------
    netinfo = None
    run_possible = bool(netlistdir)
    if "netlist" in want and run_possible:
        netinfo = step_netlist(dirs, netlistdir=netlistdir, ahdllibdir=ahdllibdir,
                               pdk_model_dir=pdk_model_dir, progress=progress)
        res["netlistdir"] = netinfo["netlistdir"]
        _ran("netlist")
    elif "netlist" in want:
        _progress(progress, "netlist",
                  "no netlistdir handoff -> stopping before the run (plan-only). Provide "
                  "netlistdir/ahdllibdir/pdk_model_dir to run a corner.")

    # 5) run_corner -- the PER-GROUP SWEEP --------------------------------------
    # The step-5 seam: group_netlister(group) -> the netlist dir for that group's one-hot.
    # DEFAULT: live session -> ade_group_netlist (box-only, netlists each one-hot in ADE);
    # offline (no session) -> reuse the handoff netlistdir for every group, so the per-group
    # dsub commands still assemble for the plan / fake-runner path.
    psf_root = dirs["psf"]
    gnl = group_netlister
    # per_group_netlist: True when each group gets its OWN one-hot netlist (injected fake, or
    # the live ADE default); False for the offline fallback that reuses ONE handoff netlistdir
    # for every group (dsub commands assemble, but they would all run the SAME netlist -- a
    # shape-only plan, never an executable sweep). Surfaced in res so a caller can't mistake the
    # offline plan for a real sweep.
    per_group_netlist = group_netlister is not None
    if gnl is None and netinfo:
        if session is not None and not dry_run:
            gnl = _live_group_netlister(session, session_name, extract_test, m,
                                        psf_root.parent / "netlist")
            per_group_netlist = True
        else:
            gnl = (lambda g, _nd=netinfo["netlistdir"]: _nd)
            per_group_netlist = False
            _progress(progress, "run", "OFFLINE: all groups share one -EP <netlistdir> "
                      "(shape-only plan; the box default per-group-netlists each one-hot)")
    res["per_group_netlist"] = per_group_netlist
    if "run" in want and netinfo:
        runres = step_run(netinfo, psf_root, m, engine=engine, donau=donau, runner=runner,
                          on_status=on_status, dry_run=dry_run, progress=progress,
                          group_netlister=gnl, poll_interval=poll_interval,
                          max_parallel=max_parallel, poll_timeout=poll_timeout, sleep=sleep)
        res["dsub_cmds"] = runres["dsub_cmds"]
        res["dsub_cmd"] = runres["dsub_cmds"][0] if runres["dsub_cmds"] else None
        res["psf_map"] = runres["psf_map"]
        res["psf_dir"] = str(psf_root)
        _ran("run")
    elif "run" in want and netlistdir:
        # netlistdir present but step skipped wiring -- still surface the per-group dsub cmds
        netinfo = netinfo or step_netlist(dirs, netlistdir=netlistdir, ahdllibdir=ahdllibdir,
                                          pdk_model_dir=pdk_model_dir, progress=progress)
        gnl = gnl or (lambda g, _nd=netinfo["netlistdir"]: _nd)
        cmds = [_runc.run_corner(
            gnl(g), netinfo["pdk_model_dir"], netinfo["ahdllibdir"],
            str(psf_root / g["tag"]), engine=engine, donau=donau or DonauCfg(), dry_run=True)
            for g in _run.groups(m)]
        res["dsub_cmds"] = cmds
        res["dsub_cmd"] = cmds[0] if cmds else None

    # the manifest used for the read/fit/emit side (pairs with the npz). Defaults to the GUI
    # manifest (real-box: the real run's npz matches it); an injected fit_manifest is used when
    # the injected npz has a different port topology (the offline stand-in).
    fm = fit_manifest or m

    # 6) import the BY-TAG psf_map -> npz ----------------------------------------
    # The read side is the dual of the per-group stimulus: importmp reads EACH measurement's
    # PSF by its TAG (each tag -> its group's PSF dir, from step_run's BY-TAG psf_map), never
    # by corner. npz_in= bypasses the read entirely (offline stand-in).
    npz_path = None
    have_psf = bool(res["psf_map"])
    if "import" in want and (npz_in is not None or have_psf):
        npz_path = step_import(fm, res["psf_map"] or {}, npz_dir=dirs["npz"], npz_in=npz_in,
                               load=corner if npz_in is None else None, progress=progress)
        res["npz"] = str(npz_path)
        _ran("import")
    elif "import" in want:
        _progress(progress, "import",
                  "no PSF and no npz_in -> skipping fit/emit (plan stopped before the run)")

    # 7) fit --------------------------------------------------------------------
    fit_result = None
    if "fit" in want and npz_path is not None:
        fit_result, txt = step_fit(npz_path, fm, progress=progress)
        res["fit_report"] = txt
        _ran("fit")

    # 8) emit -------------------------------------------------------------------
    if "emit" in want and fit_result is not None:
        va_path = dirs["model"] / f"{model_cell}.va"
        p = step_emit(fit_result, cell_name=model_cell, va_path=va_path,
                      supply=supply_pin, ground=ground, progress=progress)
        res["va"] = str(p)
        _ran("emit")

    # 9) cell build -------------------------------------------------------------
    if "cell" in want and res["va"] is not None:
        cellres = step_cell(fit_result, res["va"], model_lib=model_lib, model_cell=model_cell,
                            model_path=model_path, supply=supply_pin, ground=ground,
                            session=session, dry_run=dry_run, progress=progress)
        res["model_built"] = cellres["built"]
        res["cell_plan"] = cellres["plan"]
        _ran("cell")

    return res


# ===================================================================== coverage sweep
# STAGE 1b part 2: the LOAD x TEMPERATURE coverage SWEEP. A NEW, SEPARATE driver that REUSES
# the single-corner step functions (step_netlist / step_run / step_fit / step_emit / step_cell)
# -- it reimplements NONE of them. The single-corner run_pmu_corner is UNCHANGED; only step_run
# gained the additive `groups=` kwarg above, so a sweep cell can run a PRE-SPLIT subset of groups.
#
# WHY a sweep, and HOW it routes (HANDOFF_MODELING_COVERAGE §1, §3, §6):
#   * small-signal (1x AC + .noise) is LTI AT THE OP, so its transfer functions must be
#     re-extracted at EACH load point -> these "load-swept" groups REPEAT across the load axis.
#   * the dc (I-V / dropout) + tran (slew) + 2x-lin-gate groups characterize the load /
#     large-signal dimension INTERNALLY (a DC sweep already walks the load) -> they run ONCE at
#     the TB OP, not per load point.
#   * temperature is an OUTER axis: every cell (once + per-load) repeats per declared temp.
# Each (groups-subset, op_loads, temp) is ONE "cell": its own netlist dir + PSF dir under the
# workarea corner dir, read into a per-cell arrays dict keyed by the cell LABEL, then ALL cells
# are assembled into ONE sweep npz the existing fit/emit consume.

def _has_coverage_sweep(m):
    """True when a manifest declares a coverage SWEEP (>1 load point on some rail, OR any temp
    corner) -- so a stage-3 GUI can dispatch run_pmu_corner (single OP) vs run_pmu_coverage_sweep.
    Pure (no I/O). A coverage-free manifest (no loads, no temps) is False -> single-corner path."""
    return len(_run.load_axis(m)) > 1 or bool(_manifest.temps(m))


def run_pmu_coverage_sweep(manifest, *, work_root=None, corner=None, engine="alps",
                           netlistdir=None, ahdllibdir=None, pdk_model_dir=None,
                           base_netlist=None, model_lib=None, model_cell=None, model_path=None,
                           supply_pin=None, ground=None, runner=None, on_status=None,
                           dry_run=False, progress=None, netlister_factory=None, donau=None,
                           poll_interval=5.0, max_parallel=1, poll_timeout=10800.0, sleep=None,
                           steps=None):
    """Run the LOAD x TEMPERATURE coverage SWEEP for ONE process corner (the STAGE-1b driver).

    REUSES run_pmu_corner's step functions per CELL -- a cell = one (groups-subset, op_loads,
    temp). Routing (see the module comment + HANDOFF §1/§3/§6): the load-swept small-signal
    groups (1x AC + .noise) REPEAT across the load axis; the dc/tran/2x-lin-gate groups run ONCE
    at the TB OP; temperature is the outer axis (every cell repeats per declared temp). With NO
    load sweep, ALL groups run once per temp. Each cell gets its OWN netlist + PSF dir under the
    workarea corner dir; all cells assemble into ONE sweep npz the existing fit/emit consume.

    manifest         a LOADED manifest dict (the source of truth; drives the run verbatim).
    work_root        writable $WORK_ROOT (else env WORK_ROOT, else ~/ldo_workarea).
    corner           process-corner label (else manifest['corner'] / corners.fallback[0]).
    engine           'alps' (default) | 'spectre'.
    netlistdir/ahdllibdir/pdk_model_dir   the step-4 HANDOFF (the pdk/ahdl the sweep runs need,
                     exactly like the single path's step_netlist).
    base_netlist     the base maestro input.scs (dir or file) the DEFAULT netlister_factory
                     rewrites per cell. REQUIRED unless netlister_factory= is injected.
    netlister_factory(op_loads, temp, out_base) -> callable(group)->netlistdir : the seam tests
                     inject. DEFAULT builds cluster.netlist_augment.make_offline_group_netlister
                     from base_netlist + the manifest (op_loads rewrites each rail's reused load
                     isource dc=; temp= emits the options temp= line).
    runner/on_status/dry_run/progress/donau/poll_interval/max_parallel/poll_timeout/sleep
                     forwarded into step_run per cell (so tests drive the status loop with zero
                     real sleeping). dry_run assembles every cell's dsub command, executes nothing.
    steps            an allow-list through 'emit' (default: netlist..emit; 'cell' only with a
                     live session, which this offline driver does not take -> a dry plan).

    Returns a result dict (corner, corner_dir, dirs, manifest copy path, netlistdir, psf_dir,
    npz, va, model_lib/cell/path, dsub_cmds, dsub_cmd, loads (the cell labels), temps, load_swept,
    guardrail_warnings, warnings, steps_run)."""
    import numpy as np
    m = manifest
    gui = _gui_from_manifest(m)
    corner = corner or m.get("corner") or (m.get("corners", {}).get("fallback") or ["nom"])[0]
    base, dirs = corner_dir(work_root, gui, corner)

    # resolve the model + supply/ground exactly like run_pmu_corner's manifest-driven branch
    supply_pin = supply_pin or (_bm.supply_pins(gui) or [""])[0]
    ground = ground or gui.get("ground") or "VSS"
    model_lib = model_lib or "LDO_model_lab"
    model_cell = model_cell or (gui.get("model_cell") or f"{gui.get('dut_cell', 'model')}_model")
    model_path = model_path or gui.get("model_path") or str(base / "model" / "cds" / model_lib)

    want = set(steps) if steps is not None else {"netlist", "run", "import", "fit", "emit", "cell"}
    res = {
        "corner": corner, "work_root": str(resolve_work_root(work_root)),
        "corner_dir": str(base), "dirs": {k: str(v) for k, v in dirs.items()},
        "manifest": None, "netlistdir": None, "psf_dir": str(dirs["psf"]), "npz": None,
        "va": None, "model_lib": model_lib, "model_cell": model_cell, "model_path": model_path,
        "model_built": False, "dsub_cmds": [], "dsub_cmd": None,
        "loads": [], "temps": [], "load_swept": False,
        "guardrail_warnings": [], "warnings": list(m.get("_warnings", [])), "steps_run": [],
    }

    def _ran(s):
        if s not in res["steps_run"]:
            res["steps_run"].append(s)

    # persist a workarea copy of the manifest for traceability (mirrors run_pmu_corner) ----
    man_path = dirs["netlist"].parent / f"{gui['tb_cell']}_{corner}.manifest.json"
    man_path.parent.mkdir(parents=True, exist_ok=True)
    man_path.write_text(json.dumps(m, indent=2) + "\n")
    res["manifest"] = str(man_path)

    # 2) the pdk/ahdl handoff -- the sweep needs it like the single path -------------------
    netinfo = step_netlist(dirs, netlistdir=netlistdir, ahdllibdir=ahdllibdir,
                           pdk_model_dir=pdk_model_dir, progress=progress)
    res["netlistdir"] = netinfo["netlistdir"]
    _ran("netlist")

    # 3) the NETLISTER FACTORY seam: netlister_factory(op_loads, temp, out_base) -> per-group
    #    netlister. DEFAULT (None) builds the offline cluster.netlist_augment netlister from the
    #    base input.scs + the manifest. A missing base AND no factory cannot make a netlist.
    if netlister_factory is None:
        if base_netlist is None:
            raise PmuCornerError(
                "run_pmu_coverage_sweep needs a base netlist to sweep: pass base_netlist= (the "
                "maestro input.scs dir/file the offline netlister rewrites per cell) or inject "
                "netlister_factory=.")
        from cluster import netlist_augment as _na
        netlister_factory = (lambda op_loads, temp, out_base:
                             _na.make_offline_group_netlister(base_netlist, m, out_base,
                                                              op_loads=op_loads, temp=temp))

    # 4) the axes + the group split (ONCE) ------------------------------------------------
    temps_axis = _manifest.temps(m) or [None]
    load_axis = _run.load_axis(m)
    load_swept = len(load_axis) > 1
    res["load_swept"] = load_swept
    res["temps"] = list(temps_axis)
    all_grps = _run.groups(m)
    swept = [g for g in all_grps if _run.is_load_swept_group(g)]      # 1x AC + noise (per load)
    once = [g for g in all_grps if not _run.is_load_swept_group(g)]   # dc/tran/2x (at the OP)

    # 5) the per-cell runner: run ONE cell (a groups-subset at one op_loads+temp). Each cell has
    #    its OWN netlist + PSF dir under <corner>/{netlist,psf}/<cell_label> (never /simulation/).
    #    On a real run we read the cell's PSF -> arrays keyed by the cell LABEL (strict=False: a
    #    cell ran only ITS subset, so the other measurement tags are legitimately absent + skipped).
    cmds, merged, labels = [], {}, []

    def _cell(groups_subset, op_loads, temp, cell_label):
        if not groups_subset:
            return {}
        nl_dir = dirs["netlist"] / cell_label
        psf_cell = dirs["psf"] / cell_label
        psf_cell.mkdir(parents=True, exist_ok=True)
        gnl = netlister_factory(op_loads, temp, nl_dir)
        rr = step_run(netinfo, psf_cell, m, engine=engine, donau=donau, runner=runner,
                      on_status=on_status, dry_run=dry_run, progress=progress,
                      group_netlister=gnl, groups=groups_subset, poll_interval=poll_interval,
                      max_parallel=max_parallel, poll_timeout=poll_timeout, sleep=sleep)
        cmds.extend(rr["dsub_cmds"])
        if rr.get("psf_map"):
            # read the cell's PSF by TAG; key each array by the cell LABEL (the sweep's "load")
            reads = _imp.from_psf_multiport(
                manifest=m, psf_map={k: str(v) for k, v in rr["psf_map"].items()},
                load=cell_label, strict=False)
            reads.pop("_skipped", None)            # a per-cell subset legitimately skips the rest
            merged.update(reads)
        if cell_label not in labels:
            labels.append(cell_label)              # a cell that actually ran -> its label is real
        return rr

    def _lbl(load_label, temp):
        t = None if temp is None else f"T{temp:g}"
        base_l = load_label or "Lnom"
        return base_l if t is None else f"{base_l}_{t}"

    # 6) the LOOP -- per HANDOFF routing. With a load sweep: the once-groups run at the TB OP
    #    (op_loads=None) per temp, then the swept groups repeat per load point. With NO load
    #    sweep: ALL groups run once per temp.
    if "run" in want:
        for temp in temps_axis:
            if load_swept:
                _cell(once, None, temp, _lbl("Lnom", temp))            # dc/tran/2x at the OP, once
                for (load_label, op) in load_axis:
                    _cell(swept, op, temp, _lbl(load_label, temp))     # AC/noise per load point
            else:
                _cell(all_grps, None, temp, _lbl(None, temp))          # everything once per temp
        _ran("run")
    res["dsub_cmds"] = cmds
    res["dsub_cmd"] = cmds[0] if cmds else None
    res["loads"] = labels

    # 7) assemble ONE sweep npz: every cell's arrays + the cell labels + per-cell meta (the rail
    #    iload + the temp per label, NaN where a cell did not set that rail / had no temp). On a
    #    pure dry_run (no PSF read) merged is empty -> we still wrote the manifest copy above and
    #    return the assembled dsub commands (no npz).
    npz_path = None
    if merged:
        ref = {"loads": np.array(labels), **merged}
        # meta_iload_<o>: the rail's iload per label (NaN where that cell did not set the rail).
        # We re-derive each cell's op_loads from its label by replaying the loop bookkeeping.
        label_ops, label_temp = _sweep_label_meta(load_axis, temps_axis, load_swept, _lbl)
        for o in m["v_out"]:
            ref[f"meta_iload_{o}"] = np.array(
                [label_ops.get(lbl, {}).get(o, float("nan")) for lbl in labels], dtype=float)
        ref["meta_temp"] = np.array(
            [label_temp.get(lbl, float("nan")) for lbl in labels], dtype=float)
        npz_path = dirs["npz"] / f"{m['name']}_sweep.npz"
        np.savez(npz_path, **ref)
        res["npz"] = str(npz_path)
        _ran("import")
        _progress(progress, "import",
                  f"assembled sweep npz: {npz_path.name} ({len(labels)} cells, "
                  f"{len(merged)} arrays)")

        # 8) GUARDRAIL-3: Zout(s->0) <-> DC load-reg consistency (only when there is data) ----
        warns = _imp.check_zout_dc_consistency(ref, m)
        if warns:
            res["guardrail_warnings"] = warns
            for w in warns:
                _progress(progress, "import", "GUARDRAIL-3: " + w)
                if on_status:
                    on_status("guardrail", w)

    # 9) fit -> emit (-> cell only with a live session; this offline driver -> a dry plan). Reuse
    #    the existing step functions on the assembled npz, exactly like run_pmu_corner does.
    fit_result = None
    if "fit" in want and not dry_run and npz_path is not None:
        fit_result, txt = step_fit(npz_path, m, progress=progress)
        res["fit_report"] = txt
        _ran("fit")
    if "emit" in want and fit_result is not None:
        va_path = dirs["model"] / f"{model_cell}.va"
        p = step_emit(fit_result, cell_name=model_cell, va_path=va_path,
                      supply=supply_pin, ground=ground, progress=progress)
        res["va"] = str(p)
        _ran("emit")
    if "cell" in want and res["va"] is not None:
        # no session param here -> step_cell degrades to the SKILL PLAN (dry). A live cell build
        # is a stage-3 GUI concern (it owns the session); the sweep driver stops at the .va.
        cellres = step_cell(fit_result, res["va"], model_lib=model_lib, model_cell=model_cell,
                            model_path=model_path, supply=supply_pin, ground=ground,
                            session=None, dry_run=True, progress=progress)
        res["model_built"] = cellres["built"]
        res["cell_plan"] = cellres["plan"]
        _ran("cell")

    return res


def _sweep_label_meta(load_axis, temps_axis, load_swept, lbl_fn):
    """Reconstruct, per cell LABEL, its op_loads dict + its temp -- the meta the sweep npz stamps.
    Pure bookkeeping that REPLAYS run_pmu_coverage_sweep's loop label scheme (so the meta arrays
    line up with the `loads` label array). Returns (label_ops, label_temp)."""
    label_ops, label_temp = {}, {}
    for temp in temps_axis:
        tval = float("nan") if temp is None else float(temp)
        if load_swept:
            label_ops[lbl_fn("Lnom", temp)] = {}                 # the OP once-cell sets no rail load
            label_temp[lbl_fn("Lnom", temp)] = tval
            for (load_label, op) in load_axis:
                label_ops[lbl_fn(load_label, temp)] = dict(op or {})
                label_temp[lbl_fn(load_label, temp)] = tval
        else:
            label_ops[lbl_fn(None, temp)] = {}
            label_temp[lbl_fn(None, temp)] = tval
    return label_ops, label_temp


# --------------------------------------------------------------------------- CLI
def _load_gui(path):
    return json.loads(pathlib.Path(path).read_text())


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(
        description="Run ONE process corner of in-situ PMU LDO modeling (pure-CLI ALPS path)")
    ap.add_argument("--gui", required=True, help="JSON file of the GUI inputs (build_manifest schema)")
    ap.add_argument("--work-root", default=None, help="$WORK_ROOT (else env WORK_ROOT / ~/ldo_workarea)")
    ap.add_argument("--corner", default=None, help="process-corner label (else gui['corner'])")
    ap.add_argument("--engine", default="alps", choices=("alps", "spectre"))
    ap.add_argument("--netmap", default=None, help="JSON {pin: net} to skip the resolver (offline)")
    ap.add_argument("--netlistdir", default=None, help="ADE-prepared netlist dir (input.scs)")
    ap.add_argument("--ahdllibdir", default=None, help="compiled AHDL/VA DB (-ahdllibdir)")
    ap.add_argument("--pdk-model-dir", default=None, help="PDK model root ({alps,spectre} subtrees)")
    ap.add_argument("--npz-in", default=None, help="inject a ready npz at step 6 (bypass PSF read)")
    ap.add_argument("--model-lib", default=None)
    ap.add_argument("--model-cell", default=None)
    ap.add_argument("--model-path", default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="no Cadence + no cluster exec: write manifest + plans + dsub cmd")
    a = ap.parse_args()
    gui = _load_gui(a.gui)
    netmap = _load_gui(a.netmap) if a.netmap else None

    def _status(state, raw):                       # surface Donau transitions to the user
        print(f"  Donau: {state.upper()}")

    out = run_pmu_corner(
        gui, work_root=a.work_root, corner=a.corner, engine=a.engine, netmap=netmap,
        netlistdir=a.netlistdir, ahdllibdir=a.ahdllibdir, pdk_model_dir=a.pdk_model_dir,
        npz_in=a.npz_in, model_lib=a.model_lib, model_cell=a.model_cell,
        model_path=a.model_path, on_status=_status, dry_run=a.dry_run)
    print("\n=== result ===")
    for k in ("corner_dir", "manifest", "netlistdir", "psf_dir", "npz", "va",
              "model_lib", "model_cell", "model_built"):
        print(f"  {k:12s}: {out.get(k)}")
    if out.get("dsub_cmds"):
        print(f"  dsub jobs   : {len(out['dsub_cmds'])} (one per measurement GROUP)")
        for c in out["dsub_cmds"]:
            print("    " + " ".join(str(x) for x in c))
    elif out.get("dsub_cmd"):
        print("  dsub_cmd    : " + " ".join(str(x) for x in out["dsub_cmd"]))
    if out.get("warnings"):
        print("  warnings    :")
        for w in out["warnings"]:
            print(f"    - {w}")
