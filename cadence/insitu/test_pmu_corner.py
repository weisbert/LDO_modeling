"""HONEST end-to-end tests for the PMU-corner ORCHESTRATOR -- NO sim / Virtuoso / dsub.

The step-5 architecture is a PER-GROUP SWEEP: one cluster job per measurement GROUP
(run.groups(m) -- AC merges by (analysis, one-hot stimulus); NOISE is per-output), each
netlisting exactly one acm_* one-hot + only that group's analysis, each landing its own PSF
dir. The importer maps EACH group's PSF dir to its member measurement tags (psf_map BY TAG).

Nothing papers over the box steps:
  (A) PER-GROUP SWEEP -- real-PMU GUI + injected netmap + a FAKE group_netlister (writes a
      stub input.scs per group) + a FAKE cluster runner scripting dsub/djob per group +
      sleep=lambda*_:None. Asserts: groups submitted == len(run.groups(real_manifest));
      psf_map keyed by TAG covering ALL measurement tags; each group's dsub is the validated
      alps tuple; per-group status surfaced; artifacts under the workarea, not /simulation/.
  (B) REAL IMPORT -- drives the ACTUAL importmp reader (NOT an injected npz): a real BY-TAG
      psf_map from run.run_spectre_cli over cadence/work_pmu, fed through step_import ->
      fit_multiport -> emit_pmu_va writes a .va. The STAND-IN 2-rail/2-sink topology (the
      real 3+3+1 needs a box run). Skips with a message if work_pmu is absent.
  (C) DRY-RUN -- per-group dsub commands assembled, NOTHING executed (Boom never fires).
  (D) resolve-without-session-or-netmap -> actionable error; WORK_ROOT/corner-dir layout.

Run:  python -m pytest cadence/insitu/test_pmu_corner.py -q
"""
import json
import pathlib
import re
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # .../cadence on sys.path (bare-import convention)

from insitu import pmu_corner as PC                                        # noqa: E402
from insitu import build_manifest as BM                                    # noqa: E402
from insitu import manifest as M                                           # noqa: E402
from insitu import run as RUN                                              # noqa: E402
from insitu import importmp as IMP                                         # noqa: E402
from cluster.donau import RunResult                                        # noqa: E402
from cluster import netlist_augment as NA                                  # noqa: E402

import fit_multiport as FIT                                                # noqa: E402
import emit_pmu_model as EMIT                                              # noqa: E402

ROOT = HERE.parents[1]                          # .../LDO_modeling (HERE=.../cadence/insitu)
STANDIN_MANIFEST = HERE / "manifests" / "pmu_top.json"
WUR_MANIFEST = HERE / "manifests" / "wur_pmu_top.json"


# ----------------------------------------------------------------- the REAL PMU GUI
def real_pmu_gui():
    """The fixed real-PMU GUI inputs (1 supply + 3 voltage rails + 3 bias currents)."""
    return dict(
        tb_lib="PMU_TOP_TB", tb_cell="pmu_tb", tb_view="schematic", dut_inst="I0",
        dut_lib="PMU_TOP", dut_cell="pmu_top",
        supply={"pin": "AVDD1P0", "dc": 1.0, "tb_src": "V_AVDD"},
        v_outs=["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"],
        i_outs=["IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit", "IBP_PTAT_TUNE_1P5U_VCO"],
        ground="VSS", corner="tt_25c",
        # NB: no vdc per i_out -> the no-compliance warning MUST fire for all 3 currents.
    )


def real_netmap(gui):
    """Inject {pin: net} so step 1 is satisfied offline (a resolver gap is not our concern)."""
    pins = [gui["supply"]["pin"], *gui["v_outs"], *gui["i_outs"]]
    return {p: f"net_{p}" for p in pins}


def real_manifest():
    """The manifest the orchestrator builds for the real-PMU GUI (used to predict groups)."""
    gui = real_pmu_gui()
    return BM.build_manifest(gui, real_netmap(gui))


# ----------------------------------------------------------------- the FAKE cluster runner
class FakeRunner:
    """The cadence/cluster fake-runner style: record every argv, reply from a scripted table
    keyed by the leading token (dsub/djob/...). For djob a per-JOBID PENDING->RUNNING->DONE
    sequence is replayed (so EVERY group's poll loop walks all three states)."""

    def __init__(self):
        self.calls = []
        self._djob_i = 0

    def __call__(self, argv, timeout=None, check=False):
        self.calls.append(list(argv))
        head = argv[0]
        if head == "dsub":
            self._djob_i = 0                    # a new job -> its djob poll restarts at PENDING
            return RunResult(0, "Submit job successfully. JOBID 37238970\n", "", list(argv))
        if head == "djob":
            seq = ["JobId: 37238970  State: PENDING  Queue: short\n",
                   "JobId: 37238970  State: RUNNING  Node: sinct20-hs\n",
                   "JobId: 37238970  State: DONE  Exit: 0\n"]
            r = seq[min(self._djob_i, len(seq) - 1)]
            self._djob_i += 1
            return RunResult(0, r, "", list(argv))
        raise AssertionError(f"FakeRunner: no scripted reply for {head!r} ({argv})")

    def cmds(self, head):
        return [c for c in self.calls if c and c[0] == head]


