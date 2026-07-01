"""Lock the PVT-robustness structural fixes in the emitted PMU model (the "DC solves to a
non-physical current / FF-corner runaway" class of bug, root-caused with the real box + local Spectre):

1. PSRR supply reference AUTO-TRACKS the live supply DC (a slow ~1Hz low-pass on vrf) instead of a
   baked `V(vrf)<+vdc` source. So `V(supply,vrf)` is 0 at DC for ANY supply level -> the DC-coupled
   PSRR term sum(Gi)*(V(supply)-vdc) can no longer inject a spurious DC current when the live supply
   differs from the characterized value (i.e. across a PVT supply sweep). The AC PSRR is unchanged
   above the tracker corner (verified analytically: the tracker is 1Hz, the PSRR band is >~kHz).

2. The DEFAULT regulation (branch-A R_a termination) is the STIFF passive R_a resistor
   `V(nA,vrg) <+ Ra*I(nA,vrg)`. It pins vout to vreg with conductance 1/Ra at EVERY excursion, so the
   DC solve is well-conditioned for any vreg / PVT corner.

   A prior "DC current COMPLIANCE" (`max(-Icomp, min(Icomp, V/Ra))`, and a hand-patched `Icomp*tanh`
   variant) was REMOVED 2026-07-01. Its knee is a VOLTAGE = Icomp*Ra ~ 1.9 mV for the near-zero R_a,
   so >~10 mV off vreg the regulation saturated to a ZERO-conductance ~Icomp source and the output
   node lost its DC pin -> FF-corner runaway (Mvolts) / non-convergence (Spectre-reproduced: floating
   PLL rail + a small imbalance -> 2.3e6 V with the clamp vs 0.804 V with the resistor). The resistor
   is bit-identical in Zout/PSRR/noise (0.0000% over 1Hz-1GHz) AND in the validated large-signal
   transient (which was fit against THIS resistor form). The unbounded DC-fight hazard the compliance
   targeted is already removed by FIX 1 (PSRR auto-track); a real current-limit/dropout is out of this
   LTI core's SCOPE (stage 2b) and must be a one-sided conductance-preserving soft limit, never a
   symmetric hard saturation. slew/recov rails carry their own large-signal handling and keep their form.
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


# --------------------------------------------------------------- FIX 2: regulation = stiff resistor
def test_default_regulation_is_stiff_resistor(tmp_path):
    """The default (no-slew, no-recov) rails emit the passive R_a resistor `V(nA,vrg) <+ Ra*I(nA,vrg)`
    on EVERY such rail (conductance 1/Ra at all excursions -> DC pin preserved everywhere)."""
    res = _tem._real_pmu_fit_result()
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    for rail in ("VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"):
        assert f"V({rail}_nA, {rail}_vrg) <+ {rail}_Ra*I({rail}_nA, {rail}_vrg)" in t, rail


def test_no_regulation_current_compliance_anywhere(tmp_path):
    """REGRESSION guard for the FF-runaway root cause: neither the min/max clamp nor the hand-patched
    Icomp*tanh saturating-current regulation may reappear. The tell-tale symbol is `Icomp` -- it must
    not exist in the emitted card at all (default, slew, or recov rails)."""
    res = _tem._real_pmu_fit_result()
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    assert "Icomp" not in t                       # no localparam, no min/max clamp, no tanh variant
    # the specific saturating-regulation forms, spelled out, must be absent on every rail
    for rail in ("VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"):
        assert f"max(-{rail}_Icomp" not in t, rail
        assert f"{rail}_Icomp*tanh" not in t, rail
    # and the compliance machinery is gone from the generator module itself
    assert not hasattr(D, "_icomp_param") and not hasattr(D, "ICOMP_DEFAULT")


def test_slew_and_recov_rails_keep_their_own_regulation(tmp_path):
    """FIX 2 is the DEFAULT-path form; the large-signal paths are untouched: a slew rail emits the
    slew()-rate-limited R_a current, a recov rail keeps its resistor + its own Imax/deadzone anti-windup.
    None of them (nor the default rail) carry any Icomp compliance."""
    res = _tem._real_pmu_fit_result()
    res["voltage"]["pll"]["slew_a"] = 1.0e4
    res["voltage"]["vco"]["recovery"] = dict(Lreg=1.6e-5, Rreg=750.0, Cs=2.5e-11, Rs=2.0e3)
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    assert "slew(V(VDD0P8_PLL_nA, VDD0P8_PLL_vrg)/VDD0P8_PLL_Ra, VDD0P8_PLL_SRa)" in t   # slew rail intact
    assert "Icomp" not in t                                                              # no compliance anywhere
    assert "V(VDD0P8_DIG_nA, VDD0P8_DIG_vrg) <+ VDD0P8_DIG_Ra*I" in t                    # plain default rail = resistor
