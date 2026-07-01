"""Lock the branch-A UNLOAD-DISCHARGE core (Route 1: anti-windup on the fit-inductor energy trap).

The branch-A fit-inductor L_a carries the FULL DC load current, so on a hard UNLOAD it keeps sourcing
the OLD current into Cout and the rail overshoots. A pure OUTPUT-side clamp cannot win the trade-off
(overshoot-depth x recovery-time ~ const): a local-Spectre baseline shows an output high-side clamp
bounds the overshoot to +24 mV but HANGS ~4.7 us, and no clamp overshoots +302 mV ABOVE the supply.

Route 1 DRAINS the inductor instead of clamping the output: a source-gated, voltage-BOUNDED reverse EMF
added in series to the RESISTOR-form branch-A regulation,
    V(regnode,vrg) <+ R_a*I  +  gate(vov)*srcblk(I)*ovVmax*tanh((ovR/ovVmax)*I)
    gate  = (vov>ovVdz) ? tanh((vov-ovVdz)^2/ovVsc^2) : 0    (vov = V(o,vrg) overshoot; one-sided)
    srcblk= 0.5*(1-tanh(I/ovIsc))   (~1 sourcing = the unload dump, ~0 sinking = a sustained abuse sink)
It is ZERO value+slope at the OP (vov<0 for every load) -> Zout/PSRR/noise/DC are BIT-IDENTICAL; the
reverse EMF is clamped to +-ovVmax so the DC-pin conductance ~1/R_a is ALWAYS preserved (never the
de-pin failure of the reverted "DC current compliance"); the drain is sub-us. DEFAULT-ON + BAKED
localparams -> takes effect on `bash apply`, SURVIVES minimal-emit, adds NO CDF param. Emitted only on
the RESISTOR-form regulation (default + recov-resistor); slew rails keep their slew()+Imax.

ovVdz is the LARGE-SIGNAL TRANSPARENCY FLOOR: the dump engages for any excursion above vreg+ovVdz while
branch A sources -- including the positive half of a large-signal periodic ripple -- so ovVdz must sit
ABOVE the validated ripple regime or it clips the spur/PSRR/Zout spectra the model produces (an
adversarial-review finding). It is therefore set well above the DC droop (default 25 mV), which also
caps where the unload overshoot settles: the fix eliminates the above-supply kick and the 4 us hang
(overshoot -> tens-of-mV <= supply, monotone settle) but recovery-to-5mV is NOT sub-us at that floor.

These tests pin: (1) default-ON + the exact cards on every resistor rail; (2) absent on slew rails;
(3) survives minimal; (4) disabled/overridden via the vfit knob; (5) the disabled form is the pre-Route-1
byte-identical resistor; and -- when local Spectre is present -- (6) AC bit-identical, (7) the unload
overshoot is CAPPED (<= a few*ovVdz, never above supply) and settles monotonically (no hang), (8) the
discharge is INERT under a large-signal periodic ripple (spectra transparent), and (9) a +40 mA
floating-rail abuse-sink stays DC-pinned (no runaway)."""
import pathlib
import sys

import numpy as np
import pytest

HARNESS = pathlib.Path(__file__).resolve().parent
if str(HARNESS) not in sys.path:
    sys.path.insert(0, str(HARNESS))
sys.path.insert(0, str(HARNESS.parent / "cadence"))

import emit_pmu_model as D                       # noqa: E402

_tem = __import__("test_emit_pmu_model")

# deployed WuR PLL-rail params (cadence/wur_real_tb/PMU_model_floor.va) for a faithful single-rail sim
_PLL = dict(R_a=9.533118e-2, L_a=2.288307e-5, R_pl=4.264469e1, R_b=1e9, L_b=1e-12,
            extra=[(4.422177e-6, 1.550403e2)], esr=1.786305e2, cout=1e-13,
            iassist=dict(iaG=4.591519e-3, iaV=4.428571e-1, floor=0.0, gfloor=2e3), vreg=0.82)