def _seed_handoff(tmp_path):
    """The step-4 handoff: a compiled ahdllibdir + a pdk root (the per-group netlist dirs are
    produced by the fake group_netlister, not this)."""
    ahdl = tmp_path / "handoff" / "input.ahdlSimDB"
    ahdl.mkdir(parents=True)
    pdk = tmp_path / "pdk" / "c1x_plus"
    (pdk / "alps").mkdir(parents=True)
    netdir = tmp_path / "handoff" / "netlist"
    netdir.mkdir(parents=True)
    (netdir / "input.scs").write_text("// ADE handoff netlist (stub)\n")
    return str(netdir), str(ahdl), str(pdk)


def make_fake_group_netlister(root, written):
    """A FAKE group_netlister: per group, write a stub input.scs into a per-group netlist dir
    and return that dir. Records each (tag, dir) it produced so the test can assert one
    netlist per group."""
    root = pathlib.Path(root)

    def _netlister(group):
        d = root / "gnetlist" / group["tag"]
        d.mkdir(parents=True, exist_ok=True)
        (d / "input.scs").write_text(
            f"// per-group netlist: one-hot {group['hot']} analysis {group['analysis']}\n")
        written.append((group["tag"], str(d)))
        return str(d)

    return _netlister


def _prime_group_psf(work_root, gui, corner, m):
    """Pre-fill EACH group's psf subdir under the workarea so run_corner's _verify_psf passes
    with the fake runner (the sim 'already produced' output, like cadence/cluster's _fake_psf).
    Per-group: <corner>/psf/<g.tag>/{ac.ac|noise.noise, .simDone}."""
    base, dirs = PC.corner_dir(work_root, gui, corner)
    for g in RUN.groups(m):
        gd = dirs["psf"] / g["tag"]
        gd.mkdir(parents=True, exist_ok=True)
        ext = "noise.noise" if g["analysis"] == "noise" else "ac.ac"
        (gd / ext).write_bytes(b"PSFversion")
        (gd / ".simDone").write_bytes(b"")
    return dirs


