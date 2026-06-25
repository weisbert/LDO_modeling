"""STAGE 1b part 2 -- the LOAD x TEMPERATURE coverage SWEEP driver (pmu_corner.run_pmu_coverage_sweep).

NO sim / Virtuoso / dsub. The sweep REUSES run_pmu_corner's step functions per CELL (a cell =
one groups-subset at one op_loads+temp). Routing (HANDOFF_MODELING_COVERAGE §1/§3/§6): the
load-swept small-signal groups (1x AC + .noise) REPEAT across the load axis; the dc/tran/2x-lin-
gate groups run ONCE at the TB OP; temperature is the outer axis. Each cell gets its OWN netlist
+ PSF dir under the workarea corner dir; all cells assemble into ONE sweep npz.

The tests reuse test_pmu_corner's patterns (FakeRunner, the resolved-wur manifest, the synthetic
base TB). A STUB netlister_factory records (cell_label, op_loads, temp, group_tag); a REAL
psfascii seeder lets the actual importmp reader run so the assembled npz carries real arrays.

Run:  python -m pytest cadence/insitu/test_coverage_sweep.py -q
"""
import json
import pathlib
import re
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))            # .../cadence on sys.path (bare-import convention)

from insitu import pmu_corner as PC                                        # noqa: E402
from insitu import manifest as M                                           # noqa: E402
from insitu import run as RUN                                              # noqa: E402
from insitu import importmp as IMP                                         # noqa: E402
from cluster import netlist_augment as NA                                  # noqa: E402

# reuse the EXACT fixtures the single-corner tests pin (FakeRunner, the base TB, the wur loader)
from insitu.test_pmu_corner import (                                       # noqa: E402
    FakeRunner, _wur_base_dir, _resolved_wur_manifest, WUR_MANIFEST, _write_json)

WUR = WUR_MANIFEST


# ----------------------------------------------------------------- coverage manifests
def _wur_cov_manifest(loads=True, temps=True, kinds=True):
    """A resolved wur manifest with coverage knobs turned on: per-rail load points (PLL + VCO,
    4 pts each), temps [-40,55], and -- when kinds -- iv/dropout/transient/lin_gate so the
    'once' (dc/tran/2x) groups exist. Built off the shipped wur manifest dict (real roles)."""
    raw = re.sub(r"<net:([^>]+)>", r"\1", WUR.read_text())
    md = json.loads(raw)
    cov = md.setdefault("coverage", {})
    if loads:
        cov["loads"] = {"pll": {"points": [1e-4, 5e-4, 1e-3, 3e-3]},
                        "vco": {"points": [2e-4, 1e-3, 2e-3, 4e-3]}}
    if temps:
        cov["temps"] = [-40, 55]
    if kinds:
        cov["dropout"] = {"pll": {"sweep": {"type": "lin", "start": 1e-4, "stop": 3e-3, "n": 6}},
                          "vco": {"sweep": {"type": "lin", "start": 2e-4, "stop": 4e-3, "n": 6}}}
        cov["iv"] = {"i500n_lpf": {"sweep": {"type": "lin", "start": 0.0, "stop": 1.28, "n": 6}}}
        cov["transient"] = {"pll": {"steps": [{"from": 1e-4, "to": 3e-3, "label": "s1"}],
                                    "edge": 1e-9, "tstop": 1e-6}}
        cov["lin_gate"] = True
    return M.load(_write_json(md))


# ----------------------------------------------------------------- a recording factory
class RecordingFactory:
    """A FAKE netlister_factory: factory(op_loads, temp, out_base) -> a per-group netlister that
    writes a stub input.scs per group + records (cell_out_base, op_loads, temp, group_tag). The
    cell_out_base is dirs['netlist']/<cell_label>, so the recorded op_loads is per CELL."""

    def __init__(self):
        self.cells = []            # (out_base, op_loads, temp) -- one per factory() call
        self.groups = []           # (out_base, group_tag) -- one per netlister(group) call

    def __call__(self, op_loads, temp, out_base):
        self.cells.append((str(out_base), op_loads, temp))
        ob = pathlib.Path(out_base)

        def _netlister(group):
            d = ob / group["tag"]
            d.mkdir(parents=True, exist_ok=True)
            (d / "input.scs").write_text(
                f"// cell {ob.name} op_loads={op_loads} temp={temp} group {group['tag']}\n")
            self.groups.append((str(out_base), group["tag"]))
            return str(d)

        return _netlister

    def op_loads_for(self, cell_out_base):
        for ob, op, _t in self.cells:
            if ob == str(cell_out_base):
                return op
        return None