# ------------------------------------------------------------- emit gating (no simulator)
def test_discharge_default_on_every_resistor_rail(tmp_path):
    """DEFAULT emit (no slew) -> every rail carries the 5 baked _ov* localparams + the series discharge
    term appended to its R_a regulation. This is the deployed path (default-ON, no manifest opt-in)."""
    res = _tem._real_pmu_fit_result()
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    for rail in ("VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"):
        for p in ("ovVdz", "ovR", "ovVmax", "ovVsc", "ovIsc"):
            assert f"localparam real {rail}_{p} = " in t, f"{rail}_{p} missing"
        # the discharge rides on the R_a resistor regulation (bounded reverse EMF, source-gated)
        assert f"{rail}_ovVmax*tanh(({rail}_ovR/{rail}_ovVmax)*I({rail}_nA" in t, rail
        assert f"(0.5*(1.0 - tanh(I({rail}_nA" in t, rail
        assert f"V({rail}, {rail}_vrg) > {rail}_ovVdz ?" in t, rail


def test_discharge_default_values(tmp_path):
    """The baked defaults (_OVD_*) reach the .va literally."""
    res = _tem._real_pmu_fit_result()
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    assert f"VDD0P8_PLL_ovVdz = {D._OVD_VDZ:.6e}" in t
    assert f"VDD0P8_PLL_ovR = {D._OVD_R:.6e}" in t
    assert f"VDD0P8_PLL_ovVmax = {D._OVD_VMAX:.6e}" in t
    assert f"VDD0P8_PLL_ovVsc = {D._OVD_VSC:.6e}" in t
    assert f"VDD0P8_PLL_ovIsc = {D._OVD_ISC:.6e}" in t


def test_discharge_absent_on_slew_rail(tmp_path):
    """A slew rail keeps its slew()+Imax anti-windup and carries NO discharge (the discharge is the
    RESISTOR-form default-path fix); every OTHER (resistor) rail still has it."""
    res = _tem._real_pmu_fit_result()
    res["voltage"]["pll"]["slew_a"] = 1.0e4
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    for tok in ("VDD0P8_PLL_ovVdz", "VDD0P8_PLL_ovVmax", "VDD0P8_PLL_ovR"):
        assert tok not in t, tok            # slew rail: no discharge
    assert "slew(V(VDD0P8_PLL_nA, VDD0P8_PLL_vrg)/VDD0P8_PLL_Ra, VDD0P8_PLL_SRa)" in t
    assert "VDD0P8_DIG_ovVmax" in t and "VDD0P8_VCO_ovVmax" in t   # resistor rails keep it


def test_discharge_composes_with_recov_resistor(tmp_path):
    """recov (no slew) keeps the resistor regulation -> the discharge rides on it (from node nB)."""
    res = _tem._real_pmu_fit_result()
    res["voltage"]["pll"]["recovery"] = dict(Lreg=1.6e-5, Rreg=750.0, Cs=2.5e-11, Rs=2.0e3)
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    # regulation feeds from nB and carries BOTH the R_a resistor and the discharge term
    assert "V(VDD0P8_PLL_nB, VDD0P8_PLL_vrg) <+ VDD0P8_PLL_Ra*I(VDD0P8_PLL_nB, VDD0P8_PLL_vrg)" in t
    assert "VDD0P8_PLL_ovVmax*tanh((VDD0P8_PLL_ovR/VDD0P8_PLL_ovVmax)*I(VDD0P8_PLL_nB, VDD0P8_PLL_vrg)" in t


def test_discharge_survives_minimal(tmp_path):
    """minimal-emit minimizes the SCHEMATIC param set, NOT the internal physics -> the discharge (baked
    localparams) is still emitted; only vreg_<rail> is exposed and no iload param appears."""
    res = _tem._real_pmu_fit_result()
    for rk in res["voltage"]:
        res["voltage"][rk]["minimal"] = True
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    assert "VDD0P8_PLL_ovVmax*tanh(" in t
    assert "parameter real vreg_pll" in t and "iload_" not in t


def test_discharge_disabled_is_pre_route1_resistor(tmp_path):
    """vfit['unload_discharge']=False -> the rail emits the plain stiff R_a resistor (pre-Route-1
    byte-identical form): no _ov* param, no discharge term."""
    res = _tem._real_pmu_fit_result()
    res["voltage"]["pll"]["unload_discharge"] = False
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    for tok in ("VDD0P8_PLL_ovVdz", "VDD0P8_PLL_ovVmax", "VDD0P8_PLL_ovR", "VDD0P8_PLL_ovIsc"):
        assert tok not in t, tok
    # the synthetic fixture rail has no ladder -> regulation feeds from nA (plain pre-Route-1 resistor)
    assert "V(VDD0P8_PLL_nA, VDD0P8_PLL_vrg) <+ VDD0P8_PLL_Ra*I(VDD0P8_PLL_nA, VDD0P8_PLL_vrg);" in t