# =====================================================================================
# (A) PER-GROUP SWEEP: real GUI + injected netmap + FAKE group_netlister + FAKE runner
# =====================================================================================
def test_per_group_sweep_real_runner(tmp_path):
    gui = real_pmu_gui()
    corner = gui["corner"]
    netmap = real_netmap(gui)
    m = real_manifest()
    grps = RUN.groups(m)
    netdir, ahdl, pdk = _seed_handoff(tmp_path)
    _prime_group_psf(tmp_path, gui, corner, m)        # per-group sim output 'already there'

    runner = FakeRunner()
    written = []
    gnl = make_fake_group_netlister(tmp_path, written)

    seen = []
    res = PC.run_pmu_corner(
        gui, work_root=str(tmp_path), corner=corner, engine="alps",
        session=None, netmap=netmap,
        netlistdir=netdir, ahdllibdir=ahdl, pdk_model_dir=pdk,
        runner=runner, group_netlister=gnl,
        on_status=lambda st, raw: seen.append(st),
        sleep=lambda *_: None,                         # zero real sleeping
        # run-only: the import/fit/emit side needs a real read (covered in test B); here we
        # focus on the SWEEP, so stop after the run.
        steps=["resolve", "manifest", "augment", "netlist", "run"])

    # -- ONE cluster job submitted per measurement GROUP --------------------------------
    n = len(grps)
    assert n == 10, f"real PMU should collapse to 10 groups, got {n}"
    assert len(runner.cmds("dsub")) == n, "one dsub submit per group"
    assert res["dsub_cmds"] and len(res["dsub_cmds"]) == n
    # the fake group_netlister produced exactly one netlist dir per group
    assert {t for t, _ in written} == {g["tag"] for g in grps}

    # -- the Donau transitions surfaced per group (pending->running->done x N) -----------
    assert seen == ["pending", "running", "done"] * n

    # -- psf_map is keyed BY MEASUREMENT TAG and covers ALL measurement tags --------------
    meas_tags = {pt["tag"] for pt in M.measurements(m)}
    assert set(res["psf_map"]) == meas_tags, (
        "psf_map must be keyed by measurement TAG (not corner) and cover every measurement")
    # every member of a group maps to THAT group's PSF dir
    for g in grps:
        gdir = str(pathlib.Path(res["psf_dir"]) / g["tag"])
        for pt in g["members"]:
            assert res["psf_map"][pt["tag"]] == gdir, (pt["tag"], g["tag"])

    # -- every group's dsub command is the VALIDATED alps tuple ---------------------------
    for cmd, g in zip(res["dsub_cmds"], grps):
        s = " ".join(str(x) for x in cmd)
        assert cmd[0] == "dsub"
        assert "-A ug_rfic.rfSClass" in s and "-q short" in s
        assert "/software/empyrean/alps/2026.03.hf1/bin/alps" in s
        assert "input.scs" in s
        assert "-format ps" in s and "psfxl" not in s     # classic PSF
        assert "-ade" in s                                 # ADE names + .simDone sentinel
        assert f"-I {pdk}/alps" in s                       # the alps PDK subtree only
        assert "-mt 8" in s and "-x all" in s
        # -EP points the node cwd at THIS group's netlist dir (one acm one-hot)
        assert "-EP " + str(pathlib.Path(tmp_path) / "gnetlist" / g["tag"]) in s
        # -o points at THIS group's psf subdir (under our workarea)
        assert "-o" in cmd
        odir = cmd[cmd.index("-o") + 1]
        assert odir == str(pathlib.Path(res["psf_dir"]) / g["tag"])

    # res["dsub_cmd"] (singular) is the first group's (back-compat)
    assert res["dsub_cmd"] == res["dsub_cmds"][0]

    # an injected per-group netlister => each group has its OWN one-hot netlist (real sweep)
    assert res["per_group_netlist"] is True

    # -- the manifest validates with the REAL roles --------------------------------------
    loaded = M.load(res["manifest"])
    assert list(loaded["v_out"]) == ["dig", "pll", "vco"]
    assert list(loaded["i_out"]) == ["i1p8u", "i500n", "i1p5u"]
    assert list(loaded["supplies"]) == ["avdd1p0"]

    # -- the no-compliance warning surfaced for the 3 current outputs --------------------
    assert res["warnings"], "missing-compliance warning must surface for the i_outs"
    wtext = " ".join(res["warnings"])
    for pin in gui["i_outs"]:
        assert pin in wtext

    # -- ALL artifacts live under the WORKAREA, NEVER the designer spine -----------------
    workarea = tmp_path / "ldo_modeling"
    for key in ("manifest", "psf_dir", "corner_dir"):
        p = pathlib.Path(res[key])
        assert str(p).startswith(str(workarea)), (key, p)
        assert "/simulation/" not in str(p), key
    for gtag, gdir in res["psf_map"].items():
        assert str(gdir).startswith(str(workarea)), (gtag, gdir)
    assert res["corner_dir"].endswith(f"PMU_TOP_TB__pmu_tb/{corner}")

    # -- the run was the last step we asked for; the sweep ran in order -------------------
    assert res["steps_run"] == ["resolve", "manifest", "netlist", "run"]


# =====================================================================================
# (B) REAL IMPORT: drive the ACTUAL importmp reader (NOT an injected npz)
# =====================================================================================
def test_real_import_fit_emit_standin(tmp_path):
    """STAND-IN 2-rail/2-sink topology (pll/vco, i1u/i500n). run.run_spectre_cli gives a REAL
    BY-TAG psf_map over cadence/work_pmu; step_import drives the ACTUAL importmp reader ->
    fit_multiport -> emit_pmu_va writes a .va. The real 3+3+1 PMU needs a box run."""
    if not RUN.WORK_CLI.is_dir():
        pytest.skip(f"no cadence/work_pmu fixture at {RUN.WORK_CLI} (needs a spectre run)")

    m = M.load(str(STANDIN_MANIFEST))
    # a REAL by-tag psf_map over the dev fixture (each tag -> its CLI PSF dir)
    cli = RUN.run_spectre_cli(m)
    psf_map = {t: str(p) for t, p in cli["psf_map"].items()}
    assert set(psf_map) == {pt["tag"] for pt in M.measurements(m)}, (
        "the CLI fixture must cover every measurement tag for an honest read")

    # drive step_import directly: the REAL importmp reader (NOT injected npz). The CLI gold
    # names sink probes Vb500/Vb1u; pass run._CLI_PROBE_ALIASES so the read resolves them.
    # Use a realistic corner label WITH an underscore (e.g. 'tt_25c', what the orchestrator
    # passes as load=corner) to lock the opaque-label round-trip through import->fit->emit.
    LBL = "tt_25c"
    npz_dir = tmp_path / "npz"
    npz_dir.mkdir()
    npz = PC.step_import(m, psf_map, npz_dir=npz_dir, load=LBL,
                         probe_aliases=cli["probe_aliases"])
    assert npz.exists()

    # the read returned the stand-in arrays (voltage + current ports) ---------------------
    ref = IMP.load_multiport(npz)
    keys = set(ref)
    assert f"z_pll_{LBL}" in keys and f"z_vco_{LBL}" in keys           # Zout of both rails
    assert f"couple_pll_vco_{LBL}" in keys                            # cross-coupling
    assert f"noise_pll_{LBL}" in keys and f"noise_vco_{LBL}" in keys  # output noise
    assert f"p_pll_1p0_{LBL}" in keys                                 # PSRR vs the 1p0 supply
    assert f"y_i1u_{LBL}" in keys and f"y_i500n_{LBL}" in keys        # sink admittance
    assert f"pi_i1u_1p0_{LBL}" in keys                                # current-PSRR
    assert list(ref["loads"]) == [LBL]                               # opaque corner label kept

    # fit the multi-port npz, then emit the ONE combined .va ------------------------------
    fit = FIT.fit_multiport(str(npz), m)
    assert set(fit["voltage"]) == {"pll", "vco"}
    assert {r["sink"] for r in fit["current"]} == {"i1u", "i500n"}

    va_path = tmp_path / "model" / "pmu_standin_model.va"
    va_path.parent.mkdir(parents=True)
    out = EMIT.emit_pmu_va(fit, "pmu_standin_model", va_path, supply="VDD1P0", ground="gnd")
    txt = pathlib.Path(out).read_text()
    # stand-in topology -> 2 V-rails + 2 I-biases between the supply and ground ports
    hdr = re.search(r"module\s+\w+\s*\(([^)]*)\)\s*;", txt)
    ports = [t.strip() for t in hdr.group(1).split(",")]
    assert ports[0] == "VDD1P0" and ports[-1] == "gnd"
    assert "pll" in ports and "vco" in ports
    assert "i1u" in ports and "i500n" in ports


