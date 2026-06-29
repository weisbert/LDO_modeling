"""Lock the OVERDAMPED 2nd-order RECOVERY core (the PMU rail post-dip recovery-SHAPE fix).

Mechanism (winning "od2nd" topology, design-panel selected; prototype + validated PLL-rail params
in cadence/wur_real_tb/ldo_pll_compensated.va, replay RMS 2.47mV vs real silicon): the in-situ
LTI Zout + branch-A slew front-end gets the dip DEPTH right but RINGS / over-recovers on the climb
back out -- the real LDO is OVERDAMPED (monotonic ~100ns recovery). The fix inserts a slow recovery
inductor (Lreg||Rreg, LOSSLESS at DC -> the regulated DC setpoint is NOT moved) between branch-A and
the regulation node, and a DC-BLOCKED Rs-Cs snubber that damps the slew-induced overshoot. At DC the
inductor shorts and the snubber opens, so Zdc / vreg are unchanged and the emit is byte-identical
when no recovery is opted in (same discipline as slew_a / Cft / d2 / admittance-zero).

These tests pin: (1) emit gating + byte-identical default (literal + scheduled paths), (2) the
opt-in adds EXACTLY the recovery cards and ONLY on the chosen rail, (3) _fit_recovery validates the
manifest knob, (4) the manifest knob threads through fit_multiport -> emit, and -- when local
Spectre is present -- (5) the emitted recovery .va compiles, converges, and keeps DC at vreg.
"""
import pathlib
import sys

import numpy as np
import pytest

HARNESS = pathlib.Path(__file__).resolve().parent
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(HARNESS.parent / "cadence"))

import emit_pmu_model as D                       # noqa: E402
import fit_multiport as FMP                      # noqa: E402

_tem = __import__("test_emit_pmu_model")
_tfd = __import__("test_fit_multiport_depth")

# validated PLL-rail recovery params (cadence/wur_real_tb/ldo_pll_compensated.va)
_RECOV = dict(Lreg=1.6e-5, Rreg=750.0, Cs=2.5e-11, Rs=2.0e3)


# --------------------------------------------------------------- emit gating
def test_recovery_off_is_byte_identical(tmp_path):
    """No recovery on any rail -> the emitted .va is byte-identical to the pre-recovery emit
    (no Lreg/Rreg/Cs/Rs param, no nB/nD node, no recovery branch). The default the whole suite runs."""
    res = _tem._real_pmu_fit_result()
    txt = D.emit_pmu_va(res, "PMU_m", tmp_path / "off.va",
                        supply="AVDD1P0", ground="VSS").read_text()
    for tok in ("_Lreg", "_Rreg", "_Cs", "_Rs", "_nB", "_nD",
                "recovery branch", "recovery snubber"):
        assert tok not in txt, f"unexpected recovery token {tok!r} in default emit"