def _seed_cell_psf(dirs, cell_label, groups_subset):
    """Pre-fill EACH group's PSF subdir for one cell under <corner>/psf/<cell_label>/<g.tag>/ so
    run_corner's _verify_psf passes with the fake runner (non-empty dir + .simDone) AND the
    driver's per-cell importmp read parses it (the sweep ALWAYS reads a real run's PSF). We seed
    minimal-but-REAL psfascii (HEADER..VALUE..END) -- the content does not matter for the run-count
    tests, only that the read does not error. box-pending: the dc/tran PSF extension the cluster
    writes is confirmed on the box; _verify_psf only needs a non-empty dir + .simDone."""
    for g in groups_subset:
        gd = dirs["psf"] / cell_label / g["tag"]
        gd.mkdir(parents=True, exist_ok=True)
        (gd / ".simDone").write_bytes(b"")
        a = g["analysis"]
        if a == "noise":
            _psf_ac(gd / "nz.noise", "freq", {"out": [1e-18, 1e-18, 1e-18]})
        elif a in ("dc", "tran"):
            # a DC/tran sweep PSF: one swept-axis column + each member save (real-valued). The
            # axis name 'dc'/'time' matches _sweep_axis / _time_axis (box-pending exact key).
            sweep = "time" if a == "tran" else "dc"
            cols = {}
            for pt in g["members"]:
                for kind, ref in pt["save"]:
                    cols[ref if kind == "v" else f"{ref}:p"] = [0.8, 0.8, 0.8]
            _psf_real(gd / ("trz.tran" if a == "tran" else "dcz.dc"), sweep,
                      [1e-4, 5e-4, 1e-3], cols)
        else:                                          # ac (1x or 2x lin-gate)
            cols = {}
            for pt in g["members"]:
                for kind, ref in pt["save"]:
                    cols[ref if kind == "v" else f"{ref}:p"] = [(1.0, 0.0)] * 3
            _psf_ac(gd / "acz.ac", "freq", cols)


def _psf_real(path, sweep_name, axis_vals, sig_cols):
    """Write a tiny REAL psfascii dc/tran file: a real-valued swept axis + real-valued signals."""
    lines = ["HEADER", '"PSFversion" "1.00"', "VALUE"]
    for i, xv in enumerate(axis_vals):
        lines.append(f'"{sweep_name}" {xv:g}')
        for name, vals in sig_cols.items():
            lines.append(f'"{name}" {vals[i]:g}')
    lines.append("END")
    path.write_text("\n".join(lines) + "\n")


# ----------------------------------------------------------------- real psfascii seeding
def _psf_ac(path, sweep_name, sig_cols):
    """Write a tiny REAL psfascii .ac/.noise file (starts with HEADER so psf.read_psf parses it
    as ascii). sig_cols = {name: list-of-(re,im) or scalars}; one row per freq point."""
    freqs = [10.0, 100.0, 1000.0]
    lines = ["HEADER", '"PSFversion" "1.00"', "VALUE"]
    for i, fr in enumerate(freqs):
        lines.append(f'"{sweep_name}" {fr:g}')
        for name, vals in sig_cols.items():
            v = vals[i]
            if isinstance(v, tuple):
                lines.append(f'"{name}" ( {v[0]:g} {v[1]:g} )')
            else:
                lines.append(f'"{name}" {v:g}')
    lines.append("END")
    path.write_text("\n".join(lines) + "\n")