def test_importmp_reads_reused_probe_src_p():
    """Read-side under REUSE: the y/pi derive reads <probe_src>:p where probe_src now names the
    REUSED vdc. measurements() builds the save/reads from manifest._probe_name, so a named
    probe_src points the save+read at the designer's real vsource (no derive-math change)."""
    import numpy as np
    # a resolved wur manifest names probe_src = the reused vdc (Vbias_*)
    raw = re.sub(r"<net:([^>]+)>", r"\1", WUR_MANIFEST.read_text())
    m = M.load(_write_json(json.loads(raw)))
    probe = M._probe_name(m, "i500n_lpf")
    assert probe == "Vbias_500n_lpf"                      # the reused vdc, not Vprobe_
    # the y point reads ('i', <probe>) and saves <probe>:p -> the read targets the reused source
    y_pt = next(pt for pt in M.measurements(m) if pt["tag"] == "y_i500n_lpf")
    assert y_pt["reads"] == [("i", probe)] and y_pt["save"] == [("i", probe)]
    # the derive reads <probe>:p from the PSF; Y = -I (no math change under reuse)
    d = {"freq": np.array([1.0, 2.0]), f"{probe}:p": np.array([1+2j, 3+4j])}
    arr = IMP._derive(y_pt, d)
    assert np.allclose(arr[:, 1], [-1.0, -3.0]) and np.allclose(arr[:, 2], [-2.0, -4.0])


# =====================================================================================
# (C) DRY-RUN: per-group dsub commands assembled, NOTHING executes (Boom never fires)
# =====================================================================================
def test_dry_run_assembles_per_group_dsub_no_exec(tmp_path):
    gui = real_pmu_gui()
    corner = gui["corner"]
    netmap = real_netmap(gui)
    m = real_manifest()
    netdir, ahdl, pdk = _seed_handoff(tmp_path)

    class Boom:
        def __call__(self, *a, **k):
            raise AssertionError("runner must NOT be called on a pure dry_run")

    written = []
    gnl = make_fake_group_netlister(tmp_path, written)
    res = PC.run_pmu_corner(
        gui, work_root=str(tmp_path), corner=corner, engine="alps",
        session=None, netmap=netmap,
        netlistdir=netdir, ahdllibdir=ahdl, pdk_model_dir=pdk,
        runner=Boom(), group_netlister=gnl, dry_run=True,
        steps=["resolve", "manifest", "augment", "netlist", "run"])

    n = len(RUN.groups(m))
    # one assembled dsub per group, returned WITHOUT executing anything
    assert res["dsub_cmds"] and len(res["dsub_cmds"]) == n
    for cmd in res["dsub_cmds"]:
        s = " ".join(str(x) for x in cmd)
        assert cmd[0] == "dsub"
        assert "/software/empyrean/alps/2026.03.hf1/bin/alps" in s and "-ade" in s
    # the manifest was written + validates (no execution required)
    assert pathlib.Path(res["manifest"]).exists()
    M.load(res["manifest"])
    # dry_run produced no PSF map (nothing ran) but the warnings still surfaced
    assert not res["psf_map"]
    assert res["warnings"]


