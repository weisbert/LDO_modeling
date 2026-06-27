"""Lock the LARGE-SIGNAL branch-A SLEW core (the PMU rail load-transient undershoot fix).

Mechanism (proven locally vs the real WuR_PMU silicon TB, memory real-tb-model-vs-real-comparison):
same load + same decap + same small-signal Zout, yet the real LDO undershoots ~3x deeper than a
pure-LTI model on a load step -> the loop is SLEW-RATE limited, which an AC Zout fundamentally
cannot carry. The fix rate-limits branch-A's R_a regulation current via the built-in `slew()`
operator; slew()==identity at DC and small-signal, so PSRR/noise/Zout are UNCHANGED and the emit
is byte-identical when no slew is fitted (opt-in, same as Cft / d2 / admittance-zero).

These tests pin: (1) emit gating + byte-identical default, (2) only the slewed rail changes,
(3) the SRa fit recovers a known rate from a transient undershoot AND stays silent on a clean
monotonic step, (4) -- when local Spectre is present -- the AC Zout is bit-for-bit unchanged and
the transient undershoot deepens toward the real GT.
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

# reuse the emit fixture (real-shaped 3-rail / 3-bias PMU fit_result)
_tem = __import__("test_emit_pmu_model")


# --------------------------------------------------------------- emit gating
def test_slew_off_is_byte_identical(tmp_path):
    """No slew_a on any rail -> the emitted .va is byte-identical to the pre-slew emit (no SRa
    param, no slew() call anywhere). This is the default path the whole existing suite runs on."""
    res = _tem._real_pmu_fit_result()
    txt = D.emit_pmu_va(res, "PMU_m", tmp_path / "off.va",
                        supply="AVDD1P0", ground="VSS").read_text()
    assert "_SRa" not in txt
    assert "slew(" not in txt
    # branch-A stays the passive resistor on every rail
    assert txt.count("_vrg) <+ ") >= 3  # the V(nA,vrg)<+Ra*I form, one per rail (plus refs)


def test_slew_on_emits_exactly_the_slew_lines(tmp_path):
    """slew_a on ONE rail adds EXACTLY: a `<rail>_SRa` parameter + branch-A becomes the slew()
    form -- and ONLY on that rail. The off->on diff is the gate's whole surface area."""
    off = _tem._real_pmu_fit_result()
    on = _tem._real_pmu_fit_result()
    on["voltage"]["pll"]["slew_a"] = 1.0e4
    t_off = D.emit_pmu_va(off, "PMU_m", tmp_path / "o.va",
                          supply="AVDD1P0", ground="VSS").read_text()
    t_on = D.emit_pmu_va(on, "PMU_m", tmp_path / "n.va",
                         supply="AVDD1P0", ground="VSS").read_text()
    assert "parameter real VDD0P8_PLL_SRa = 1.000000e+04" in t_on
    assert ("I(VDD0P8_PLL_nA, VDD0P8_PLL_vrg) <+ "
            "slew(V(VDD0P8_PLL_nA, VDD0P8_PLL_vrg)/VDD0P8_PLL_Ra, VDD0P8_PLL_SRa)") in t_on
    # the PLL rail no longer has the passive-resistor branch-A form
    assert "V(VDD0P8_PLL_nA, VDD0P8_PLL_vrg) <+ VDD0P8_PLL_Ra*" not in t_on
    # the OTHER rails are untouched (still the resistor, no SRa)
    assert "V(VDD0P8_DIG_nA, VDD0P8_DIG_vrg) <+ VDD0P8_DIG_Ra*" in t_on
    assert "V(VDD0P8_VCO_nA, VDD0P8_VCO_vrg) <+ VDD0P8_VCO_Ra*" in t_on
    assert "VDD0P8_DIG_SRa" not in t_on and "VDD0P8_VCO_SRa" not in t_on
    # everything else byte-identical: drop the 3 changed lines and compare
    import difflib
    changed = [l for l in difflib.unified_diff(t_off.splitlines(), t_on.splitlines(), lineterm="")
               if l and l[0] in "+-" and not l.startswith(("+++", "---"))]
    assert len(changed) == 3, f"expected 3 changed lines, got {len(changed)}: {changed}"


def test_slew_scheduled_path_gated(tmp_path):
    """The MULTI-LOAD (ln(iload) scheduled) emit path also honours the gate: slew_a present ->
    SRa + slew() on that rail; absent -> byte-identical scheduled emit."""
    res = _tem._real_pmu_fit_result()
    # promote the pll rail to a 2-load schedule so it takes _voltage_block_scheduled
    base = res["voltage"]["pll"]
    P = {}
    for il, sc in (("loA", 1.0), ("loB", 1.6)):
        p = dict(base["P"]["nom"]); p["iv"] = 2e-4 * sc
        P[il] = p
    base["P"] = P
    base["slew_a"] = 2.5e4
    txt = D.emit_pmu_va(res, "PMU_s", tmp_path / "s.va",
                        supply="AVDD1P0", ground="VSS").read_text()
    assert "parameter real VDD0P8_PLL_SRa = 2.500000e+04" in txt
    assert "slew(V(VDD0P8_PLL_nA, VDD0P8_PLL_vrg)/VDD0P8_PLL_Ra, VDD0P8_PLL_SRa)" in txt