def test_discharge_override_dict_reaches_emit(tmp_path):
    """A per-rail override dict retunes the baked values; a bad key falls back to the default."""
    res = _tem._real_pmu_fit_result()
    res["voltage"]["pll"]["unload_discharge"] = dict(ovVdz=5e-3, ovVmax=1.0, ovR="bad")
    t = D.emit_pmu_va(res, "PMU_m", tmp_path / "o.va", supply="AVDD1P0", ground="VSS").read_text()
    assert "VDD0P8_PLL_ovVdz = 5.000000e-03" in t
    assert "VDD0P8_PLL_ovVmax = 1.000000e+00" in t
    assert f"VDD0P8_PLL_ovR = {D._OVD_R:.6e}" in t     # bad override -> default


def test_ovd_vals_helper():
    """_ovd_vals: default-ON (key absent -> full set); False -> None (disabled); dict -> validated."""
    assert D._ovd_vals({}) == dict(ovVdz=D._OVD_VDZ, ovR=D._OVD_R, ovVmax=D._OVD_VMAX,
                                   ovVsc=D._OVD_VSC, ovIsc=D._OVD_ISC)
    assert D._ovd_vals({"unload_discharge": False}) is None
    assert D._ovd_vals({"unload_discharge": 0}) is None
    v = D._ovd_vals({"unload_discharge": {"ovVdz": 2e-3, "ovVmax": -1.0}})
    assert v["ovVdz"] == pytest.approx(2e-3) and v["ovVmax"] == pytest.approx(D._OVD_VMAX)  # bad->default


# ------------------------------------------------------------- local Spectre (optional)
def _spectre():
    try:
        import spectre_run as sr
        return sr if sr.available() else None
    except Exception:                                         # noqa: BLE001
        return None


def _pll_rail(tmp_path, discharge=True, name="pll.va"):
    """Single deployed-param PLL rail with (default) / without the unload-discharge -> a real LDO."""
    G = [1e-6, 0.0, 1e6, 0.0, 1e9, 0.0, 1e9]
    Q = (0.0, 0.0, 1e6, 1.0)
    nfk = list(np.logspace(2, 6, 4))
    p = dict(iv=4e-3, R_a=_PLL["R_a"], L_a=_PLL["L_a"], R_pl=_PLL["R_pl"], R_b=_PLL["R_b"],
             L_b=_PLL["L_b"], extra=list(_PLL["extra"]), G0=G[0], G1=G[1], w1=G[2], G2=G[3],
             w2=G[4], G3=G[5], w3=G[6], pcb0=Q[0], pcb1=Q[1], pcw0=Q[2], pcq=Q[3],
             gnw=1e-12, vreg=_PLL["vreg"], _psrr={"AVDD1P0": (G, Q)})
    for k in range(len(nfk)):
        p[f"gn{k+1}"] = 1e-12
    vf = dict(P={"nom": p}, nfk=nfk, cout=_PLL["cout"], esr=_PLL["esr"], err=[],
              supplies=["AVDD1P0"], pin="VDD0P8_PLL", iassist=dict(_PLL["iassist"]))
    if not discharge:
        vf["unload_discharge"] = False
    res = dict(voltage={"pll": vf}, current=[],
               meta=dict(name="pllUD", loads=["nom"], supplies=["AVDD1P0"]))
    return str(D.emit_pmu_va(res, "PLLR", tmp_path / name, supply="AVDD1P0", ground="VSS").resolve())


def _unload_deck(va, cout=20e-12, avdd=0.98, edge=1e-9, stop=8e-6):
    return (f'simulator lang=spectre\nahdl_include "{va}"\n'
            "Xd (AVDD1P0 VDD0P8_PLL 0) PLLR\n"
            f"Vs (AVDD1P0 0) vsource dc={avdd}\n"
            f"Cd (VDD0P8_PLL 0) capacitor c={cout}\n"
            f"Il (VDD0P8_PLL 0) isource type=pwl wave=[0 4e-3 1e-6 4e-3 {1e-6+edge:g} 0.3e-3 {stop:g} 0.3e-3]\n"
            f"tr tran stop={stop:g} step=1e-9 maxstep=2e-9 errpreset=conservative\n")