def test_dry_run_plan_without_npz_stops_before_fit(tmp_path):
    """dry_run with NO npz_in and no real PSF: writes the manifest + plans + assembles the
    per-group dsub commands, but stops before fit/emit (nothing to read)."""
    gui = real_pmu_gui()
    netmap = real_netmap(gui)
    netdir, ahdl, pdk = _seed_handoff(tmp_path)
    res = PC.run_pmu_corner(
        gui, work_root=str(tmp_path), session=None, netmap=netmap,
        netlistdir=netdir, ahdllibdir=ahdl, pdk_model_dir=pdk, dry_run=True)
    assert pathlib.Path(res["manifest"]).exists()
    assert res["dsub_cmds"] and res["dsub_cmds"][0][0] == "dsub"
    assert res["npz"] is None and res["va"] is None        # stopped before fit/emit
    assert res["fit_report"] is None
    assert res["warnings"]                                  # the i_out warning still surfaced
    # offline default reused ONE handoff netlistdir for every group -> shape-only, flagged
    assert res["per_group_netlist"] is False


# =====================================================================================
# (D) the resolve seam: no session + no netmap -> an actionable error
# =====================================================================================
def test_resolve_without_session_or_netmap_raises_actionable(tmp_path, monkeypatch):
    gui = real_pmu_gui()
    # force the offline path FAST + deterministically: make a skillbridge connect raise
    # immediately (the resolver collapses any open failure to ResolveUnavailable).
    try:
        import skillbridge
        monkeypatch.setattr(skillbridge.Workspace, "open",
                            staticmethod(lambda *a, **k: (_ for _ in ()).throw(
                                ConnectionRefusedError("no CIW server (test)"))))
    except Exception:                                   # noqa: BLE001 (absent -> already offline)
        pass
    with pytest.raises(PC.PmuCornerError) as ei:
        PC.run_pmu_corner(gui, work_root=str(tmp_path), session=None, netmap=None,
                          dry_run=True)
    msg = str(ei.value)
    assert "netmap=" in msg and ("box" in msg or "skillbridge" in msg)


# =====================================================================================
# (E) storage: <Lib>__<Cell>/<corner> tree, env WORK_ROOT fallback, no designer spine
# =====================================================================================
def test_corner_dir_layout_and_work_root(tmp_path, monkeypatch):
    gui = real_pmu_gui()
    base, dirs = PC.corner_dir(str(tmp_path), gui, "ss_125c")
    assert base.name == "ss_125c"
    assert base.parent.name == "PMU_TOP_TB__pmu_tb"
    assert base.parent.parent.name == "ldo_modeling"
    for sub in ("netlist", "psf", "npz", "model"):
        assert dirs[sub].is_dir()
    assert "/simulation/" not in str(base)             # never the designer spine

    monkeypatch.setenv("WORK_ROOT", str(tmp_path / "envroot"))
    assert PC.resolve_work_root() == tmp_path / "envroot"


# =====================================================================================
# (F) OFFLINE SWEEP: run_pmu_corner(manifest=) + the REAL offline group netlister
#     (cluster.netlist_augment) -- NO Virtuoso, NO dsub. The manifest-driven path reuses
#     all 9 steps (no second orchestrator) and rewrites ONE base input.scs into the 8
#     per-group one-hot ac/noise netlists.
# =====================================================================================
def _resolved_wur_manifest():
    """A RESOLVED copy of the shipped wur_pmu_top manifest ('<net:X>' -> 'X'), loaded +
    validated -- the offline-sweep input (the real red-zone roles: 1 supply + 2 v_out + 3
    i_out -> 14 measurement points -> 8 groups)."""
    raw = re.sub(r"<net:([^>]+)>", r"\1", WUR_MANIFEST.read_text())
    return M.load(_write_json(json.loads(raw)))


_OFFLINE_TMP = []


def _write_json(obj):
    import tempfile
    f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(obj, f)
    f.close()
    _OFFLINE_TMP.append(f.name)
    return f.name


def _wur_base_dir(tmp_path):
    """A synthetic base .tran TB matching the resolved wur nets + the designer's OWN source on
    EVERY tagged pin (the source-reuse model): the supply vsource, a load isource per v_out, a
    compliance vdc vsource per i_out (the named *_src instances the manifest reuses), and a
    .tran to be stripped."""
    d = tmp_path / "base"
    d.mkdir()
    (d / "input.scs").write_text(
        "simulator lang=spectre\n"
        'include "models.scs"\n'
        "Xdut (AVDD1P0 VDD0P8_PLL VDD0P8_VCO IBP_POLY_500N_LPF IBP_POLY_3P6U_VCO "
        "IBP_PTAT_TUNE_1P5U_VCO VSS) WuR_PMU_TOP\n"
        "V_AVDD (AVDD1P0 0) vsource dc=0.98\n"
        "Iload_pll (VDD0P8_PLL 0) isource dc=500u\n"
        "Iload_vco (VDD0P8_VCO 0) isource dc=2m\n"
        "Vbias_500n_lpf (IBP_POLY_500N_LPF 0) vsource dc=1.28\n"
        "Vbias_3p6u_vco (IBP_POLY_3P6U_VCO 0) vsource dc=1.28\n"
        "Vbias_1p5u_ptat (IBP_PTAT_TUNE_1P5U_VCO 0) vsource dc=0.667\n"
        "tt tran stop=1u\n")
    return d