def _seed_real_cell_psf(m, dirs, cell_label, groups_subset, zout_re=12.0):
    """Seed REAL psfascii PSF for one cell's groups so the ACTUAL importmp reader produces arrays.
    Handles every analysis: ac (V@zout_re / I@1A), noise ('out'), dc (iv -> probe vs swept Vsink;
    dropout -> Vout vs swept Iload with slope -zout_re so GUARDRAIL-3 is CONSISTENT by default),
    tran (Vout vs time)."""
    for g in groups_subset:
        gd = dirs["psf"] / cell_label / g["tag"]
        gd.mkdir(parents=True, exist_ok=True)
        (gd / ".simDone").write_bytes(b"")
        a = g["analysis"]
        if a == "noise":
            _psf_ac(gd / "nz.noise", "freq", {"out": [1e-18, 1e-18, 1e-18]})
        elif a == "dc":
            # the swept DC axis is named "dc"; one column per member save. A dropout dc_<o> group
            # saves the v_out node -> Vout vs Iload; an iv_<c> group saves <probe>:p -> I vs Vsink.
            x = [1e-4, 5e-4, 1e-3, 3e-3]
            cols = {}
            for pt in g["members"]:
                for kind, ref in pt["save"]:
                    if kind == "v":                       # dropout: Vout droops at slope -zout_re
                        cols[ref] = [0.8 - zout_re * xi for xi in x]
                    else:                                 # iv: probe current rises with Vsink
                        cols[f"{ref}:p"] = [2e-7 * xi / x[-1] for xi in x]
            _psf_real(gd / "dcz.dc", "dc", x, cols)
        elif a == "tran":
            t = [0.0, 1e-9, 2e-9, 3e-9]
            cols = {}
            for pt in g["members"]:
                for kind, ref in pt["save"]:
                    cols[ref if kind == "v" else f"{ref}:p"] = [0.8, 0.799, 0.7985, 0.8]
            _psf_real(gd / "trz.tran", "time", t, cols)
        else:                                             # ac (1x Zout/PSRR/couple/y/pi, or 2x z2)
            cols = {}
            for pt in g["members"]:
                for kind, ref in pt["save"]:
                    if kind == "v":
                        # Zout group: V at the read net == zout_re (1 A injected -> V = Z); flat
                        cols[ref] = [(zout_re, 0.0), (zout_re, 0.0), (zout_re, 0.0)]
                    else:                                 # ('i', probe) -> <probe>:p
                        cols[f"{ref}:p"] = [(1.0, 0.0), (1.0, 0.0), (1.0, 0.0)]
            _psf_ac(gd / "acz.ac", "freq", cols)


# =====================================================================================
# (1) cell count + job count: 2 temps, 4-pt load sweep on BOTH rails, kinds declared
# =====================================================================================
def test_sweep_cell_and_job_count(tmp_path):
    m = _wur_cov_manifest(loads=True, temps=True, kinds=True)
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)

    grps = RUN.groups(m)
    swept = [g for g in grps if RUN.is_load_swept_group(g)]
    once = [g for g in grps if not RUN.is_load_swept_group(g)]
    assert len(swept) == 8 and len(once) == 6        # 8 AC/noise per load; 6 dc/tran/2x at OP

    factory = RecordingFactory()
    # seed every cell's stub PSF so the fake runner's _verify_psf passes
    load_axis = RUN.load_axis(m)
    for temp in [-40, 55]:
        t = f"T{temp:g}"
        _seed_cell_psf(dirs, f"Lnom_{t}", once)
        for (ll, _op) in load_axis:
            _seed_cell_psf(dirs, f"{ll}_{t}", swept)

    runner = FakeRunner()
    res = PC.run_pmu_coverage_sweep(
        manifest=m, work_root=str(tmp_path), corner=corner, engine="alps",
        base_netlist=str(base), netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"),
        netlister_factory=factory, runner=runner, sleep=lambda *_: None,
        steps=["netlist", "run"])           # run-only: read covered in test 2

    # cells: per temp = 1 once-cell + 4 load-cells = 5; over 2 temps = 10 cells
    assert len(res["loads"]) == 10
    assert set(res["loads"]) == {
        "Lnom_T-40", "L0_T-40", "L1_T-40", "L2_T-40", "L3_T-40",
        "Lnom_T55", "L0_T55", "L1_T55", "L2_T55", "L3_T55"}
    # jobs = sum over cells of that cell's group count = 2 * (6 + 4*8) = 76
    jobs = 2 * (len(once) + len(load_axis) * len(swept))
    assert jobs == 76
    assert len(res["dsub_cmds"]) == 76
    assert len(runner.cmds("dsub")) == 76               # the dsub count matches
    assert res["load_swept"] is True and res["temps"] == [-40, 55]
    assert res["dsub_cmd"] == res["dsub_cmds"][0]