def _tr(d, node="VDD0P8_PLL"):
    tr = d["tr"]
    tk = next(k for k in tr if k.lower() in ("time", "tran"))
    vk = next(k for k in tr if k.lower() == node.lower())
    return np.asarray(tr[tk]).real.ravel(), np.asarray(tr[vk]).real.ravel()


def test_unload_overshoot_bounded_le_supply_no_hang(tmp_path):
    """THE regression: a hard unload (4mA->0.3mA, 1ns, 20pF) on the deployed-param PLL rail. WITH the
    discharge the overshoot is CAPPED to physical tens-of-mV (~ovVdz scale), NEVER exceeds the supply, and
    the big kick is prevented sub-us (the rail never reaches the pre-Route-1 ceiling); then it settles
    MONOTONICALLY toward vreg (no chatter, no 4us ceiling-hang). WITHOUT the discharge the branch-A
    fit-inductor kick overshoots ABOVE the supply. (Recovery-to-5mV is NOT sub-us at the default ovVdz --
    that would require ovVdz < 5mV, which distorts the large-signal spectra; see
    test_large_signal_ripple_transparency. ovVdz is a transparency floor, tunable per-rail.)"""
    sr = _spectre()
    if sr is None:
        pytest.skip("local Spectre not available")
    vreg, avdd = 0.82, 0.98
    ovvdz = D._OVD_VDZ
    va_on = _pll_rail(tmp_path, discharge=True, name="on.va")
    va_off = _pll_rail(tmp_path, discharge=False, name="off.va")

    def metrics(va, tag):
        t, v = _tr(sr.run(_unload_deck(va), tag))
        m = t >= 1e-6
        tt, vv = t[m] - 1e-6, v[m]
        ip = int(np.argmax(vv))
        nsc = int(np.sum(np.diff(np.sign(np.diff(vv[ip:]))) != 0))
        return vv.max() - vreg, vv.max(), nsc, np.all(np.isfinite(v)), vv[-1]

    pk_on, vmax_on, nsc_on, fin_on, vfin_on = metrics(va_on, "ud_on")
    pk_off, vmax_off, _, _, _ = metrics(va_off, "ud_off")
    assert fin_on, "discharge unload diverged (non-finite)"
    # CAPPED to the deadzone scale (physical tens-of-mV) and NEVER above the supply
    assert pk_on < 3 * ovvdz + 10e-3, f"overshoot not capped near the deadzone: {pk_on*1e3:.1f} mV (ovVdz={ovvdz*1e3:.0f})"
    assert vmax_on <= avdd, f"overshoot exceeds supply: {vmax_on:.4f} > {avdd}"
    # monotone settle to vreg -- no chatter / no ceiling-hang
    assert nsc_on == 0, f"non-monotone recovery (chatter/hang): {nsc_on} sign changes"
    assert abs(vfin_on - vreg) < 2e-3, f"did not settle to vreg: {vfin_on:.5f}"
    # fixes BOTH pre-Route-1 pathologies: the baseline exceeds supply AND overshoots more
    assert vmax_off > avdd and pk_on < pk_off, \
        f"discharge must fix the baseline: on={pk_on*1e3:.1f}mV/{vmax_on:.3f} off={pk_off*1e3:.1f}mV/{vmax_off:.3f}"