# --------------------------------------------------------------- SRa fit
def _step_wave(t_edge, t_bottom, depth, settled, span=200e-9, n=400, undershoot=True):
    """Synthesize a load-step [t,V]: flat pre-tail, an edge, a dip to `depth` below `settled` at
    t_edge+t_bottom (undershoot) or a MONOTONE settle (undershoot=False), then the settled tail."""
    t = np.linspace(0.0, span, n)
    pre = 0.80
    V = np.full(n, pre)
    for k, tt in enumerate(t):
        if tt < t_edge:
            V[k] = pre
        elif undershoot:
            if tt < t_edge + t_bottom:                       # slewing down to the dip
                V[k] = pre + (settled - depth - pre) * (tt - t_edge) / t_bottom
            else:                                            # exponential recovery dip->settled
                V[k] = settled - depth * np.exp(-(tt - t_edge - t_bottom) / (2 * t_bottom))
        else:                                                # monotone settle, NO dip below settled
            V[k] = settled + (pre - settled) * np.exp(-(tt - t_edge) / t_bottom)
    return np.c_[t, V]


def test_fit_slew_a_recovers_known_rate():
    """SRa = dI / t_bottom recovered from a transient undershoot within tolerance."""
    dI = 2.0e-3
    t_bottom = 25e-9
    sp = {"tr_pll_2m": _step_wave(50e-9, t_bottom, depth=0.080, settled=0.78)}
    sr = FMP._fit_slew_a(sp, {"tr_pll_2m": (1e-4, 2.1e-3)})
    assert sr is not None
    assert sr == pytest.approx(dI / t_bottom, rel=0.25), f"got {sr:.3g}, want ~{dI/t_bottom:.3g}"


def test_fit_slew_a_silent_on_monotonic_step():
    """A clean monotonically-settling step (no undershoot below the settled level) yields NO SRa
    -> the rail emits byte-identical (the byte-identical default is data-gated, not just flag-gated)."""
    sp = {"tr_pll_2m": _step_wave(50e-9, 30e-9, depth=0.0, settled=0.78, undershoot=False)}
    assert FMP._fit_slew_a(sp, {"tr_pll_2m": (1e-4, 2.1e-3)}) is None


def test_fit_slew_a_none_without_transient():
    """No transient steps -> None -> byte-identical."""
    assert FMP._fit_slew_a({}, {}) is None
    assert FMP._fit_slew_a({"z_pll": np.zeros((5, 3))}, {}) is None


# --------------------------------------------------------------- manifest manual knob
_tfd = __import__("test_fit_multiport_depth")


def test_manifest_slew_a_knob_threads_to_emit(tmp_path):
    """The MANUAL knob: m['v_out'][rail]['slew_a'] flows fit_multiport -> vfit['slew_a'] -> the
    emitted .va carries the editable VDD0P8_PLL_SRa param + the slew()-limited branch-A. This is
    what lets the user tune SRa in their TB. The transient AUTO-fit is held (unreliable on GHz),
    so the manifest is the source of truth."""
    npz, m = _tfd._sweep_npz(tmp_path, name="slewman")
    m["v_out"]["pll"]["pin"] = "VDD0P8_PLL"
    m["v_out"]["pll"]["slew_a"] = 12000.0
    res = FMP.fit_multiport(str(npz), m)
    assert res["voltage"]["pll"]["slew_a"] == 12000.0
    va = D.emit_pmu_va(res, "PMU_man", tmp_path / "man.va",
                       supply="AVDD1P0", ground="VSS").read_text()
    assert "parameter real VDD0P8_PLL_SRa = 1.200000e+04" in va
    assert "slew(V(VDD0P8_PLL_nA, VDD0P8_PLL_vrg)/VDD0P8_PLL_Ra, VDD0P8_PLL_SRa)" in va


def test_manifest_no_slew_a_is_byte_identical(tmp_path):
    """No slew_a in the manifest (and the auto-fit held) -> vfit['slew_a'] None -> no SRa, no
    slew() -> byte-identical to the pre-slew emit. Also a 0/negative slew_a is treated as off."""
    npz, m0 = _tfd._sweep_npz(tmp_path, name="slewoff")
    for sa in (None, 0.0, -1.0):
        import copy
        m = copy.deepcopy(m0)
        m["v_out"]["pll"]["pin"] = "VDD0P8_PLL"
        if sa is not None:
            m["v_out"]["pll"]["slew_a"] = sa
        res = FMP.fit_multiport(str(npz), m)
        assert res["voltage"]["pll"]["slew_a"] is None
        va = D.emit_pmu_va(res, "PMU_off", tmp_path / "off.va",
                           supply="AVDD1P0", ground="VSS").read_text()
        assert "_SRa" not in va and "slew(" not in va