# =====================================================================================
# (2) assembled npz: per-load Zout for each cell label; once-groups appear ONCE; loads array
# =====================================================================================
def test_sweep_assembles_per_load_zout_and_once_arrays(tmp_path):
    m = _wur_cov_manifest(loads=True, temps=False, kinds=True)   # single temp -> shorter labels
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)

    grps = RUN.groups(m)
    swept = [g for g in grps if RUN.is_load_swept_group(g)]
    once = [g for g in grps if not RUN.is_load_swept_group(g)]
    load_axis = RUN.load_axis(m)

    # seed REAL psfascii so the ACTUAL importmp reader produces arrays. Per load cell give a
    # DISTINCT Zout magnitude so the per-load arrays are distinguishable.
    _seed_real_cell_psf(m, dirs, "Lnom", once, zout_re=10.0)     # the OP once-cell (dc/iv/tr/z2)
    for k, (ll, _op) in enumerate(load_axis):
        _seed_real_cell_psf(m, dirs, ll, swept, zout_re=10.0 + k)

    res = PC.run_pmu_coverage_sweep(
        manifest=m, work_root=str(tmp_path), corner=corner, engine="alps",
        base_netlist=str(base), netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"),
        netlister_factory=RecordingFactory(), runner=FakeRunner(), sleep=lambda *_: None,
        steps=["netlist", "run", "import"])    # stop before fit (the wur 2+3 fit needs a box npz)

    assert res["npz"] and pathlib.Path(res["npz"]).exists()
    ref = IMP.load_multiport(res["npz"])

    # the loads array lists EVERY cell label (the once-cell + the 4 load cells)
    labels = [str(x) for x in ref["loads"]]
    assert labels == ["Lnom", "L0", "L1", "L2", "L3"]

    # per-load Zout: z_<o>_<label> present for EACH load cell + rail (the swept small-signal)
    for o in ("pll", "vco"):
        for ll in ("L0", "L1", "L2", "L3"):
            assert f"z_{o}_{ll}" in ref, (o, ll)
        # the Zout is NOT present at the once-cell label (swept groups did not run there)
        assert f"z_{o}_Lnom" not in ref

    # the once-groups' arrays appear ONCE, at the OP cell's label (Lnom): dropout dc_, iv_, tr_, z2_
    assert "dc_pll_Lnom" in ref and "dc_vco_Lnom" in ref       # dropout (the 'once' dc groups)
    assert "iv_i500n_lpf_Lnom" in ref                          # I-V
    assert any(k.startswith("tr_pll_") and k.endswith("_Lnom") for k in ref)   # transient slew
    assert "z2_pll_Lnom" in ref and "z2_vco_Lnom" in ref       # 2x lin-gate
    # and they do NOT also appear under a load label (run once, not per load)
    assert not any(k.startswith("dc_pll_L0") for k in ref)


# =====================================================================================
# (3) per-cell netlist + psf dirs land under the WORKAREA corner dir, never /simulation/
# =====================================================================================
def test_sweep_dirs_under_workarea(tmp_path):
    m = _wur_cov_manifest(loads=True, temps=True, kinds=True)
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)

    grps = RUN.groups(m)
    swept = [g for g in grps if RUN.is_load_swept_group(g)]
    once = [g for g in grps if not RUN.is_load_swept_group(g)]
    for temp in [-40, 55]:
        t = f"T{temp:g}"
        _seed_cell_psf(dirs, f"Lnom_{t}", once)
        for (ll, _op) in RUN.load_axis(m):
            _seed_cell_psf(dirs, f"{ll}_{t}", swept)

    factory = RecordingFactory()
    res = PC.run_pmu_coverage_sweep(
        manifest=m, work_root=str(tmp_path), corner=corner, engine="alps",
        base_netlist=str(base), netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"),
        netlister_factory=factory, runner=FakeRunner(), sleep=lambda *_: None,
        steps=["netlist", "run"])

    workarea = tmp_path / "ldo_modeling"
    # every per-cell netlist out_base the factory was handed lives under the corner netlist dir
    for ob, _op, _t in factory.cells:
        assert str(ob).startswith(str(dirs["netlist"])), ob
        assert "/simulation/" not in str(ob)
    # the -EP of every dsub points at a per-cell-per-group netlist dir under the workarea
    for cmd in res["dsub_cmds"]:
        s = " ".join(str(x) for x in cmd)
        ep = s.split("-EP ", 1)[1].split()[0]
        assert ep.startswith(str(dirs["netlist"])) and "/simulation/" not in ep
        # the -o (psf out) is under the corner psf dir
        odir = cmd[cmd.index("-o") + 1]
        assert str(odir).startswith(str(dirs["psf"])) and "/simulation/" not in str(odir)
    assert str(res["corner_dir"]).startswith(str(workarea))


