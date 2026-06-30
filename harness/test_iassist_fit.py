"""Lock the PURE-PYTHON current-assist derivation (harness/fit_iassist.py) -- NO simulator.

Pins: (1) predict_dip (the branch-A ladder + Cext + assist ODE) reproduces the Spectre load-step dip
on both WuR rails, baseline + assisted, to <3 mV; (2) gt_dips_from_npz reads the coverage.transient
GT straight out of the extraction npz; (3) fit_rail solves (iaG,iaV) from the GT with a good held-out
prediction, off the search boundary; (4) derive_iassist DERIVES from the tr_* GT when present and
falls back to the manifest seed otherwise. All pure-Python -> runs on the box/GUI/CI (no Spectre/ALPS).
"""
import json
import pathlib
import sys

import numpy as np
import pytest

HARNESS = pathlib.Path(__file__).resolve().parent
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(HARNESS.parent / "cadence"))

import fit_iassist as F            # noqa: E402

# the deployed WuR PLL/VCO branch-A Zout (from the fitted model)
PLL = dict(Ra=9.533047e-02, sections=[(2.289480e-05, 4.286014e+01), (4.378928e-06, 1.515896e+02)])
VCO = dict(Ra=2.093699e-02, sections=[(8.552905e-07, 2.467427e+00), (1.218538e-06, 4.909871e+01)])
NPZ_TR = HARNESS.parent / "results" / "redzone" / "wur_pmu_real_sweep.repro.npz"
NPZ_AC = HARNESS.parent / "results" / "redzone" / "wur_pmu_real_tt_55c.repro.npz"
MAN = HARNESS.parent / "cadence" / "insitu" / "manifests" / "REAL_wur_pmu_top.json"


def test_predict_dip_matches_spectre_both_rails():
    """The pure-Python ODE reproduces the Spectre load-step dip (the predictor the whole fit rests on)."""
    pll = [F.predict_dip(PLL["Ra"], PLL["sections"], 20e-12, 0.5e-3, x) for x in (2e-3, 3e-3, 4e-3)]
    assert pll == pytest.approx([243.1, 405.1, 567.1], abs=3)          # PLL baseline
    pll_a = [F.predict_dip(PLL["Ra"], PLL["sections"], 20e-12, 0.5e-3, x, 2.8e-3, 0.33)
             for x in (2e-3, 3e-3, 4e-3)]
    assert pll_a == pytest.approx([154, 224, 290], abs=3)              # PLL assisted
    vco = [F.predict_dip(VCO["Ra"], VCO["sections"], 20e-12, 2e-3, x) for x in (4e-3, 5e-3, 6e-3)]
    assert vco == pytest.approx([93.9, 140.6, 187.3], abs=3)          # VCO baseline


def test_predict_dip_assist_is_monotone_and_bounded():
    """More assist -> shallower dip (the compressive direction); iaG=0 is the LTI baseline."""
    base = F.predict_dip(PLL["Ra"], PLL["sections"], 20e-12, 0.5e-3, 4e-3, 0.0)
    a1 = F.predict_dip(PLL["Ra"], PLL["sections"], 20e-12, 0.5e-3, 4e-3, 2e-3, 0.33)
    a2 = F.predict_dip(PLL["Ra"], PLL["sections"], 20e-12, 0.5e-3, 4e-3, 5e-3, 0.33)
    assert base > a1 > a2 > 0


@pytest.mark.skipif(not NPZ_TR.exists(), reason="real WuR sweep npz absent")
def test_gt_dips_from_npz():
    dips = F.gt_dips_from_npz(str(NPZ_TR), "pll", 0.5e-3)
    assert sorted(round(k * 1e3, 1) for k in dips) == [1.5, 2.5, 3.5]
    assert [round(dips[k] * 1e3) for k in sorted(dips)] == pytest.approx([160, 227, 282], abs=2)


@pytest.mark.skipif(not NPZ_TR.exists(), reason="real WuR sweep npz absent")
def test_fit_rail_solves_from_gt():
    gt = F.gt_dips_from_npz(str(NPZ_TR), "pll", 0.5e-3)
    r = F.fit_rail(PLL["Ra"], PLL["sections"], 20e-12, 0.5e-3, gt)
    assert r["iaG"] > 0 and r["iaV"] > 0
    d = r["_diag"]
    assert d["rms_mV"] < 10.0, d                       # dips reproduced
    assert abs(d["held_out"]["err_pct"]) < 8.0, d      # generalizes across amplitudes
    assert not d["on_boundary"], d                     # converged inside the search range


@pytest.mark.skipif(not (NPZ_TR.exists() and NPZ_AC.exists() and MAN.exists()),
                    reason="real WuR npz/manifest absent")