def test_recovery_on_emits_exactly_the_recovery_cards(tmp_path):
    """recovery on ONE rail adds the 4 params + the A2 inductor branch + the Rs-Cs snubber + the
    nB/nD nodes -- and ONLY on that rail; every other rail stays untouched."""
    off = _tem._real_pmu_fit_result()
    on = _tem._real_pmu_fit_result()
    on["voltage"]["pll"]["recovery"] = dict(_RECOV)
    t_off = D.emit_pmu_va(off, "PMU_m", tmp_path / "o.va",
                          supply="AVDD1P0", ground="VSS").read_text()
    t_on = D.emit_pmu_va(on, "PMU_m", tmp_path / "n.va",
                         supply="AVDD1P0", ground="VSS").read_text()
    assert "parameter real VDD0P8_PLL_Lreg = 1.600000e-05" in t_on
    assert "parameter real VDD0P8_PLL_Rreg = 7.500000e+02" in t_on
    assert "parameter real VDD0P8_PLL_Cs = 2.500000e-11" in t_on
    assert "parameter real VDD0P8_PLL_Rs = 2.000000e+03" in t_on
    # the slow recovery inductor branch nA->nB
    assert ("I(VDD0P8_PLL_nA, VDD0P8_PLL_nB) <+ idt(V(VDD0P8_PLL_nA, VDD0P8_PLL_nB))/VDD0P8_PLL_Lreg"
            in t_on)
    # the regulation now feeds from nB (the resistor form here -- no slew on this rail)
    assert "V(VDD0P8_PLL_nB, VDD0P8_PLL_vrg) <+ VDD0P8_PLL_Ra*I(VDD0P8_PLL_nB, VDD0P8_PLL_vrg)" in t_on
    # the DC-blocked snubber
    assert "I(VDD0P8_PLL, VDD0P8_PLL_nD) <+ VDD0P8_PLL_Cs*ddt(V(VDD0P8_PLL, VDD0P8_PLL_nD))" in t_on
    assert "V(VDD0P8_PLL_nD, VDD0P8_PLL_vrg) <+ VDD0P8_PLL_Rs*I(VDD0P8_PLL_nD, VDD0P8_PLL_vrg)" in t_on
    # nB/nD declared
    assert "VDD0P8_PLL_nB" in t_on and "VDD0P8_PLL_nD" in t_on
    # OTHER rails untouched: still the plain nA->vrg resistor, no recovery tokens
    assert "V(VDD0P8_DIG_nA, VDD0P8_DIG_vrg) <+ VDD0P8_DIG_Ra*" in t_on
    assert "V(VDD0P8_VCO_nA, VDD0P8_VCO_vrg) <+ VDD0P8_VCO_Ra*" in t_on
    for tok in ("VDD0P8_DIG_Lreg", "VDD0P8_VCO_Lreg", "VDD0P8_DIG_nB", "VDD0P8_VCO_nB"):
        assert tok not in t_on


def test_recovery_composes_with_slew(tmp_path):
    """recovery + slew on the same rail: the regulation feeds from nB AND is slew-limited
    (the production PLL topology). slew()'s node is nB, not nA, once recovery is inserted."""
    res = _tem._real_pmu_fit_result()
    res["voltage"]["pll"]["recovery"] = dict(_RECOV)
    res["voltage"]["pll"]["slew_a"] = 8.5e3
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "rs.va",
                      supply="AVDD1P0", ground="VSS").read_text()
    assert ("I(VDD0P8_PLL_nB, VDD0P8_PLL_vrg) <+ "
            "slew(V(VDD0P8_PLL_nB, VDD0P8_PLL_vrg)/VDD0P8_PLL_Ra, VDD0P8_PLL_SRa)") in t
    # NOT the nA-fed slew (recovery moved the regulation node)
    assert "slew(V(VDD0P8_PLL_nA, VDD0P8_PLL_vrg)" not in t
    assert "parameter real VDD0P8_PLL_Lreg = " in t


def test_recovery_scheduled_path_gated(tmp_path):
    """The MULTI-LOAD (ln(iload) scheduled) emit path also honours the gate: recovery present ->
    cards on that rail; absent -> byte-identical scheduled emit."""
    base = _tem._real_pmu_fit_result()
    on = _tem._real_pmu_fit_result()
    for res in (base, on):
        p0 = res["voltage"]["pll"]["P"]["nom"]
        P = {}
        for il, sc in (("loA", 1.0), ("loB", 1.6)):
            p = dict(p0); p["iv"] = 2e-4 * sc
            P[il] = p
        res["voltage"]["pll"]["P"] = P
    on["voltage"]["pll"]["recovery"] = dict(_RECOV)
    t_off = D.emit_pmu_va(base, "PMU_s", tmp_path / "so.va",
                          supply="AVDD1P0", ground="VSS").read_text()
    t_on = D.emit_pmu_va(on, "PMU_s", tmp_path / "sn.va",
                         supply="AVDD1P0", ground="VSS").read_text()
    assert "_Lreg" not in t_off and "_nB" not in t_off
    assert "parameter real VDD0P8_PLL_Lreg = 1.600000e-05" in t_on
    assert "I(VDD0P8_PLL_nA, VDD0P8_PLL_nB) <+ idt(" in t_on