# =====================================================================================
# (4) op_loads routing: at load cell L_k the factory received each rail's k-th load_points;
#     the once-cell received op_loads=None.
# =====================================================================================
def test_sweep_op_loads_routing(tmp_path):
    m = _wur_cov_manifest(loads=True, temps=False, kinds=True)
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)

    grps = RUN.groups(m)
    swept = [g for g in grps if RUN.is_load_swept_group(g)]
    once = [g for g in grps if not RUN.is_load_swept_group(g)]
    _seed_cell_psf(dirs, "Lnom", once)
    for (ll, _op) in RUN.load_axis(m):
        _seed_cell_psf(dirs, ll, swept)

    factory = RecordingFactory()
    PC.run_pmu_coverage_sweep(
        manifest=m, work_root=str(tmp_path), corner=corner, engine="alps",
        base_netlist=str(base), netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"),
        netlister_factory=factory, runner=FakeRunner(), sleep=lambda *_: None,
        steps=["netlist", "run"])

    pll_pts = M.load_points(m, "pll")             # [1e-4, 5e-4, 1e-3, 3e-3]
    vco_pts = M.load_points(m, "vco")             # [2e-4, 1e-3, 2e-3, 4e-3]
    # the once-cell (Lnom) received op_loads=None (it runs at the TB OP)
    assert factory.op_loads_for(dirs["netlist"] / "Lnom") is None
    # each load cell L_k received op_loads mapping each rail to its k-th load point
    for k in range(4):
        op = factory.op_loads_for(dirs["netlist"] / f"L{k}")
        assert op == {"pll": pll_pts[k], "vco": vco_pts[k]}, (k, op)


# =====================================================================================
# (5) GUARDRAIL-3 wiring: a fabricated dropout slope disagreeing with Zout(0) -> a warning
# =====================================================================================
def test_sweep_guardrail3_surfaces_warning(tmp_path):
    m = _wur_cov_manifest(loads=True, temps=False, kinds=True)
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)

    grps = RUN.groups(m)
    swept = [g for g in grps if RUN.is_load_swept_group(g)]
    once = [g for g in grps if not RUN.is_load_swept_group(g)]
    load_axis = RUN.load_axis(m)

    # Zout(0) seeded at ~12 ohm on every load cell, but craft the dropout PSF with a FLAT Vout
    # (slope ~0) so |dVout/dIload| (~0) disagrees with Zout(0)=12 -> GUARDRAIL-3 must warn for pll.
    for k, (ll, _op) in enumerate(load_axis):
        _seed_real_cell_psf(m, dirs, ll, swept, zout_re=12.0)
    # the OP once-cell: seed iv/tr/z2 normally, but the dropout dc_<o> with a FLAT Vout
    _seed_real_cell_psf(m, dirs, "Lnom", once, zout_re=12.0)
    for o, onet in (("pll", m["v_out"]["pll"]["net"]), ("vco", m["v_out"]["vco"]["net"])):
        gd = dirs["psf"] / "Lnom" / f"g_dc_{o}"
        # DC dropout PSF: swept Iload axis "dc", FLAT Vout -> slope ~ 0 (fabricated/mis-scaled)
        Iload = [1e-4, 5e-4, 1e-3, 3e-3]
        lines = ["HEADER", '"PSFversion" "1.00"', "VALUE"]
        for il in Iload:
            lines.append(f'"dc" {il:g}')
            lines.append(f'"{onet}" 0.8')       # FLAT -> dVout/dIload = 0, far from Zout(0)=12
        lines.append("END")
        (gd / "dcz.dc").write_text("\n".join(lines) + "\n")

    res = PC.run_pmu_coverage_sweep(
        manifest=m, work_root=str(tmp_path), corner=corner, engine="alps",
        base_netlist=str(base), netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"),
        netlister_factory=RecordingFactory(), runner=FakeRunner(), sleep=lambda *_: None,
        steps=["netlist", "run", "import"])

    assert res["guardrail_warnings"], "GUARDRAIL-3 must surface for the fabricated dropout slope"
    w = " ".join(res["guardrail_warnings"])
    assert "GUARDRAIL-3" in w and ("pll" in w or "vco" in w)