def test_offline_sweep_dry_run_eight_groups(tmp_path):
    """run_pmu_corner(manifest=) + the REAL offline netlister, dry_run: exactly 8 per-group
    dsub commands assembled, NOTHING executed; the per-group one-hot netlists were written
    under the workarea; resolve/manifest steps SKIPPED (manifest-driven)."""
    m = _resolved_wur_manifest()
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    grps = RUN.groups(m)
    assert len(grps) == 8

    # the per-group netlists land under the workarea corner dir (NOT the designer spine)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)
    gnl = NA.make_offline_group_netlister(base, m, dirs["netlist"])

    class Boom:
        def __call__(self, *a, **k):
            raise AssertionError("runner must NOT be called on a dry_run sweep")

    res = PC.run_pmu_corner(
        manifest=m, work_root=str(tmp_path), corner=corner, engine="alps",
        netlistdir=str(base), ahdllibdir=None, pdk_model_dir=str(tmp_path / "pdk"),
        group_netlister=gnl, runner=Boom(), dry_run=True, sleep=lambda *_: None)

    # exactly one assembled dsub per measurement GROUP, returned without executing -----------
    assert res["dsub_cmds"] and len(res["dsub_cmds"]) == 8
    for cmd, g in zip(res["dsub_cmds"], grps):
        s = " ".join(str(x) for x in cmd)
        assert cmd[0] == "dsub"
        assert "/software/empyrean/alps/2026.03.hf1/bin/alps" in s and "-ade" in s
        # -EP points at THIS group's offline-written netlist dir (a real per-group one-hot)
        assert "-EP " + str(dirs["netlist"] / g["tag"]) in s
        assert (dirs["netlist"] / g["tag"] / "input.scs").is_file()
    # injected real factory -> each group has its OWN one-hot netlist (a real sweep, not shape)
    assert res["per_group_netlist"] is True
    # the manifest-driven path SKIPS resolve+manifest; the run still happened (dry) ----------
    assert "resolve" not in res["steps_run"] and "manifest" not in res["steps_run"]
    assert "run" in res["steps_run"]
    # a workarea copy of the manifest was persisted for traceability
    assert res["manifest"] and pathlib.Path(res["manifest"]).exists()
    assert not res["psf_map"]                            # nothing ran -> no PSF map


def test_offline_sweep_real_runner_psf_map_by_all_14_tags(tmp_path):
    """Non-dry offline sweep with a FAKE runner that pre-seeds each group's stub PSF + .simDone:
    psf_map is keyed by ALL 14 measurement tags (each tag -> its group's PSF dir)."""
    m = _resolved_wur_manifest()
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    grps = RUN.groups(m)

    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)
    gnl = NA.make_offline_group_netlister(base, m, dirs["netlist"])

    # pre-fill each group's psf subdir so run_corner's _verify_psf passes with the fake runner
    for g in grps:
        gd = dirs["psf"] / g["tag"]
        gd.mkdir(parents=True, exist_ok=True)
        ext = "noise.noise" if g["analysis"] == "noise" else "ac.ac"
        (gd / ext).write_bytes(b"PSFversion")
        (gd / ".simDone").write_bytes(b"")

    runner = FakeRunner()
    seen = []
    res = PC.run_pmu_corner(
        manifest=m, work_root=str(tmp_path), corner=corner, engine="alps",
        netlistdir=str(base), ahdllibdir=None, pdk_model_dir=str(tmp_path / "pdk"),
        group_netlister=gnl, runner=runner, on_status=lambda st, raw: seen.append(st),
        sleep=lambda *_: None,
        # run-only: the read/fit/emit side needs a real PSF (covered elsewhere); the npz fixture
        # for wur's 2+3 topology needs a box run, so we stop after the SWEEP.
        steps=["augment", "netlist", "run"])

    # one dsub per group; the Donau transitions surfaced per group --------------------------
    assert len(runner.cmds("dsub")) == 8
    assert seen == ["pending", "running", "done"] * 8

    # psf_map keyed BY MEASUREMENT TAG, covering ALL 14 measurement points -------------------
    meas_tags = {pt["tag"] for pt in M.measurements(m)}
    assert len(meas_tags) == 14
    assert set(res["psf_map"]) == meas_tags
    # every member of a group maps to THAT group's PSF dir
    for g in grps:
        gdir = str(pathlib.Path(res["psf_dir"]) / g["tag"])
        for pt in g["members"]:
            assert res["psf_map"][pt["tag"]] == gdir, (pt["tag"], g["tag"])

    # every artifact under the workarea, never the designer spine ----------------------------
    workarea = tmp_path / "ldo_modeling"
    for gtag, gdir in res["psf_map"].items():
        assert str(gdir).startswith(str(workarea)) and "/simulation/" not in str(gdir)