def test_derive_iassist_fits_when_tr_present_else_seed():
    import fit_multiport as FMP
    m = json.loads(MAN.read_text())
    for rk in m["v_out"]:
        m["v_out"][rk].pop("iassist", None)            # hide the seed
    fr = FMP.fit_multiport(str(NPZ_AC), m)             # LTI fit (AC npz, no tr_)
    nom = lambda P: list(P)[len(P) // 2]               # noqa: E731
    # tr_* present -> DERIVE both rails pure-Python
    src = F.derive_iassist(fr["voltage"], str(NPZ_TR), m, nom_corner=nom)
    assert src == {"pll": "fit", "vco": "fit"}
    assert 0 < fr["voltage"]["pll"]["iassist"]["iaG"] < 1.2e-2
    assert abs(fr["voltage"]["pll"]["iassist_diag"]["held_out"]["err_pct"]) < 8.0
    assert "floor" not in fr["voltage"]["pll"]["iassist"]   # backstop OFF by default

    # no tr_ (AC npz) but a manifest seed -> fall back to the seed
    m2 = json.loads(MAN.read_text())                  # shipped manifest carries the seed
    fr2 = FMP.fit_multiport(str(NPZ_AC), m2)
    src2 = F.derive_iassist(fr2["voltage"], str(NPZ_AC), m2, nom_corner=nom)
    assert src2 == {"pll": "seed", "vco": "seed"}
    assert fr2["voltage"]["pll"]["iassist"]["iaG"] == pytest.approx(2.8e-3)


@pytest.mark.skipif(not (NPZ_TR.exists() and NPZ_AC.exists() and MAN.exists()),
                    reason="real WuR npz/manifest absent")
def test_derive_iassist_accepts_pathlib_path():
    """REGRESSION: a pathlib.Path npz must DERIVE, not silently fall back to the seed. The GUI run
    path (cli.py) + the emit/fit CLIs pass a Path; the old `isinstance(npz, str)`-only guard treated
    it as pre-loaded data -> the assist degraded to the seed everywhere except the box step_fit
    (which str()'d it). gt_dips_from_npz and derive_iassist must both accept os.PathLike."""
    import fit_multiport as FMP
    assert isinstance(NPZ_TR, pathlib.Path)                  # the fixtures ARE Paths, not strs
    # gt_dips reads tr_* straight off a Path
    dips = F.gt_dips_from_npz(NPZ_TR, "pll", 0.5e-3)
    assert sorted(round(k * 1e3, 1) for k in dips) == [1.5, 2.5, 3.5]
    # derive_iassist DERIVES from a Path. Hide the seed so a regressed (mishandled) Path -> 'none',
    # not a seed that masks the bug.
    m = json.loads(MAN.read_text())
    for rk in m["v_out"]:
        m["v_out"][rk].pop("iassist", None)
    fr = FMP.fit_multiport(str(NPZ_AC), m)
    nom = lambda P: list(P)[len(P) // 2]                     # noqa: E731
    src = F.derive_iassist(fr["voltage"], NPZ_TR, m, nom_corner=nom)
    assert src == {"pll": "fit", "vco": "fit"}, src


@pytest.mark.skipif(not (NPZ_TR.exists() and NPZ_AC.exists() and MAN.exists()),
                    reason="real WuR npz/manifest absent")
def test_derive_iassist_carries_manifest_floor_onto_derived():
    """The deep backstop floor is a manifest knob INDEPENDENT of the iaG/iaV derivation. The derived
    dict carries only iaG/iaV, so floor/gfloor must be merged onto it -- else a manifest floor would
    only ever reach the seed path, and the DEPLOYED model derives (tr_* present), so it would never
    see the floor. The shipped REAL manifest enables floor=0.0."""
    import fit_multiport as FMP
    m = json.loads(MAN.read_text())
    assert m["v_out"]["pll"]["iassist"].get("floor") == 0.0   # shipped manifest: backstop ON
    fr = FMP.fit_multiport(str(NPZ_AC), m)
    nom = lambda P: list(P)[len(P) // 2]                       # noqa: E731
    src = F.derive_iassist(fr["voltage"], NPZ_TR, m, nom_corner=nom)   # tr_* present -> DERIVE
    assert src == {"pll": "fit", "vco": "fit"}, src
    assert fr["voltage"]["pll"]["iassist"]["floor"] == 0.0     # floor carried onto the DERIVED assist
    assert fr["voltage"]["vco"]["iassist"]["floor"] == 0.0
    assert fr["voltage"]["pll"]["iassist"]["iaG"] != pytest.approx(2.8e-3)  # iaG is DERIVED, not the seed