# =====================================================================================
# (6) BACKWARD-COMPAT: _has_coverage_sweep is False for a coverage-free manifest;
#     a coverage-free manifest is NOT routed through the sweep; run_pmu_corner unchanged.
# =====================================================================================
def test_has_coverage_sweep_false_for_coverage_free_manifest():
    m = _resolved_wur_manifest()                  # shipped wur: NO loads, NO temps
    assert M.temps(m) == []
    assert RUN.load_axis(m) == [(None, None)]
    assert PC._has_coverage_sweep(m) is False


def test_has_coverage_sweep_true_with_loads_or_temps():
    m_loads = _wur_cov_manifest(loads=True, temps=False, kinds=False)
    assert PC._has_coverage_sweep(m_loads) is True
    m_temps = _wur_cov_manifest(loads=False, temps=True, kinds=False)
    assert PC._has_coverage_sweep(m_temps) is True


def test_run_pmu_corner_body_unchanged_offline_path_still_green(tmp_path):
    """A coverage-free manifest still runs through the UNCHANGED run_pmu_corner offline path
    (the single-corner driver), proving the sweep additions did not disturb it. Mirrors the
    test_pmu_corner offline-sweep dry-run shape."""
    m = _resolved_wur_manifest()
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)
    gnl = NA.make_offline_group_netlister(base, m, dirs["netlist"])

    class Boom:
        def __call__(self, *a, **k):
            raise AssertionError("runner must NOT be called on a dry_run")

    res = PC.run_pmu_corner(
        manifest=m, work_root=str(tmp_path), corner=corner, engine="alps",
        netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"),
        group_netlister=gnl, runner=Boom(), dry_run=True, sleep=lambda *_: None)
    assert res["dsub_cmds"] and len(res["dsub_cmds"]) == 8     # the coverage-free 8-group set
    assert not res["psf_map"]


# =====================================================================================
# (7) dry_run: assembles all per-cell dsub commands, executes nothing, persists the manifest
# =====================================================================================
def test_sweep_dry_run_assembles_no_exec(tmp_path):
    m = _wur_cov_manifest(loads=True, temps=True, kinds=True)
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)

    class Boom:
        def __call__(self, *a, **k):
            raise AssertionError("runner must NOT fire on a dry_run sweep")

    # the DEFAULT factory (real offline netlist_augment) on a dry_run: the per-group netlists are
    # still written, but NOTHING executes (Boom never fires) and no PSF is read.
    res = PC.run_pmu_coverage_sweep(
        manifest=m, work_root=str(tmp_path), corner=corner, engine="alps",
        base_netlist=str(base), netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"),
        runner=Boom(), dry_run=True, sleep=lambda *_: None)

    assert len(res["dsub_cmds"]) == 76                # the full assembled per-cell command set
    for cmd in res["dsub_cmds"]:
        s = " ".join(str(x) for x in cmd)
        assert cmd[0] == "dsub"
        assert "/software/empyrean/alps/2026.03.hf1/bin/alps" in s and "-ade" in s
    assert res["npz"] is None                         # nothing read -> no npz
    assert res["va"] is None
    assert pathlib.Path(res["manifest"]).exists()     # the manifest copy was persisted
    M.load(res["manifest"])                           # ... and it validates
    assert res["steps_run"] == ["netlist", "run"]     # dry stops after the run