def test_offline_sweep_bplus_resolves_net_equals_pin(tmp_path):
    """The shipped wur manifest carries '<net:PIN>' placeholders whose PIN IS a base net
    (net==pin); B+ net resolution resolves them silently against the base netlist (no raise),
    so net==pin needs zero hand-edits before the offline sweep."""
    m = M.load(str(WUR_MANIFEST))                       # placeholders intact
    base = _wur_base_dir(tmp_path)
    NA.make_offline_group_netlister(base, m, tmp_path / "out")   # no raise -- B+ resolves
    assert m["supplies"]["avdd1p0"]["net"] == "AVDD1P0"          # resolved in place
    assert m["v_out"]["pll"]["net"] == "VDD0P8_PLL"


def test_offline_sweep_bplus_hard_stops_on_net_not_equal_pin(tmp_path):
    """A placeholder PIN that is NOT a net in the base netlist (net!=pin) still hard-stops loudly
    so the designer sets the real TB net."""
    m = M.load(str(WUR_MANIFEST))
    m["i_out"]["i500n_lpf"]["net"] = "<net:NOT_A_NET>"
    base = _wur_base_dir(tmp_path)
    with pytest.raises(NA.NetlistAugmentError) as ei:
        NA.make_offline_group_netlister(base, m, tmp_path / "out")
    assert "NOT_A_NET" in str(ei.value)


def _seed_all_group_psf(dirs, grps):
    for g in grps:
        gd = dirs["psf"] / g["tag"]
        gd.mkdir(parents=True, exist_ok=True)
        ext = "noise.noise" if g["analysis"] == "noise" else "ac.ac"
        (gd / ext).write_bytes(b"PSFversion")
        (gd / ".simDone").write_bytes(b"")


