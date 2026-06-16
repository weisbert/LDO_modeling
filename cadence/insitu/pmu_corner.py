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
            "ADE work dir holding input.scs), ahdllibdir= (the compiled AHDL/VA DB), and "
            "pdk_model_dir= (the PDK model root). On the company box, netlist the "
            "<tb>_extract cellview in ADE (it pre-compiles the Verilog-A) and hand us those "
            "three paths. [future hook: ADE-triggered netlisting from the augmented TB]")
    if not ahdllibdir or not pdk_model_dir:
        raise PmuCornerError(
            "step 4 (netlist): netlistdir given but ahdllibdir= and/or pdk_model_dir= missing "
            "-- run_corner needs the compiled -ahdllibdir and the PDK model root.")
    _progress(progress, "netlist",
              f"handoff netlist dir: {netlistdir}  (ahdllibdir + pdk_model_dir provided)")
    return dict(netlistdir=str(netlistdir), ahdllibdir=str(ahdllibdir),
                pdk_model_dir=str(pdk_model_dir))


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
             poll_interval=5.0, poll_timeout=10800.0, sleep=None):
    """5) run ONE corner as a PER-GROUP SWEEP via the pure-CLI Donau+ALPS path (Component A).

    grps = run.groups(m). For each group g (i/N) we get the group's netlist dir from the
    INJECTABLE seam group_netlister(g) -> netlistdir (each holding ONE acm one-hot's
    input.scs), then cluster.run_corner(group_netlistdir, pdk, ahdl, out=<psf_root>/<g.tag>)
    -> the group's PSF dir; psf_map[pt.tag] = that PSF dir for EVERY member pt of g.

    Returns dict(psf_map (BY MEASUREMENT TAG), dsub_cmds (one per group), ran). dry_run
    assembles + returns ALL per-group dsub commands WITHOUT executing. on_status is wrapped
    to prefix 'group i/N {g.tag}'. poll_interval/poll_timeout/sleep are forwarded into
    run_corner so tests drive the status loop with zero real sleeping.

    group_netlister(group) -> netlistdir is the seam: the DEFAULT (ade_group_netlist, bound by
    run_pmu_corner with the live ws/session/test) reuses the run_ade wiring to netlist each
    one-hot on the box; tests inject a fake that writes a stub input.scs per group."""
    cfg = donau or DonauCfg()
    grps = _run.groups(m)
    n = len(grps) or 1
    psf_root = pathlib.Path(psf_root)
    psf_map, dsub_cmds = {}, []

    for i, g in enumerate(grps):
        tag = g["tag"]
        group_netdir = group_netlister(g)             # the seam -> this group's netlist dir
        group_psf = psf_root / tag

        # the assembled command is ALWAYS available (run_corner dry_run never executes)
        dsub_cmd = _runc.run_corner(
            group_netdir, netinfo["pdk_model_dir"], netinfo["ahdllibdir"], str(group_psf),
            engine=engine, donau=cfg, dry_run=True)
        dsub_cmds.append(dsub_cmd)

        def _status(state, raw, _i=i, _tag=tag):
            _progress(progress, "run", f"group {_i+1}/{n} {_tag}: Donau job {state.upper()}")
            if on_status:
                on_status(state, raw)

        if dry_run:
            _progress(progress, "run",
                      f"DRY -- group {i+1}/{n} {tag}: assembled dsub command, not executing")
            continue

        _progress(progress, "run", f"group {i+1}/{n} {tag}: submitting")
        out = _runc.run_corner(
            group_netdir, netinfo["pdk_model_dir"], netinfo["ahdllibdir"], str(group_psf),
            engine=engine, donau=cfg, on_status=_status, runner=runner, dry_run=False,
            poll_interval=poll_interval, poll_timeout=poll_timeout, sleep=sleep)
        _progress(progress, "run", f"group {i+1}/{n} {tag}: PSF landed: {out}")
        for pt in g["members"]:                        # map EVERY member tag at this group PSF
            psf_map[pt["tag"]] = str(out)

    return dict(psf_map=psf_map, dsub_cmds=dsub_cmds, ran=not dry_run)


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


def step_emit(fit_result, *, cell_name, va_path, supply, ground, progress=None):
    """8) emit the ONE combined PMU Verilog-A module (Component D). Ports are the GUI symbol
    PIN names (propagated by fit_multiport as each port's 'pin'). Returns the .va path."""
    p = _emit.emit_pmu_va(fit_result, cell_name, va_path, supply=supply, ground=ground)
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
def run_pmu_corner(gui, work_root=None, corner=None, engine="alps",
                   session=None, netmap=None, netlistdir=None, ahdllibdir=None,
                   pdk_model_dir=None, model_lib=None, model_cell=None, model_path=None,
                   runner=None, on_status=None, dry_run=False, steps=None,
                   donau=None, npz_in=None, fit_manifest=None, supply_pin=None,
                   ground=None, progress=None, group_netlister=None,
                   extract_test="insitu_extract", session_name="fnxSession0",
                   poll_interval=5.0, poll_timeout=10800.0, sleep=None):
    """Run ONE process corner of in-situ PMU LDO modeling end-to-end (the 9-step flow).

    gui            the designer's GUI inputs (build_manifest schema: tb_lib/tb_cell/tb_view,
                   dut_inst/dut_lib/dut_cell, supply={pin,dc}, v_outs=[pin], i_outs=[pin],
                   ground, corner, optional biases/iload/vdc).
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
    poll_interval/poll_timeout/sleep  forwarded into each group's run_corner status loop (so
                   tests drive pending->running->done with zero real sleeping).

    Returns a result dict with ALL artifact paths (manifest, netlist used, psf root, npz, va,
    model cell) + the fit report + the BY-TAG psf_map + per-group dsub commands (dsub_cmd is
    the first group's; dsub_cmds is the full list) -- ALWAYS, even in dry_run."""
    want = set(steps) if steps is not None else set(STEPS)
    corner = corner or gui.get("corner") or "nom"
    base, dirs = corner_dir(work_root, gui, corner)
    supply_pin = supply_pin or (_bm.supply_pins(gui) or [""])[0]   # model symbol's LEFT input = first supply
    ground = ground or gui.get("ground") or "VSS"
    model_lib = model_lib or "LDO_model_lab"
    model_cell = model_cell or (gui.get("model_cell") or f"{gui['dut_cell']}_model")
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
    if netmap is None:
        raise PmuCornerError("no netmap: include 'resolve' in steps= or pass netmap=")

    # 2) manifest ---------------------------------------------------------------
    man_path = dirs["netlist"].parent / f"{gui['tb_cell']}_{corner}.manifest.json"
    if "manifest" in want:
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
                          poll_timeout=poll_timeout, sleep=sleep)
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
