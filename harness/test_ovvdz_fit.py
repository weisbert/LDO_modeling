"""Lock the PURE-PYTHON unload-discharge transparency-floor derivation (harness/fit_ovvdz.py) -- NO
simulator. It replaces the hardcoded emit_pmu_model._OVD_VDZ (25 mV) with a per-rail ovVdz derived from
THIS rail's characterized large-signal excursion (max|Zout| * di_ripple [+ opt-in spur/PSRR]).

Pins: (1) zout_mag_max reads the characterized Zout plateau; (2) derive_ovvdz REPRODUCES the shipped
hand value (PLL ~25 mV = 2.5*50uA*197Ohm, VCO ~26 mV = 2.5*200uA*52Ohm) end-to-end from the real npz +
manifest; (3) the derived floor is ALWAYS >= the characterized excursion (the transparency guard, in
DERIVATION units -- the Spectre-side guard lives in test_unload_discharge_core.py); (4) priority: a
manual ovVdz wins, a disabled rail stays off, and a rail with NO characterized Zout falls back to
_OVD_VDZ (emit byte-identical to today); (5) the [VDZ_MIN, ALPHA*headroom] clamps engage. All
pure-Python -> runs on the box/GUI/CI.
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

import fit_ovvdz as OV                 # noqa: E402
import emit_pmu_model as E             # noqa: E402

NPZ_AC = HARNESS.parent / "results" / "redzone" / "wur_pmu_real_tt_55c.repro.npz"
MAN = HARNESS.parent / "cadence" / "insitu" / "manifests" / "REAL_wur_pmu_top.json"
NOM = lambda P: list(P)[len(P) // 2]   # noqa: E731 -- same nominal-corner picker fit_multiport uses


# ------------------------------------------------------------- fabricated fixtures (no npz needed)
def _view(zmax, il="tt_55c", prim="avdd1p0", hmax=None):
    """A split_ports-shaped voltage view with a rising-shelf z_<il> whose |Z| plateaus at `zmax` Ohm
    (and an optional PSRR p_<il> plateauing at `hmax` V/V)."""
    f = np.logspace(1, 8.7, 30)
    # rising shelf that clips at the plateau: |Z| ramps up then holds `zmax` at HF -> max|Z| == zmax
    mag = np.minimum(zmax, zmax * f / f[len(f) // 3])
    z = np.column_stack([f, mag, np.zeros_like(f)])       # [freq, Re, Im] (Im=0 -> |Z|=Re=mag)
    npz = {"loads": np.array([il]), f"z_{il}": z}
    supplies = {}
    if hmax is not None:
        h = np.minimum(hmax, hmax * f / f[len(f) // 3])
        p = np.column_stack([f, h, np.zeros_like(f)])
        npz[f"p_{il}"] = p
        supplies = {prim: {il: p}}
    return {"npz": npz, "supplies": supplies, "loads": [il], "primary_supply": prim}


def _volt(il="tt_55c", iv=0.0, vreg=0.82):
    return {"P": {il: {"iv": iv, "vreg": vreg, "R_a": 0.095}}}


def _man(i_op_pll=0.5e-3, supply_dc=0.98, **cov):
    """A minimal manifest with one 'pll' rail, a transient base (i_op) and a primary supply DC."""
    m = {"supplies": {"avdd1p0": {"dc": supply_dc}},
         "v_out": {"pll": {}},
         "coverage": {"transient": {"pll": {"steps": [{"from": i_op_pll, "to": i_op_pll * 4}]}}}}
    m["coverage"].update(cov)
    return m


# ------------------------------------------------------------- unit tests
def test_zout_mag_max_reads_plateau():
    v = _view(197.1)
    assert OV.zout_mag_max(v) == pytest.approx(197.1, rel=1e-3)
    assert OV.zout_mag_max({"npz": {}, "loads": []}) is None      # no z -> None (fallback trigger)
    assert OV.zout_mag_max(None) is None


def test_derive_reproduces_pll_hand_value():
    """The core anchor: 2.5 * (50 uA * 197 Ohm) ~= 24.6 mV. di = RIPPLE_FRAC(0.1) * 500 uA = 50 uA."""
    volt = {"pll": _volt()}
    views = {"pll": _view(197.1)}
    src = OV.derive_ovvdz(volt, views, _man(0.5e-3), supplies=["avdd1p0"], nom_corner=NOM)
    assert src == {"pll": "fit"}
    d = volt["pll"]["ovvdz_diag"]
    assert d["di_A"] == pytest.approx(50e-6)
    assert d["exc_z_V"] == pytest.approx(197.1 * 50e-6)
    assert volt["pll"]["unload_discharge"]["ovVdz"] == pytest.approx(24.6e-3, abs=0.5e-3)
    # the transparency guard, in derivation units: the floor SITS ABOVE the characterized excursion
    assert volt["pll"]["unload_discharge"]["ovVdz"] >= d["exc_V"]


def test_derive_reproduces_vco_hand_value():
    """VCO anchor: 2.5 * (200 uA * 52 Ohm) ~= 26.1 mV. di = 0.1 * 2 mA = 200 uA."""
    volt = {"vco": _volt()}
    views = {"vco": _view(52.14)}
    m = {"supplies": {"avdd1p0": {"dc": 0.98}}, "v_out": {"vco": {}},
         "coverage": {"transient": {"vco": {"steps": [{"from": 2e-3, "to": 6e-3}]}}}}
    src = OV.derive_ovvdz(volt, views, m, supplies=["avdd1p0"], nom_corner=NOM)
    assert src == {"vco": "fit"}
    assert volt["vco"]["unload_discharge"]["ovVdz"] == pytest.approx(26.1e-3, abs=0.5e-3)


def test_manual_ovvdz_wins():
    """A per-rail manual ovVdz (user/manifest) is authoritative -- never overwritten by the derivation."""
    volt = {"pll": dict(_volt(), unload_discharge={"ovVdz": 40e-3, "ovR": 5e3})}
    views = {"pll": _view(197.1)}
    src = OV.derive_ovvdz(volt, views, _man(), supplies=["avdd1p0"], nom_corner=NOM)
    assert src == {"pll": "manual"}
    assert volt["pll"]["unload_discharge"]["ovVdz"] == 40e-3
    assert volt["pll"]["unload_discharge"]["ovR"] == 5e3      # other manual keys preserved
    assert "ovvdz_diag" not in volt["pll"]


def test_manifest_ovvdz_threads_and_wins():
    """A manual ovVdz set on the MANIFEST rail threads onto the fit result and wins over the derivation."""
    volt = {"pll": _volt()}
    views = {"pll": _view(197.1)}
    m = _man()
    m["v_out"]["pll"]["unload_discharge"] = {"ovVdz": 12e-3}
    src = OV.derive_ovvdz(volt, views, m, supplies=["avdd1p0"], nom_corner=NOM)
    assert src == {"pll": "manual"}
    assert volt["pll"]["unload_discharge"]["ovVdz"] == 12e-3


def test_manifest_partial_override_merges_derived():
    """A manifest override that tunes a DIFFERENT knob (e.g. ovR) but not ovVdz -> ovVdz is derived and
    MERGED onto it (the user's ovR is kept)."""
    volt = {"pll": _volt()}
    views = {"pll": _view(197.1)}
    m = _man()
    m["v_out"]["pll"]["unload_discharge"] = {"ovR": 8e3}
    src = OV.derive_ovvdz(volt, views, m, supplies=["avdd1p0"], nom_corner=NOM)
    assert src == {"pll": "fit"}
    assert volt["pll"]["unload_discharge"]["ovR"] == 8e3
    assert volt["pll"]["unload_discharge"]["ovVdz"] == pytest.approx(24.6e-3, abs=0.5e-3)


def test_disabled_rail_stays_off():
    """unload_discharge False/0 -> feature disabled, left disabled (plain stiff resistor at emit)."""
    for off in (False, 0):
        volt = {"pll": dict(_volt(), unload_discharge=off)}
        src = OV.derive_ovvdz(volt, {"pll": _view(197.1)}, _man(), supplies=["avdd1p0"], nom_corner=NOM)
        assert src == {"pll": "off"}
        assert volt["pll"]["unload_discharge"] is False
    # also honored from the manifest
    volt = {"pll": _volt()}
    m = _man()
    m["v_out"]["pll"]["unload_discharge"] = False
    src = OV.derive_ovvdz(volt, {"pll": _view(197.1)}, m, supplies=["avdd1p0"], nom_corner=NOM)
    assert src == {"pll": "off"} and volt["pll"]["unload_discharge"] is False


def test_no_zout_falls_back_to_default():
    """No characterized Zout (legacy/single-OP npz) -> 'default': ovVdz is NOT written, so emit uses
    _OVD_VDZ (byte-identical to today)."""
    volt = {"pll": _volt()}
    views = {"pll": {"npz": {}, "loads": [], "supplies": {}}}
    src = OV.derive_ovvdz(volt, views, _man(), supplies=["avdd1p0"], nom_corner=NOM)
    assert src == {"pll": "default"}
    assert "unload_discharge" not in volt["pll"]              # nothing written -> emit -> _OVD_VDZ


def test_no_op_current_falls_back():
    """A rail with characterized Zout but NO derivable operating load -> 'default' (can't form di)."""
    volt = {"pll": _volt(iv=0.0)}
    m = {"supplies": {"avdd1p0": {"dc": 0.98}}, "v_out": {"pll": {}}, "coverage": {}}
    src = OV.derive_ovvdz(volt, {"pll": _view(197.1)}, m, supplies=["avdd1p0"], nom_corner=NOM)
    assert src == {"pll": "default"} and "unload_discharge" not in volt["pll"]


def test_upper_clamp_to_headroom():
    """A large excursion is clamped to ALPHA*(supply - vreg) so the floor never eats the headroom."""
    volt = {"pll": _volt(vreg=0.82)}
    views = {"pll": _view(5000.0)}                            # absurd |Z| -> K*exc >> headroom
    src = OV.derive_ovvdz(volt, views, _man(0.5e-3, supply_dc=0.98), supplies=["avdd1p0"], nom_corner=NOM)
    assert src == {"pll": "fit"}
    assert volt["pll"]["unload_discharge"]["ovVdz"] == pytest.approx(OV.ALPHA * (0.98 - 0.82))
    assert volt["pll"]["ovvdz_diag"]["capped"] == "headroom"


def test_lower_clamp_to_floor():
    """A tiny excursion is clamped UP to VDZ_MIN so the floor always clears the DC/abuse-sink droop."""
    volt = {"pll": _volt()}
    views = {"pll": _view(1.0)}                              # 1 Ohm * 50 uA * 2.5 = 0.125 mV << VDZ_MIN
    src = OV.derive_ovvdz(volt, views, _man(), supplies=["avdd1p0"], nom_corner=NOM)
    assert volt["pll"]["unload_discharge"]["ovVdz"] == pytest.approx(OV.VDZ_MIN)
    assert volt["pll"]["ovvdz_diag"]["capped"] == "floor"


def test_spur_and_psrr_terms_optin():
    """The spur (sum vout_amp_k) and PSRR (max|H|*vrip) terms ADD to the excursion when characterized,
    and are 0 (Zout-only) otherwise -- WuR has neither, so the base case must not include them."""
    # spur term: a characterized above-vreg tone raises the floor
    volt = {"pll": dict(_volt(), spurs=[{"vout_amp": 8e-3}, {"vout_amp": 2e-3}])}
    src = OV.derive_ovvdz(volt, {"pll": _view(197.1)}, _man(), supplies=["avdd1p0"], nom_corner=NOM)
    assert volt["pll"]["ovvdz_diag"]["exc_spur_V"] == pytest.approx(10e-3)
    assert volt["pll"]["ovvdz_diag"]["exc_V"] == pytest.approx(197.1 * 50e-6 + 10e-3)
    # PSRR term: a supply-ripple spec through |H| raises the floor
    volt2 = {"pll": _volt()}
    m = _man(); m["coverage"]["supply_ripple"] = 20e-3       # 20 mV supply ripple
    src2 = OV.derive_ovvdz(volt2, {"pll": _view(197.1, hmax=0.5)}, m, supplies=["avdd1p0"], nom_corner=NOM)
    assert volt2["pll"]["ovvdz_diag"]["exc_psrr_V"] == pytest.approx(0.5 * 20e-3, rel=1e-2)


# ------------------------------------------------------------- integration (real npz + manifest)
@pytest.mark.skipif(not (NPZ_AC.exists() and MAN.exists()), reason="real WuR npz/manifest absent")
def test_end_to_end_reproduces_shipped_floor_and_emit():
    """fit_multiport -> derive_ovvdz -> emit: the emitted .va carries the DERIVED per-rail ovVdz, ~= the
    shipped 25 mV hand value, and >= each rail's characterized excursion."""
    import fit_multiport as FMP
    import tempfile
    m = json.loads(MAN.read_text())
    fr = FMP.fit_multiport(str(NPZ_AC), m, vout_dc={"pll": 0.82, "vco": 0.82})
    for rk, want in (("pll", 24.6e-3), ("vco", 26.1e-3)):
        d = fr["voltage"][rk]["ovvdz_diag"]
        ov = fr["voltage"][rk]["unload_discharge"]["ovVdz"]
        assert ov == pytest.approx(want, abs=1e-3), (rk, ov)
        assert ov >= d["exc_V"], (rk, ov, d)               # floor above the char excursion
    out = pathlib.Path(tempfile.mkdtemp()) / "PMU_model.va"
    t = E.emit_pmu_va(fr, "PMU_model", out, supply="AVDD1P0", ground="VSS").read_text()
    import re
    for rail, want in (("VDD0P8_PLL", 24.6e-3), ("VDD0P8_VCO", 26.1e-3)):
        mm = re.search(rf"{rail}_ovVdz = ([\d.eE+-]+)", t)
        assert mm and float(mm.group(1)) == pytest.approx(want, abs=1e-3), rail
    # the fallback constant is untouched (a no-z rail would still emit this)
    assert E._OVD_VDZ == pytest.approx(25e-3)