def test_sweep_needs_base_or_factory(tmp_path):
    """No base_netlist AND no netlister_factory -> an actionable PmuCornerError (nothing to sweep)."""
    m = _wur_cov_manifest(loads=True, temps=False, kinds=False)
    base = _wur_base_dir(tmp_path)
    with pytest.raises(PC.PmuCornerError) as ei:
        PC.run_pmu_coverage_sweep(
            manifest=m, work_root=str(tmp_path), corner="tt_25c",
            netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"), dry_run=True)
    assert "base_netlist" in str(ei.value) or "netlister_factory" in str(ei.value)


# =====================================================================================
# (N) TEMP-SWEEP SCOPING: coverage.temp_sweep=['dc'] re-runs ONLY the dc/I-V groups at off-
#     nominal temps (small-signal once at the nominal temp). Default (absent) = full per-temp
#     matrix (backward-compatible). This is what cuts the real load x temp run 225 -> 81 jobs.
# =====================================================================================
def test_temp_sweep_scoping_offnominal_runs_dc_only(tmp_path):
    m = _wur_cov_manifest(loads=True, temps=True, kinds=True)         # load x temp
    m["coverage"]["temps"] = [-40, 55, 125]
    m["coverage"]["temp_sweep"] = ["dc"]
    corner = "tt_25c"
    base = _wur_base_dir(tmp_path)
    gui = PC._gui_from_manifest(m)
    _, dirs = PC.corner_dir(str(tmp_path), gui, corner)
    grps = RUN.groups(m)
    swept = [g for g in grps if RUN.is_load_swept_group(g)]
    once = [g for g in grps if not RUN.is_load_swept_group(g)]
    once_dc = [g for g in once if g["analysis"] == "dc"]
    assert once_dc, "fixture needs dc (I-V) groups in the once set"
    # seed exactly what each cell RUNS: nominal temp -> once(all) + load cells(swept);
    # off-nominal temps -> the once-cell's dc groups only.
    for temp in (-40, 55, 125):
        t = f"T{temp:g}"
        _seed_cell_psf(dirs, f"Lnom_{t}", once if temp == 55 else once_dc)
        if temp == 55:
            for (ll, _op) in RUN.load_axis(m):
                _seed_cell_psf(dirs, f"{ll}_{t}", swept)

    factory = RecordingFactory()
    PC.run_pmu_coverage_sweep(
        manifest=m, work_root=str(tmp_path), corner=corner, engine="alps",
        base_netlist=str(base), netlistdir=str(base), pdk_model_dir=str(tmp_path / "pdk"),
        netlister_factory=factory, runner=FakeRunner(), sleep=lambda *_: None,
        steps=["netlist", "run"])

    ran = {}
    for ob, tag in factory.groups:
        ran.setdefault(pathlib.Path(ob).name, set()).add(tag)
    assert ran["Lnom_T55"] == {g["tag"] for g in once}, "nominal temp runs ALL once-groups"
    assert ran["Lnom_T-40"] == {g["tag"] for g in once_dc}, f"off-nom dc only: {ran['Lnom_T-40']}"
    assert ran["Lnom_T125"] == {g["tag"] for g in once_dc}
    # load cells (swept AC/noise) exist only at the nominal temp -- not re-run off-nominal
    assert "L0_T55" in ran and "L0_T-40" not in ran and "L0_T125" not in ran


def test_fan_nominal_smallsignal_completes_offnominal():
    """The npz fan: a non-load-swept temp sweep (the cells ARE the corners) copies the nominal
    temp's small-signal onto its off-nominal siblings so fit_multiport reads a complete npz; @iv
    is left per-temp. A single-temp load group (load sweep) is a no-op."""
    merged = {"z_pll_Lnom_T55": np.array([[1.0, 2.0, 0.0]]),
              "iv_i500n_Lnom_T-40": np.array([[0.0, 1e-6]]),
              "iv_i500n_Lnom_T55": np.array([[0.0, 2e-6]]),
              "iv_i500n_Lnom_T125": np.array([[0.0, 3e-6]])}
    labels = ["Lnom_T-40", "Lnom_T55", "Lnom_T125"]
    ltemp = {"Lnom_T-40": -40.0, "Lnom_T55": 55.0, "Lnom_T125": 125.0}
    PC._fan_nominal_smallsignal(merged, labels, ltemp, 55.0)
    assert "z_pll_Lnom_T-40" in merged and "z_pll_Lnom_T125" in merged
    assert merged["z_pll_Lnom_T-40"] is merged["z_pll_Lnom_T55"], "off-nom small-signal = nominal copy"
    assert merged["iv_i500n_Lnom_T-40"][0, 1] == 1e-6, "@iv must stay per temp (never fanned)"
    # a load-sweep once-cell (single temp per load group, temp=NaN) -> no-op
    m2 = {"z_pll_L0": np.array([[1.0, 2.0, 0.0]])}
    PC._fan_nominal_smallsignal(m2, ["L0"], {"L0": float("nan")}, None)
    assert list(m2) == ["z_pll_L0"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