def test_step_run_group_status_streams_states(tmp_path):
    """step_run's group_status callback streams each group's lifecycle (pending -> the Donau
    states -> done) -- the structured feed the GUI per-group status table renders. Every group
    is reported, each stream begins 'pending', walks 'running', and ends 'done'."""
    m = _resolved_wur_manifest()
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    grps = RUN.groups(m)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)
    gnl = NA.make_offline_group_netlister(base, m, dirs["netlist"])
    _seed_all_group_psf(dirs, grps)
    netinfo = PC.step_netlist(dirs, netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"))

    events = []
    runres = PC.step_run(netinfo, dirs["psf"], m, engine="alps", runner=FakeRunner(),
                         group_netlister=gnl, sleep=lambda *_: None,
                         group_status=lambda i, n, g, st: events.append((i, g["tag"], st)))

    assert {t for _i, t, _s in events} == {g["tag"] for g in grps}      # every group reported
    by_group = {}
    for _i, t, s in events:
        by_group.setdefault(t, []).append(s)
    for t, states in by_group.items():
        assert states[0] == "pending" and states[-1] == "done", (t, states)
        assert "running" in states                    # the Donau RUNNING transition surfaced
    assert runres["ran"] is True


def test_step_run_cancel_between_groups(tmp_path):
    """A cancel() that turns True after the first group stops the sweep with CancelledError
    BETWEEN groups (a submitted group is never interrupted mid-poll)."""
    m = _resolved_wur_manifest()
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    grps = RUN.groups(m)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)
    gnl = NA.make_offline_group_netlister(base, m, dirs["netlist"])
    _seed_all_group_psf(dirs, grps)
    netinfo = PC.step_netlist(dirs, netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"))

    runner = FakeRunner()
    with pytest.raises(RUN.CancelledError):
        PC.step_run(netinfo, dirs["psf"], m, engine="alps", runner=runner,
                    group_netlister=gnl, sleep=lambda *_: None,
                    cancel=lambda: len(runner.cmds("dsub")) >= 1)   # cancel after group 1 submits
    assert len(runner.cmds("dsub")) == 1               # exactly one group submitted before cancel
    # the 2nd group's netlist dir was never produced (the sweep stopped before it began)
    assert not (dirs["netlist"] / grps[1]["tag"]).exists()


# =====================================================================================
# (G) BOUNDED-PARALLEL scheduler: max_parallel > 1 overlaps group jobs (Donau runs them in
#     parallel; the sweep just stops blocking on one before submitting the next). A serial
#     max_parallel=1 is already locked by (A)/(F); these lock the parallel + cancel-drain paths.
# =====================================================================================
class ConcurrentFakeRunner:
    """A fake runner that gives each dsub a UNIQUE job_id and holds every job RUNNING for
    >=2 djob polls before DONE, so several jobs are genuinely in flight at once under a >1 cap.
    Tracks the PEAK number of simultaneously in-flight job_ids (submitted, not yet returned
    DONE) so the test can assert the scheduler honoured min(cap, n_groups)."""

    def __init__(self, running_polls=2):
        self.calls = []
        self.running_polls = running_polls          # djob polls spent RUNNING before DONE
        self._next = 0
        self._polls = {}                            # job_id -> how many djob queries seen
        self._live = set()                          # job_ids submitted but not yet DONE
        self.peak = 0

    def __call__(self, argv, timeout=None, check=False):
        self.calls.append(list(argv))
        head = argv[0]
        if head == "dsub":
            self._next += 1
            jid = str(40000000 + self._next)
            self._polls[jid] = 0
            self._live.add(jid)
            self.peak = max(self.peak, len(self._live))   # concurrency at submit time
            return RunResult(0, f"Submit job successfully. JOBID {jid}\n", "", list(argv))
        if head == "djob":
            jid = str(argv[1])
            k = self._polls.get(jid, 0)
            self._polls[jid] = k + 1
            if k == 0:
                state = "PENDING"
            elif k <= self.running_polls:
                state = "RUNNING"
            else:
                state = "DONE"
                self._live.discard(jid)
            line = {"PENDING": f"JobId: {jid}  State: PENDING  Queue: short\n",
                    "RUNNING": f"JobId: {jid}  State: RUNNING  Node: sinct20-hs\n",
                    "DONE":    f"JobId: {jid}  State: DONE  Exit: 0\n"}[state]
            return RunResult(0, line, "", list(argv))
        raise AssertionError(f"ConcurrentFakeRunner: no scripted reply for {head!r} ({argv})")

    def cmds(self, head):
        return [c for c in self.calls if c and c[0] == head]


def test_step_run_parallel_overlaps(tmp_path):
    """max_parallel=3 over the wur groups (>=4): the scheduler keeps min(3, n) jobs in flight,
    every member tag lands in psf_map, and exactly one dsub per group."""
    m = _resolved_wur_manifest()
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    grps = RUN.groups(m)
    assert len(grps) >= 4
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)
    gnl = NA.make_offline_group_netlister(base, m, dirs["netlist"])
    _seed_all_group_psf(dirs, grps)
    netinfo = PC.step_netlist(dirs, netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"))

    runner = ConcurrentFakeRunner(running_polls=2)
    res = PC.step_run(netinfo, dirs["psf"], m, engine="alps", runner=runner,
                      group_netlister=gnl, sleep=lambda *_: None, max_parallel=3)

    assert runner.peak == min(3, len(grps)), (runner.peak, len(grps))   # honoured the cap
    assert len(runner.cmds("dsub")) == len(grps)                        # one job per group
    assert len(res["dsub_cmds"]) == len(grps)
    meas_tags = {pt["tag"] for pt in M.measurements(m)}
    assert set(res["psf_map"]) == meas_tags                             # all members mapped
    for g in grps:
        gdir = str(dirs["psf"] / g["tag"])
        for pt in g["members"]:
            assert res["psf_map"][pt["tag"]] == gdir
    assert res["ran"] is True


def test_step_run_parallel_cancel_drains(tmp_path):
    """With max_parallel>=2, a cancel() that flips True after a couple of submits stops launching
    NEW groups, DRAINS the in-flight ones (their PSF still maps), and raises CancelledError; no
    group beyond those already launched gets netlisted."""
    m = _resolved_wur_manifest()
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    grps = RUN.groups(m)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)
    gnl = NA.make_offline_group_netlister(base, m, dirs["netlist"])
    _seed_all_group_psf(dirs, grps)
    netinfo = PC.step_netlist(dirs, netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"))

    runner = ConcurrentFakeRunner(running_polls=2)
    psf_map = {}
    with pytest.raises(RUN.CancelledError):
        PC.step_run(netinfo, dirs["psf"], m, engine="alps", runner=runner,
                    group_netlister=gnl, sleep=lambda *_: None, max_parallel=2,
                    cancel=lambda: len(runner.cmds("dsub")) >= 2)   # cancel once 2 are launched

    n_sub = len(runner.cmds("dsub"))
    assert n_sub == 2, n_sub                            # exactly the 2 launched before cancel
    # the launched groups are the first two in FIFO order; only those got netlisted
    launched = {g["tag"] for g in grps[:n_sub]}
    for g in grps[:n_sub]:
        assert (dirs["netlist"] / g["tag"]).exists(), g["tag"]
    for g in grps[n_sub:]:
        assert not (dirs["netlist"] / g["tag"]).exists(), g["tag"]
    # every djob job reached DONE -> all live jobs drained before the raise
    assert runner._live == set()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