def test_large_signal_ripple_transparency(tmp_path):
    """#6 / the adversarial-review guard: the discharge must be INERT under a large-signal PERIODIC drive
    (the spur/PSRR/Zout regime the model exists to produce). A sinusoidal load that swings the rail up to
    ~ovVdz above vreg must give a rail waveform BIT-identical (<0.2 mV) to the discharge-OFF emit -- i.e.
    the dump does not clip the positive ripple peaks. (At ovVdz=3mV this fails by ~16 mV; the default
    ovVdz sits above the validated large-signal ripple so the dump stays off in-band.)"""
    sr = _spectre()
    if sr is None:
        pytest.skip("local Spectre not available")
    va_on = _pll_rail(tmp_path, discharge=True, name="ron.va")
    va_off = _pll_rail(tmp_path, discharge=False, name="roff.va")
    f, iac = 5e6, 0.12e-3          # swings the rail ~15-18 mV above vreg (just under the default ovVdz)
    T = 1.0 / f

    def ripple(va, tag):
        scs = (f'simulator lang=spectre\nahdl_include "{va}"\n'
               "Xd (AVDD1P0 VDD0P8_PLL 0) PLLR\nVs (AVDD1P0 0) vsource dc=0.98\n"
               "Cd (VDD0P8_PLL 0) capacitor c=20e-12\n"
               f"Il (VDD0P8_PLL 0) isource dc=2e-3 type=sine ampl={iac} freq={f}\n"
               f"tr tran stop={8*T:g} step={T/200:g} maxstep={T/200:g} errpreset=conservative\n")
        t, v = _tr(sr.run(scs, tag))
        m = t >= 4 * T                       # steady-state cycles only
        return t[m], v[m]

    ton, von = ripple(va_on, "rip_on")
    tof, vof = ripple(va_off, "rip_off")
    dist = np.max(np.abs(von - np.interp(ton, tof, vof)))
    above = vof.max() - 0.82
    assert above > 8e-3, f"drive too weak to exercise the guard (only {above*1e3:.1f} mV above vreg)"
    assert dist < 0.2e-3, f"discharge distorts large-signal ripple: {dist*1e3:.3f} mV (ovVdz={D._OVD_VDZ*1e3:.0f} mV)"


def test_ac_bit_identical_with_discharge(tmp_path):
    """#1: the discharge is ZERO value+slope at the OP -> |Zout(f)| 1Hz-1GHz is BIT-identical to the
    discharge-off emit at BOTH a light and a heavy load OP (the deadzone must not perturb small-signal)."""
    sr = _spectre()
    if sr is None:
        pytest.skip("local Spectre not available")
    va_on = _pll_rail(tmp_path, discharge=True, name="acon.va")
    va_off = _pll_rail(tmp_path, discharge=False, name="acoff.va")

    def zmag(va, iload, tag):
        scs = (f'simulator lang=spectre\nahdl_include "{va}"\n'
               "Xd (AVDD1P0 VDD0P8_PLL 0) PLLR\nVs (AVDD1P0 0) vsource dc=0.98 mag=1\n"
               "Cd (VDD0P8_PLL 0) capacitor c=20e-12\n"
               f"Il (VDD0P8_PLL 0) isource dc={iload} mag=1\nac ac start=1 stop=1e9 dec=20\n")
        o = sr.run(scs, tag)["ac"]
        vk = next(k for k in o if k.lower() == "vdd0p8_pll")
        return np.abs(np.asarray(o[vk], complex))
    for il, tag in ((4e-3, "hi"), (3e-4, "lo")):
        z1 = zmag(va_on, il, f"acon_{tag}")
        z0 = zmag(va_off, il, f"acoff_{tag}")
        n = min(len(z1), len(z0))
        assert n > 20 and np.allclose(z1[:n], z0[:n], rtol=0, atol=1e-12), \
            f"discharge perturbed AC |Z| at iload={il}"


def test_plus40mA_abuse_sink_stays_pinned(tmp_path):
    """#4: a +40 mA floating-rail injection (an abuse SINK, ~100x the rail) must NOT engage the drain --
    srcblk kills it and the bounded EMF preserves the 1/R_a DC pin -> the rail settles ~vreg (<= supply),
    no runaway. This is the property the reverted 'DC current compliance' violated."""
    sr = _spectre()
    if sr is None:
        pytest.skip("local Spectre not available")
    va = _pll_rail(tmp_path, discharge=True, name="inj.va")
    scs = (f'simulator lang=spectre\nahdl_include "{va}"\n'
           "Xd (AVDD1P0 VDD0P8_PLL 0) PLLR\nVs (AVDD1P0 0) vsource dc=0.98\n"
           "Cd (VDD0P8_PLL 0) capacitor c=20e-12\nIi (0 VDD0P8_PLL) isource dc=40e-3\n"
           "tr tran stop=8e-6 step=2e-9 maxstep=5e-9 errpreset=conservative\n")
    _, v = _tr(sr.run(scs, "ud_inj40"))
    assert np.all(np.isfinite(v)), "+40mA injection diverged"
    assert v.max() < 1.0, f"+40mA runaway / not pinned: max {v.max():.4f} V"
    assert abs(v[-1] - 0.82) < 0.02, f"+40mA final not near vreg: {v[-1]:.4f} V"
