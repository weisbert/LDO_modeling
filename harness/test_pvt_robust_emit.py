"""Lock the two PVT-robustness structural fixes in the emitted PMU model (the "DC solves to a
non-physical current" class of bug, root-caused with the real box):

1. PSRR supply reference AUTO-TRACKS the live supply DC (a slow ~1Hz low-pass on vrf) instead of a
   baked `V(vrf)<+vdc` source. So `V(supply,vrf)` is 0 at DC for ANY supply level -> the DC-coupled
   PSRR term sum(Gi)*(V(supply)-vdc) can no longer inject a spurious DC current when the live supply
   differs from the characterized value (i.e. across a PVT supply sweep). The AC PSRR is unchanged
   above the tracker corner (verified analytically: the tracker is 1Hz, the PSRR band is >~kHz).

2. The DEFAULT regulation (branch-A R_a termination) gets a DC current COMPLIANCE (the same min/max
   anti-windup form the recovery path already uses): it is EXACTLY V/Ra while |I|<=Icomp -> Zout /
   PSRR / noise are BITWISE unchanged in the validated load, but the DC regulation current clamps to
   +-Icomp. A near-zero R_a used to convert a sub-mV output mismatch (off-corner / co-driven node)
   into hundreds of mA and an ill-conditioned DC solve; the clamp bounds it -> well-conditioned DC
   for ANY vreg. slew/recov rails carry their own large-signal handling and keep their form (no Icomp).
"""
import pathlib
import sys

HARNESS = pathlib.Path(__file__).resolve().parent
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(HARNESS.parent / "cadence"))

import emit_pmu_model as D                       # noqa: E402

_tem = __import__("test_emit_pmu_model")


# --------------------------------------------------------------- FIX 1: PSRR DC auto-track
def test_psrr_reference_auto_tracks_supply(tmp_path):
    """vrf is a slow low-pass of the live supply, NOT a baked vdc source -> zero DC injection at any
    supply. The baked `V(vrf)<+vdc` source is gone; the tracker RC + its localparams are present."""
    res = _tem._real_pmu_fit_result()
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    # the tracker: vrf follows AVDD1P0 through Rtrk, with Ctrk to ground (per rail)
    assert ("I(AVDD1P0, VDD0P8_PLL_vrf) <+ (V(AVDD1P0, VSS) - V(VDD0P8_PLL_vrf, VSS))/Rtrk_psrr" in t)
    assert ("I(VDD0P8_PLL_vrf, VSS) <+ Ctrk_psrr*ddt(V(VDD0P8_PLL_vrf, VSS))" in t)
    assert "localparam real Rtrk_psrr =" in t and "localparam real Ctrk_psrr =" in t
    # the OLD baked source must be gone on every rail
    assert "V(VDD0P8_PLL_vrf, VSS) <+ vdc_AVDD1P0" not in t
    assert "V(VDD0P8_VCO_vrf, VSS) <+ vdc_AVDD1P0" not in t
    # the PSRR injection term itself is UNCHANGED (still reads V(supply,vrf)); only vrf's definition moved
    assert "VDD0P8_PLL_G0*V(AVDD1P0, VDD0P8_PLL_vrf)" in t
    # vdc_AVDD1P0 still exists (the current-bias gdd term uses it) but as a localparam, not a param
    assert "localparam real vdc_AVDD1P0 =" in t and "parameter real vdc_AVDD1P0" not in t


# --------------------------------------------------------------- FIX 2: regulation DC compliance
def test_default_regulation_has_dc_compliance(tmp_path):
    """The default (no-slew, no-recov) rails emit the min/max DC-compliance regulation + an Icomp
    localparam, on EVERY such rail. In-band it is exactly V/Ra (the '/Ra' is literally present)."""
    res = _tem._real_pmu_fit_result()
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    for rail in ("VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"):
        assert (f"I({rail}_nA, {rail}_vrg) <+ max(-{rail}_Icomp, min({rail}_Icomp,"
                f" V({rail}_nA, {rail}_vrg)/{rail}_Ra))" in t), rail
        assert f"localparam real {rail}_Icomp =" in t, rail
        # the old unbounded resistor form must be gone
        assert f"V({rail}_nA, {rail}_vrg) <+ {rail}_Ra*I" not in t, rail


def test_icomp_value_overridable_per_rail(tmp_path):
    """Icomp defaults to ICOMP_DEFAULT but is taken from the rail's fitted params p['icomp'] when set
    (the pass-device max / dropout current, ideally characterized per corner)."""
    res = _tem._real_pmu_fit_result()
    # default
    t0 = D.emit_pmu_va(res, "PMU_m", tmp_path / "d.va", supply="AVDD1P0", ground="VSS").read_text()
    assert f"VDD0P8_PLL_Icomp = {D.ICOMP_DEFAULT:.6e}" in t0
    # override on the PLL rail only (P is a dict keyed by corner label; set every corner's dict)
    res2 = _tem._real_pmu_fit_result()
    for Pil in res2["voltage"]["pll"]["P"].values():
        Pil["icomp"] = 8.0e-3
    t1 = D.emit_pmu_va(res2, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    assert "VDD0P8_PLL_Icomp = 8.000000e-03" in t1
    assert f"VDD0P8_VCO_Icomp = {D.ICOMP_DEFAULT:.6e}" in t1   # other rails keep the default


def test_slew_and_recov_rails_have_no_icomp(tmp_path):
    """The compliance is the DEFAULT-path fix only. A slew rail (slew() rate-limit) and a recov rail
    (its own Imax/deadzone anti-windup) keep their own regulation and get NO Icomp localparam."""
    res = _tem._real_pmu_fit_result()
    res["voltage"]["pll"]["slew_a"] = 1.0e4
    res["voltage"]["vco"]["recovery"] = dict(Lreg=1.6e-5, Rreg=750.0, Cs=2.5e-11, Rs=2.0e3)
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    assert "VDD0P8_PLL_Icomp" not in t          # slew rail: no compliance localparam
    assert "VDD0P8_VCO_Icomp" not in t          # recov rail: no compliance localparam
    assert "VDD0P8_DIG_Icomp" in t              # the plain default rail still gets it
    # and the slew/recov rails reference no undefined Icomp in their bodies
    assert "VDD0P8_PLL_Icomp, min" not in t and "VDD0P8_VCO_Icomp, min" not in t