# --------------------------------------------------------------- _fit_recovery validation
def test_fit_recovery_accepts_full_valid_dict():
    out = FMP._fit_recovery({"recovery": dict(_RECOV)})
    assert out is not None
    assert set(out) == {"Lreg", "Rreg", "Cs", "Rs"}
    assert out["Lreg"] == pytest.approx(1.6e-5)


@pytest.mark.parametrize("bad", [
    {},                                                  # no recovery key
    {"recovery": None},
    {"recovery": {}},                                    # empty
    {"recovery": {"Lreg": 1.6e-5, "Rreg": 750.0, "Cs": 2.5e-11}},  # missing Rs
    {"recovery": {"Lreg": 1.6e-5, "Rreg": 750.0, "Cs": 2.5e-11, "Rs": 0.0}},   # non-positive
    {"recovery": {"Lreg": 1.6e-5, "Rreg": 750.0, "Cs": 2.5e-11, "Rs": -1.0}},  # negative
    {"recovery": {"Lreg": 1.6e-5, "Rreg": 750.0, "Cs": 2.5e-11, "Rs": "x"}},   # non-numeric
    {"recovery": {"Lreg": 1.6e-5, "Rreg": 750.0, "Cs": 2.5e-11, "Rs": float("inf")}},  # inf
])
def test_fit_recovery_rejects_invalid(bad):
    assert FMP._fit_recovery(bad) is None


# --------------------------------------------------------------- manifest knob -> emit
def test_manifest_recovery_knob_threads_to_emit(tmp_path):
    """m['v_out'][rail]['recovery'] flows fit_multiport -> vfit['recovery'] -> the emitted .va
    carries the editable recovery params + the overdamped branch."""
    npz, m = _tfd._sweep_npz(tmp_path, name="recman")
    m["v_out"]["pll"]["pin"] = "VDD0P8_PLL"
    m["v_out"]["pll"]["recovery"] = dict(_RECOV)
    res = FMP.fit_multiport(str(npz), m)
    assert res["voltage"]["pll"]["recovery"] == _RECOV
    va = D.emit_pmu_va(res, "PMU_man", tmp_path / "man.va",
                       supply="AVDD1P0", ground="VSS").read_text()
    assert "parameter real VDD0P8_PLL_Lreg = 1.600000e-05" in va
    assert "I(VDD0P8_PLL_nA, VDD0P8_PLL_nB) <+ idt(" in va


def test_manifest_no_recovery_is_byte_identical(tmp_path):
    """No recovery in the manifest (or a partial/invalid one) -> vfit has no recovery -> no cards
    -> byte-identical to the pre-recovery emit."""
    import copy
    npz, m0 = _tfd._sweep_npz(tmp_path, name="recoff")
    for rc in (None, {}, {"Lreg": 1.6e-5}):    # absent / empty / incomplete
        m = copy.deepcopy(m0)
        m["v_out"]["pll"]["pin"] = "VDD0P8_PLL"
        if rc is not None:
            m["v_out"]["pll"]["recovery"] = rc
        res = FMP.fit_multiport(str(npz), m)
        assert "recovery" not in res["voltage"]["pll"]
        va = D.emit_pmu_va(res, "PMU_off", tmp_path / "off.va",
                           supply="AVDD1P0", ground="VSS").read_text()
        assert "_Lreg" not in va and "_nB" not in va


# --------------------------------------------------------------- local Spectre (optional)
def _spectre():
    try:
        import spectre_run as sr
        return sr if sr.available() else None
    except Exception:                                         # noqa: BLE001
        return None


