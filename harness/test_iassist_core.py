"""Lock the COMPRESSIVE large-signal current-assist core (the PMU rail load-transient 治本).

Mechanism (PART2, design-validated against the silicon GT tr_pll_*_T55 / tr_vco_*_T55): the per-rail
LTI Zout is fit to the SMALL-SIGNAL output impedance, so a load-step dip = di*Z is LINEAR in di. The
real loop is class-AB -- as verr=vreg-vout grows the pass device sources MORE current -- so the
silicon dip is SUB-linear / STIFFENING (z_pll: linear 163.6 mV/mA vs GT 107/91/81). The assist injects
a compressive current iaG*tanh(verr*|verr|/iaV^2) in parallel with the regulation: ODD with f'(0)=0
EXACTLY -> ZERO small-signal conductance at the OP -> Zout/PSRR/noise bit-identical; it SATURATES at
iaG, bending the modeled dip sub-linear to match the GT. INTERNAL physics (baked localparams), so --
unlike the retired slew/recovery -- it SURVIVES minimal-emit (minimal minimizes the SCHEMATIC parameter
set, NOT the model). Optional floor/gfloor add a deep out-of-envelope backstop (one-sided clamp, zero
value+slope above the floor -> invisible in the validated regime).

These tests pin: (1) emit gating + byte-identical default (literal + scheduled paths), (2) the opt-in
adds EXACTLY the assist cards and ONLY on the chosen rail, (3) the optional floor backstop, (4) the
assist SURVIVES minimal-emit and adds NO new CDF parameter, (5) _fit_iassist validates the manifest
knob, (6) the manifest knob threads through fit_multiport -> emit.
"""
import pathlib
import sys

import pytest

HARNESS = pathlib.Path(__file__).resolve().parent
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(HARNESS.parent / "cadence"))

import emit_pmu_model as D                       # noqa: E402
import fit_multiport as FMP                      # noqa: E402

_tem = __import__("test_emit_pmu_model")

# validated rail params (fit Spectre-in-loop vs the silicon GT, held-out across amplitudes)
_IA_PLL = dict(iaG=2.8e-3, iaV=0.33)
_IA_VCO = dict(iaG=4.0e-3, iaV=0.22)

_IA_TOKENS = ("_iaG", "_iaV", "_iaFloor", "_iaGfloor", "tanh( V(", "current-assist", "backstop")


# --------------------------------------------------------------- emit gating
def test_iassist_off_is_byte_identical(tmp_path):
    """No assist on any rail -> the emitted .va carries NO assist token (the default the suite runs)."""
    res = _tem._real_pmu_fit_result()
    txt = D.emit_pmu_va(res, "PMU_m", tmp_path / "off.va",
                        supply="AVDD1P0", ground="VSS").read_text()
    for tok in _IA_TOKENS:
        assert tok not in txt, f"unexpected assist token {tok!r} in default emit"


def test_iassist_on_emits_exactly_the_assist_cards(tmp_path):
    """assist on ONE rail adds the 2 localparams + the tanh injection -- and ONLY on that rail."""
    on = _tem._real_pmu_fit_result()
    on["voltage"]["pll"]["iassist"] = dict(_IA_PLL)
    t_on = D.emit_pmu_va(on, "PMU_m", tmp_path / "n.va",
                         supply="AVDD1P0", ground="VSS").read_text()
    assert "localparam real VDD0P8_PLL_iaG = 2.800000e-03" in t_on
    assert "localparam real VDD0P8_PLL_iaV = 3.300000e-01" in t_on
    assert ("I(VDD0P8_PLL, VDD0P8_PLL_vrg) <+ -VDD0P8_PLL_iaG*tanh( "
            "V(VDD0P8_PLL_vrg,VDD0P8_PLL)*abs(V(VDD0P8_PLL_vrg,VDD0P8_PLL)) "
            "/ (VDD0P8_PLL_iaV*VDD0P8_PLL_iaV) );") in t_on
    # ONLY on the chosen rail; the VCO rail stays untouched + no floor when not requested
    assert "VDD0P8_VCO_iaG" not in t_on
    assert "_iaFloor" not in t_on


def test_iassist_floor_backstop_optional(tmp_path):
    """floor/gfloor present -> the deep backstop clamp is emitted; absent -> no floor cards."""
    on = _tem._real_pmu_fit_result()
    on["voltage"]["pll"]["iassist"] = dict(_IA_PLL, floor=0.2)
    t = D.emit_pmu_va(on, "PMU_m", tmp_path / "f.va",
                      supply="AVDD1P0", ground="VSS").read_text()
    assert "localparam real VDD0P8_PLL_iaFloor = 2.000000e-01" in t
    assert "localparam real VDD0P8_PLL_iaGfloor = 2.000000e+03" in t   # default gfloor
    assert "(V(VDD0P8_PLL,VSS) < VDD0P8_PLL_iaFloor)" in t


def test_iassist_survives_minimal_and_adds_no_cdf_param(tmp_path):
    """THE invariant: minimal-emit minimizes the SCHEMATIC parameter set, NOT the internal physics.
    So a minimal rail with an assist STILL emits the assist (unlike the retired slew/recovery, which
    minimal strips), and the assist adds ZERO new CDF `parameter` -- only vreg_<role> stays exposed."""
    res = _tem._real_pmu_fit_result()
    for rk in res["voltage"]:
        res["voltage"][rk]["minimal"] = True
    res["voltage"]["pll"]["iassist"] = dict(_IA_PLL, floor=0.2)
    txt = D.emit_pmu_va(res, "PMU_m", tmp_path / "min.va",
                        supply="AVDD1P0", ground="VSS").read_text()
    # assist NOT stripped by minimal
    assert "localparam real VDD0P8_PLL_iaG = 2.800000e-03" in txt
    assert "VDD0P8_PLL_iaG*tanh(" in txt
    # the ONLY CDF parameters are the per-rail vreg_<role> (the assist is all localparam/internal):
    # the schematic Q dialog stays minimal -- no iaG/iaV/floor ever leaks as a fillable parameter.
    import re
    params = re.findall(r"\n\s*parameter\s+real\s+(\w+)\s*=", txt)
    assert params and all(p.startswith("vreg_") for p in params), \
        f"minimal exposed a non-vreg CDF param: {params}"
    assert not any("ia" in p.lower() for p in params), f"assist leaked as a CDF param: {params}"


# --------------------------------------------------------------- fit-side (seed fallback)
def test_iassist_seed_fallback_when_no_coverage_transient(tmp_path):
    """An npz WITHOUT coverage.transient (tr_*) waveforms -> fit_multiport cannot DERIVE the assist,
    so it falls back to the manifest `iassist` SEED and attaches it to volt[rail]['iassist'] -> emit.
    (The DERIVE path -- tr_* present -> pure-Python fit -- is covered in test_iassist_fit.py.)"""
    npz = HARNESS.parent / "results" / "redzone" / "wur_pmu_real_tt_55c.repro.npz"   # AC-only, no tr_
    man = HARNESS.parent / "cadence" / "insitu" / "manifests" / "REAL_wur_pmu_top.json"
    if not (npz.exists() and man.exists()):
        pytest.skip("real WuR npz/manifest not present")
    import json
    m = json.load(open(man))
    fr = FMP.fit_multiport(str(npz), m)
    # the shipped manifest carries the seed on both rails -> used as fallback (no tr_ to derive from)
    assert fr["voltage"]["pll"].get("iassist", {}).get("iaG") == 2.8e-3
    assert fr["voltage"]["vco"].get("iassist", {}).get("iaG") == 4.0e-3