# --------------------------------------------------------------- local Spectre (optional)
def _spectre():
    try:
        import spectre_run as sr
        return sr if sr.available() else None
    except Exception:                                         # noqa: BLE001
        return None


def _emit_single_rail(tmp_path, slew):
    """A clean single-rail PMU (real pasted PLL params) so the transient is a real regulating LDO."""
    G = [1e-6, 0, 1e6, 0, 1e9, 0, 1e9]
    Q = (0, 0, 1e6, 1.0)
    p = dict(iv=1e-4, R_a=9.719499e-2, L_a=2.390843e-5, R_pl=1.602450e2, R_b=1e9, L_b=1.5e-12,
             G0=G[0], G1=G[1], w1=G[2], G2=G[3], w2=G[4], G3=G[5], w3=G[6],
             pcb0=Q[0], pcb1=Q[1], pcw0=Q[2], pcq=Q[3], gnw=1e-12, vreg=0.8,
             _psrr={"AVDD1P0": (G, Q)})
    for k in range(4):
        p[f"gn{k+1}"] = 1e-12
    vf = dict(P={"nom": p}, nfk=list(np.logspace(2, 6, 4)), cout=1e-13, esr=179.3,
              err=[], supplies=["AVDD1P0"], pin="VDD0P8_PLL")
    if slew:
        vf["slew_a"] = 1.0e4
    res = dict(voltage={"pll": vf}, current=[],
               meta=dict(name="pll1", loads=["nom"], supplies=["AVDD1P0"]))
    name = "on.va" if slew else "off.va"
    return D.emit_pmu_va(res, "PLL1", tmp_path / name, supply="AVDD1P0", ground="VSS")


def test_slew_ac_zout_unchanged(tmp_path):
    """slew()==identity in AC -> the emitted small-signal Zout is BIT-FOR-BIT the same with the
    slew core on vs off. This is the guarantee that PSRR / noise / Zout fits are not disturbed."""
    sr = _spectre()
    if sr is None:
        pytest.skip("local Spectre not available")
    off = str(_emit_single_rail(tmp_path, slew=False).resolve())
    on = str(_emit_single_rail(tmp_path, slew=True).resolve())

    def zout(va, tag):
        scs = (f'simulator lang=spectre\nahdl_include "{va}"\n'
               "Xd (AVDD1P0 VDD0P8_PLL 0) PLL1\n"
               "Vs (AVDD1P0 0) vsource dc=1.0 mag=0\n"
               "Iac (0 VDD0P8_PLL) isource mag=1 dc=1e-4\n"
               "ac1 ac start=1k stop=100M dec=10\n")
        d = sr.run(scs, tag)
        return np.abs(np.asarray(d["ac1"]["VDD0P8_PLL"]))

    zo, zn = zout(off, "zoff_lock"), zout(on, "zon_lock")
    assert np.allclose(zo, zn, rtol=0, atol=0), "slew core perturbed the AC Zout"


def test_slew_deepens_transient_undershoot(tmp_path):
    """End-to-end: the emitted model + slew core undershoots DEEPER than slew-off on a load step
    (the linear model is too shallow). Drives a clean 37u->370u step into the emitted .va."""
    sr = _spectre()
    if sr is None:
        pytest.skip("local Spectre not available")
    off = str(_emit_single_rail(tmp_path, slew=False).resolve())
    on = str(_emit_single_rail(tmp_path, slew=True).resolve())

    def step(va, tag):
        scs = (f'simulator lang=spectre\nahdl_include "{va}"\n'
               "Xd (AVDD1P0 VDD0P8_PLL 0) PLL1\n"
               "Vs (AVDD1P0 0) vsource dc=1.0\n"
               "Cd (VDD0P8_PLL 0) capacitor c=60e-12\n"
               "Il (VDD0P8_PLL 0) isource type=pwl wave=[0 3.7e-5 5e-9 3.7e-5 "
               "5.1e-9 3.7e-4 200e-9 3.7e-4]\n"
               "tr tran stop=120e-9 step=2e-11 maxstep=2e-11\n")
        d = sr.run(scs, tag)
        return np.asarray(d["tr"]["VDD0P8_PLL"]).real.min()

    v_off, v_on = step(off, "stoff_lock"), step(on, "ston_lock")
    assert v_on < v_off - 5e-3, f"slew did not deepen the undershoot (off {v_off:.4f} on {v_on:.4f})"