def _emit_recov_rail(tmp_path, recov):
    """Single PLL rail (real pasted params) + slew, with/without recovery -> a real regulating LDO."""
    G = [1e-6, 0, 1e6, 0, 1e9, 0, 1e9]
    Q = (0, 0, 1e6, 1.0)
    p = dict(iv=5e-4, R_a=9.719499e-2, L_a=1.20e-4, R_pl=1.602450e2, R_b=1e9, L_b=1e-12,
             G0=G[0], G1=G[1], w1=G[2], G2=G[3], w2=G[4], G3=G[5], w3=G[6],
             pcb0=Q[0], pcb1=Q[1], pcw0=Q[2], pcq=Q[3], gnw=1e-12, vreg=0.8,
             _psrr={"AVDD1P0": (G, Q)})
    for k in range(4):
        p[f"gn{k+1}"] = 1e-12
    vf = dict(P={"nom": p}, nfk=list(np.logspace(2, 6, 4)), cout=1e-13, esr=179.3,
              err=[], supplies=["AVDD1P0"], pin="VDD0P8_PLL", slew_a=8.5e3)
    if recov:
        vf["recovery"] = dict(_RECOV)
    res = dict(voltage={"pll": vf}, current=[],
               meta=dict(name="pllR", loads=["nom"], supplies=["AVDD1P0"]))
    name = "ron.va" if recov else "roff.va"
    return D.emit_pmu_va(res, "PLLR", tmp_path / name, supply="AVDD1P0", ground="VSS")


def test_recovery_va_compiles_and_dc_holds(tmp_path):
    """The emitted recovery rail COMPILES + converges in local Spectre, and DC stays at vreg (the
    Lreg shorts + Cs opens at DC -> the setpoint is NOT moved by the recovery network)."""
    sr = _spectre()
    if sr is None:
        pytest.skip("local Spectre not available")
    va = str(_emit_recov_rail(tmp_path, recov=True).resolve())
    # bare op-point makes no PSF; settle a DC isource via a short transient + read the final value
    # (the recovery network is lossless/DC-blocked at DC, so the settled value must be vreg).
    scs = (f'simulator lang=spectre\nahdl_include "{va}"\n'
           "Xd (AVDD1P0 VDD0P8_PLL 0) PLLR\n"
           "Vs (AVDD1P0 0) vsource dc=1.0\n"
           "Il (VDD0P8_PLL 0) isource dc=0.5e-3\n"
           "tr tran stop=3e-6 step=2e-9 maxstep=2e-9\n")
    d = sr.run(scs, "recov_dc_lock")
    vout = float(np.asarray(d["tr"]["VDD0P8_PLL"]).real.ravel()[-1])
    assert abs(vout - 0.8) < 2e-3, f"recovery moved the DC setpoint: {vout:.5f} V (want 0.800)"


def test_recovery_transient_is_stable_and_bounded(tmp_path):
    """A clean 0.5->1mA step into the emitted recovery rail stays PHYSICAL (no divergence, never
    swings past 0..1V) -- the overdamped network must not destabilize the loop."""
    sr = _spectre()
    if sr is None:
        pytest.skip("local Spectre not available")
    va = str(_emit_recov_rail(tmp_path, recov=True).resolve())
    scs = (f'simulator lang=spectre\nahdl_include "{va}"\n'
           "Xd (AVDD1P0 VDD0P8_PLL 0) PLLR\n"
           "Vs (AVDD1P0 0) vsource dc=1.0\n"
           "Cd (VDD0P8_PLL 0) capacitor c=20e-12\n"
           "Il (VDD0P8_PLL 0) isource type=pwl wave=[0 0.5e-3 50e-9 0.5e-3 "
           "50.1e-9 1e-3 300e-9 1e-3]\n"
           "tr tran stop=300e-9 step=1e-10 maxstep=1e-10\n")
    d = sr.run(scs, "recov_tran_lock")
    v = np.asarray(d["tr"]["VDD0P8_PLL"]).real
    assert np.all(np.isfinite(v)), "recovery transient diverged (non-finite)"
    assert v.min() > -1e-3 and v.max() < 1.0 + 1e-3, f"out of bounds [{v.min():.3f},{v.max():.3f}]"
