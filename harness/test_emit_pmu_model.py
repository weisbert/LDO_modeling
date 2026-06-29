"""Unit/static tests for Component D (harness/emit_pmu_model.py). NO simulator: the
emitted .va is checked statically (module/port/interface + the built-in VA sanity
check). One test exercises a SMALL synthetic fit_result shaped like the REAL PMU
(1 supply AVDD1P0, 3 voltage rails, 3 current biases); another rides the real
fit_multiport output on the stand-in npz if present."""
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "cadence")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import emit_pmu_model as D  # noqa: E402


# ------------------------------------------------------------- synthetic fixture
def _vfit(vreg=0.8, nnoise=6):
    """A minimal but COMPLETE voltage-output fit (what fit_multiport produces per rail):
    one corner 'nom' with all Zout/PSRR/noise params + per-supply _psrr."""
    G = [1e-3, -5e-4, 2e5, 2e-4, 1e6, -1e-4, 5e6]      # [G0,G1,w1,G2,w2,G3,w3]
    Q = (1e-3, 1e-9, 3e6, 2.0)                          # (pcb0,pcb1,pcw0,pcq)
    p = dict(iv=5e-4, R_a=0.05, L_a=1e-7, R_pl=1e3, R_b=1e9, L_b=1e-12,
             G0=G[0], G1=G[1], w1=G[2], G2=G[3], w2=G[4], G3=G[5], w3=G[6],
             pcb0=Q[0], pcb1=Q[1], pcw0=Q[2], pcq=Q[3],
             gnw=1e-9, vreg=vreg,
             _psrr={"AVDD1P0": (G, Q)})
    for k in range(nnoise):
        p[f"gn{k+1}"] = 1e-9 * (k + 1)
    nfk = list(np.logspace(2, 6, nnoise))
    return dict(P={"nom": p}, nfk=nfk, cout=2.0e-8, esr=0.12,
                err=[], supplies=["AVDD1P0"])


def _crow(sink, g0=1e-7, Cp=1e-15, pi_dc=2e-7):
    return dict(sink=sink, il="nom", g0=g0, Cp=Cp, yrms=1e-12, ydc=g0,
                pi={"AVDD1P0": {"rms": 1e-12, "dc": pi_dc}})


def _real_pmu_fit_result():
    """The contract's real PMU exactly as the REAL PRODUCER shapes it: fit_multiport keys
    voltage/current by the manifest ROLE KEYS (dig/pll/vco, i1p8u/...) and propagates the
    designer's GUI symbol pin name in a 'pin' side field. emit_pmu_va must name the module
    PORTS from 'pin' (not the role key) so the .va binds to the pmuBuildModelCell symbol."""
    v_rails = [("dig", "VDD0P8_DIG"), ("pll", "VDD0P8_PLL"), ("vco", "VDD0P8_VCO")]
    i_bias = [("i1p8u", "IBP_POLY_1P8U_VCO"), ("i500n", "IBP_POLY_500N_VCO_Fit"),
              ("i1p5u", "IBP_PTAT_TUNE_1P5U_VCO")]
    voltage = {}
    for i, (rk, pin) in enumerate(v_rails):
        vf = _vfit(vreg=0.8 + 0.001 * i)
        vf["pin"] = pin                      # what fit_multiport propagates from the manifest
        voltage[rk] = vf
    current = []
    for i, (rk, pin) in enumerate(i_bias):
        cr = _crow(rk, g0=1e-7 * (i + 1))    # sink = role key
        cr["pin"] = pin
        current.append(cr)
    return dict(voltage=voltage, current=current,
                meta=dict(name="pmu_real_synth", loads=["nom"], supplies=["AVDD1P0"]))


# --------------------------------------------------------------------- tests
def _port_list(va_text):
    import re
    mh = re.search(r"module\s+\w+\s*\(([^)]*)\)\s*;", va_text)
    return [t.strip() for t in mh.group(1).split(",") if t.strip()]


def test_real_pmu_interface(tmp_path):
    """1 supply in (AVDD1P0) / 3 rails + 3 biases out / VSS ground, exact order."""
    res = _real_pmu_fit_result()
    va = tmp_path / "PMU_model.va"
    out = D.emit_pmu_va(res, "PMU_model", va, supply="AVDD1P0", ground="VSS")
    assert out == va and va.exists()
    text = va.read_text()
    expected = (["AVDD1P0"]
                + ["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"]
                + ["IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit", "IBP_PTAT_TUNE_1P5U_VCO"]
                + ["VSS"])
    assert _port_list(text) == expected, _port_list(text)
    # exactly one module + the built-in sanity check passes on the emitted text
    assert text.count("endmodule") == 1
    assert "module PMU_model(" in text
    ok, problems = D.va_sanity(text, "AVDD1P0",
                               ["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"],
                               ["IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit",
                                "IBP_PTAT_TUNE_1P5U_VCO"], "VSS")
    assert ok, problems


def test_direction_decls(tmp_path):
    res = _real_pmu_fit_result()
    text = D.emit_pmu_va(res, "PMU_model", tmp_path / "m.va").read_text()
    assert "input AVDD1P0;" in text
    assert "inout VSS;" in text
    # every output named in an 'output' decl
    for o in ["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO",
              "IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit", "IBP_PTAT_TUNE_1P5U_VCO"]:
        assert o in text.split("inout")[0]  # appears in the header/decl region


def test_fitted_params_not_placeholders(tmp_path):
    """The fitted Zout/PSRR/noise literals must reach the .va (not zeros/defaults)."""
    res = _real_pmu_fit_result()
    text = D.emit_pmu_va(res, "PMU_model", tmp_path / "m.va").read_text()
    # branch params, PSRR bank gains, complex section, noise gms appear as literals
    assert "VDD0P8_DIG_Ra = 5.000000e-02;" in text
    assert "VDD0P8_DIG_G0 = 1.000000e-03;" in text
    assert "VDD0P8_DIG_pcb0 = 1.000000e-03;" in text
    assert "VDD0P8_DIG_gnw = 1.000000e-09;" in text
    # current bias: admittance + current-PSRR literals
    assert "IBP_POLY_1P8U_VCO_g0 = 1.000000e-07;" in text
    assert "IBP_POLY_1P8U_VCO_pidc = 2.000000e-07;" in text
    # PSRR injected INTO the right rail node, referenced to the single supply
    assert "I(VDD0P8_DIG, VSS) <+ -(VDD0P8_DIG_G0*V(AVDD1P0, VDD0P8_DIG_vrf)" in text


def test_per_output_node_namespacing(tmp_path):
    """Each rail's internal nodes are prefixed so the 3 rails don't collide."""
    res = _real_pmu_fit_result()
    text = D.emit_pmu_va(res, "PMU_model", tmp_path / "m.va").read_text()
    for o in ["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"]:
        assert f"{o}_vrg" in text and f"{o}_nA" in text and f"{o}_nw" in text


def test_sanity_catches_bad_interface():
    """va_sanity must REJECT a port-list / direction mismatch (guards the box)."""
    good = """module M(AVDD1P0, VO, VSS);
  input AVDD1P0;
  output VO;
  inout VSS;
  analog begin
  end
endmodule
"""
    ok, _ = D.va_sanity(good, "AVDD1P0", ["VO"], [], "VSS")
    assert ok
    # wrong: missing the ground inout decl
    bad = good.replace("  inout VSS;\n", "")
    ok2, probs = D.va_sanity(bad, "AVDD1P0", ["VO"], [], "VSS")
    assert not ok2 and any("VSS" in p for p in probs)
    # wrong: two modules
    ok3, probs3 = D.va_sanity(good + good, "AVDD1P0", ["VO"], [], "VSS")
    assert not ok3 and any("module" in p for p in probs3)


def test_stand_in_via_fit_multiport(tmp_path):
    """End-to-end on the real fit_multiport output (stand-in npz) if present: 2 rails +
    2 sinks. Validates the consumer contract against the ACTUAL producer."""
    npz = ROOT / "results" / "ref" / "pmu_standin.npz"
    if not npz.exists():
        import pytest
        pytest.skip("stand-in npz not present")
    import fit_multiport as FMP
    from insitu import manifest as M
    m = M.load("pmu_top")
    res = FMP.fit_multiport(npz, m)
    va = tmp_path / "PMU_standin.va"
    D.emit_pmu_va(res, "PMU_standin", va, supply="AVDD1P0", ground="VSS")
    text = va.read_text()
    ports = _port_list(text)
    assert ports[0] == "AVDD1P0" and ports[-1] == "VSS"
    # the stand-in's outputs (pll, vco + i500n, i1u) all became module ports
    for o in ["pll", "vco", "i500n", "i1u"]:
        assert o in ports
    ok, problems = D.va_sanity(text, "AVDD1P0", ["pll", "vco"],
                               ["i500n", "i1u"], "VSS")
    assert ok, problems


def test_pin_propagation_via_fit_multiport(tmp_path):
    """REAL producer chain: a manifest carrying 'pin' fields -> fit_multiport propagates
    them -> emit_pmu_va names the module PORTS from the pins (not the internal role keys).
    This is the integration guard for the role-key/pin-name seam (uses the stand-in npz +
    its manifest, with synthetic GUI pin names injected onto the role keys)."""
    npz = ROOT / "results" / "ref" / "pmu_standin.npz"
    if not npz.exists():
        import pytest
        pytest.skip("stand-in npz not present")
    import fit_multiport as FMP
    from insitu import manifest as M
    m = M.load("pmu_top")
    # inject GUI symbol pin names onto the manifest role keys (what build_manifest does)
    pinmap_v = {o: f"VRAIL_{o.upper()}" for o in m["v_out"]}
    pinmap_i = {c: f"IBIAS_{c.upper()}" for c in m["i_out"]}
    for o, pin in pinmap_v.items():
        m["v_out"][o]["pin"] = pin
    for c, pin in pinmap_i.items():
        m["i_out"][c]["pin"] = pin
    res = FMP.fit_multiport(npz, m)
    # fit_multiport must have propagated 'pin' onto every port
    for o in m["v_out"]:
        assert res["voltage"][o]["pin"] == pinmap_v[o]
    for r in res["current"]:
        assert r["pin"] == pinmap_i[r["sink"]]
    # emit names the module ports from the PINS, never the role keys
    text = D.emit_pmu_va(res, "PMU_pinned", tmp_path / "p.va",
                         supply="AVDD1P0", ground="VSS").read_text()
    ports = _port_list(text)
    assert ports[0] == "AVDD1P0" and ports[-1] == "VSS"
    # every interior port is a GUI pin name; the internal role keys never appear as ports
    assert set(ports[1:-1]) == set(pinmap_v.values()) | set(pinmap_i.values())
    assert not (set(m["v_out"]) | set(m["i_out"])) & set(ports)


# ===================================================== ln(iload) scheduling (T3)
def _multiload_vfit(currents, vreg0=0.8, nnoise=4):
    """A 3-load voltage fit with REAL distinct iv per load and DISTINCT per-load param
    values (so each scheduled poly is non-degenerate). Carries schedule_loads (what
    fit_multiport now exposes) so emit_pmu_va takes the scheduling path."""
    G = [1e-3, -5e-4, 2e5, 2e-4, 1e6, -1e-4, 5e6]
    Q = (1e-3, 1e-9, 3e6, 2.0)
    labels = [f"L{i}" for i in range(len(currents))]

    def mkP(iv, scale):
        p = dict(iv=iv, R_a=0.05 * scale, L_a=1e-7 * scale, R_pl=1e3 * scale,
                 R_b=1e9, L_b=1e-12 * scale,
                 G0=G[0] * scale, G1=G[1] * scale, w1=G[2] * scale, G2=G[3] * scale,
                 w2=G[4] * scale, G3=G[5] * scale, w3=G[6] * scale,
                 pcb0=Q[0] * scale, pcb1=Q[1] * scale, pcw0=Q[2] * scale, pcq=Q[3] * scale,
                 gnw=1e-9 * scale, vreg=vreg0 + 0.05 * scale,
                 _psrr={"AVDD1P0": (G, Q)})
        for k in range(nnoise):
            p[f"gn{k+1}"] = 1e-9 * (k + 1) * scale
        return p
    scales = [1.0, 1.3, 0.7][:len(currents)]
    P = {labels[i]: mkP(currents[i], scales[i]) for i in range(len(currents))}
    nfk = list(np.logspace(2, 6, nnoise))
    return dict(P=P, nfk=nfk, cout=2e-8, esr=0.12, err=[], supplies=["AVDD1P0"],
                schedule_loads=labels, pin="VPLL"), labels


def _eval_va_expr(expr, u):
    """Numerically evaluate an emitted clamped scheduling expr (exp/min/max/poly in u)."""
    import re
    py = re.sub(r"\bexp\b", "np.exp", expr)
    py = py.replace("min(", "np.minimum(").replace("max(", "np.maximum(")
    return float(eval(py, {"np": np, "u": u}))   # noqa: S307 (trusted, generated)


def test_pmu_scheduling_multiload(tmp_path):
    """A 3-load voltage fit (real distinct iv) -> the .va carries `parameter real iload_<o>`
    + scheduled (min/max/exp/u) param exprs; AT each corner load the scheduled expr
    evaluates (in python) to the per-corner fitted value (corner-exact to the emitted
    6-sig-fig precision). va_sanity passes."""
    currents = [50e-6, 580e-6, 2000e-6]
    vfit, labels = _multiload_vfit(currents)
    res = dict(voltage={"pll": vfit}, current=[],
               meta=dict(name="sched", loads=labels, supplies=["AVDD1P0"]))
    va = tmp_path / "sched.va"
    D.emit_pmu_va(res, "PMU_sched", va, supply="AVDD1P0", ground="VSS")
    txt = va.read_text()
    # the per-rail iload module parameter + the clamped ln(iload) drive
    assert "parameter real iload_VPLL =" in txt
    assert "VPLL_u = ln(min(max(iload_VPLL," in txt
    # a few scheduled params are min/max-clamped exp/poly in the rail's u (not literals)
    assert "VPLL_Ra = min(max(exp(" in txt
    assert "VPLL_G0 = min(max(" in txt          # signed -> no exp, still clamped
    # header documents the scheduling
    assert "load-SCHEDULED" in txt and "ln(iload" in txt
    ok, problems = D.va_sanity(txt, "AVDD1P0", ["VPLL"], [], "VSS")
    assert ok, problems

    # CORNER-EXACT: each scheduled expr evaluates to the per-corner fitted value at that
    # corner's iload (reuse the proven fit_model._pexpr machinery -> same residual).
    P = vfit["P"]
    spec = list(D._SCHED_SPEC_BASE) + [(f"gn{k+1}", True) for k in range(len(vfit["nfk"]))]
    for key, logspace in spec:
        vals = [float(P[il][key]) for il in labels]
        expr = D._sched_expr(currents, vals, "u", logspace)
        for il, cur in zip(labels, currents):
            got = _eval_va_expr(expr, np.log(cur))
            ref = float(P[il][key])
            assert abs(got - ref) <= 1e-4 * (abs(ref) + 1e-30), (key, il, got, ref)


def test_pmu_single_load_bakes_literals(tmp_path):
    """A 1-load fit (single OP / coverage-free) -> NO iload_<o> param, NO scheduled exprs:
    params are baked as LITERALS exactly as before (byte-identical path). va_sanity passes."""
    vfit = _vfit()                                  # single 'nom' corner, no schedule_loads
    res = dict(voltage={"dig": vfit}, current=[],
               meta=dict(name="single", loads=["nom"], supplies=["AVDD1P0"]))
    res["voltage"]["dig"]["pin"] = "VDD0P8_DIG"
    va = tmp_path / "single.va"
    D.emit_pmu_va(res, "PMU_single", va, supply="AVDD1P0", ground="VSS")
    txt = va.read_text()
    assert "parameter real iload_" not in txt, "single OP must NOT emit an iload schedule"
    assert "min(max(exp(" not in txt, "single OP must bake literals, not schedule"
    assert "VDD0P8_DIG_Ra = 5.000000e-02;" in txt   # literal, as before
    assert "load-SCHEDULED" not in txt
    assert "no ln(iload) interpolation" in txt      # the single-OP header note
    ok, problems = D.va_sanity(txt, "AVDD1P0", ["VDD0P8_DIG"], [], "VSS")
    assert ok, problems


def test_pmu_schedule_skips_degenerate(tmp_path):
    """schedule_loads with DUPLICATE iv (degenerate poly) -> falls back to baked literals
    (no schedule), so a coverage run that happened to repeat one load stays well-posed."""
    vfit, labels = _multiload_vfit([500e-6, 500e-6, 500e-6])   # all identical iv
    res = dict(voltage={"pll": vfit}, current=[],
               meta=dict(name="dup", loads=labels, supplies=["AVDD1P0"]))
    va = tmp_path / "dup.va"
    D.emit_pmu_va(res, "PMU_dup", va, supply="AVDD1P0", ground="VSS")
    txt = va.read_text()
    assert "parameter real iload_" not in txt, "duplicate iv must NOT schedule (degenerate)"
    ok, problems = D.va_sanity(txt, "AVDD1P0", ["VPLL"], [], "VSS")
    assert ok, problems


def test_pmu_mixed_scheduled_and_baked(tmp_path):
    """A PMU with ONE scheduled rail (3 loads) + ONE single-OP rail: only the scheduled rail
    gets an iload param; both rails coexist in one module; va_sanity passes."""
    sched_fit, labels = _multiload_vfit([50e-6, 580e-6, 2000e-6])
    sched_fit["pin"] = "VPLL"
    single_fit = _vfit()
    single_fit["pin"] = "VDIG"
    res = dict(voltage={"pll": sched_fit, "dig": single_fit}, current=[],
               meta=dict(name="mix", loads=labels, supplies=["AVDD1P0"]))
    va = tmp_path / "mix.va"
    D.emit_pmu_va(res, "PMU_mix", va, supply="AVDD1P0", ground="VSS")
    txt = va.read_text()
    assert "parameter real iload_VPLL =" in txt      # scheduled rail
    assert "parameter real iload_VDIG" not in txt    # single-OP rail
    assert "VDIG_Ra = 5.000000e-02;" in txt          # baked literal
    assert "VPLL_Ra = min(max(exp(" in txt           # scheduled
    ok, problems = D.va_sanity(txt, "AVDD1P0", ["VPLL", "VDIG"], [], "VSS")
    assert ok, problems


def test_pmu_largesignal_vs_legacy_current_dispatch(tmp_path):
    """A current row carrying idc55 -> the large-signal VA block (idc55/didt/gdd/knee);
    a legacy row (no idc55) -> the legacy AC-only block. Both reach the emitted .va."""
    G = [1e-3, -5e-4, 2e5, 2e-4, 1e6, -1e-4, 5e6]
    Q = (1e-3, 1e-9, 3e6, 2.0)
    vf = _vfit()
    vf["pin"] = "VPLL"
    ls_row = dict(sink="ils", pin="IB_LS", pol="sink", idc55=2.0e-4, didt=1e-7,
                  g0=1e-7, vc=0.4, gdd=2e-7, vknee=0.05, knee_p=1.0, Cp=1e-15,
                  in_white=0.0, in_kf=0.0, tnom_c=55.0)
    legacy_row = _crow("ilg", g0=3e-7, pi_dc=5e-7)
    legacy_row["pin"] = "IB_LG"
    res = dict(voltage={"pll": vf}, current=[ls_row, legacy_row],
               meta=dict(name="cur", loads=["nom"], supplies=["AVDD1P0"]))
    va = tmp_path / "cur.va"
    D.emit_pmu_va(res, "PMU_cur", va, supply="AVDD1P0", ground="VSS", supply_dc=1.05)
    txt = va.read_text()
    # large-signal block markers (sink fold of gdd -> -gdd)
    assert "IB_LS_idc55 = 2.000000e-04;" in txt
    assert "IB_LS_gdd = -2.000000e-07;" in txt
    assert "tanh(pow(" in txt and "$temperature -" in txt
    # legacy block markers (admittance + |PI(0)| magnitude), no large-signal core for it
    assert "IB_LG_g0 = 3.000000e-07;" in txt
    assert "IB_LG_pidc = 5.000000e-07;" in txt
    assert "IB_LG_idc55" not in txt
    ok, problems = D.va_sanity(txt, "AVDD1P0", ["VPLL"], ["IB_LS", "IB_LG"], "VSS")
    assert ok, problems


def test_split_ground_per_port(tmp_path):
    """port_grounds binds each port to its own ground net: the DISTINCT nets become extra
    module ground pins, each rail/bias references ONLY its assigned ground, and va_sanity
    accepts the multi-ground interface."""
    res = _real_pmu_fit_result()
    pg = {"VDD0P8_PLL": "VSS_PLL", "VDD0P8_DIG": "VSS_PLL", "IBP_POLY_500N_VCO_Fit": "VSS_PLL",
          "VDD0P8_VCO": "VSS_VCO", "IBP_POLY_1P8U_VCO": "VSS_VCO",
          "IBP_PTAT_TUNE_1P5U_VCO": "VSS_VCO"}
    txt = D.emit_pmu_va(res, "PMU_split", tmp_path / "s.va", supply="AVDD1P0",
                        ground="VSS_PLL", port_grounds=pg).read_text()
    # both ground pins present, in first-seen order, after all the outputs
    assert _port_list(txt) == (["AVDD1P0", "VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO",
                                "IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit",
                                "IBP_PTAT_TUNE_1P5U_VCO", "VSS_PLL", "VSS_VCO"])
    assert "inout VSS_PLL, VSS_VCO;" in txt
    # each rail returns to its assigned ground only
    assert "I(VDD0P8_VCO, VSS_VCO)" in txt and "I(VDD0P8_VCO, VSS_PLL)" not in txt
    assert "I(VDD0P8_PLL, VSS_PLL)" in txt and "I(VDD0P8_PLL, VSS_VCO)" not in txt
    assert "I(IBP_PTAT_TUNE_1P5U_VCO, VSS_VCO)" in txt
    assert "I(IBP_POLY_500N_VCO_Fit, VSS_PLL)" in txt
    ok, problems = D.va_sanity(txt, "AVDD1P0",
                               ["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"],
                               ["IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit",
                                "IBP_PTAT_TUNE_1P5U_VCO"],
                               ["VSS_PLL", "VSS_VCO"])
    assert ok, problems


def test_split_ground_default_is_byte_identical(tmp_path):
    """No map (or every port mapped to the SAME net) collapses to the single-ground interface
    byte-for-byte -- the split feature never perturbs the established default emission."""
    res = _real_pmu_fit_result()
    base = D.emit_pmu_va(res, "PMU_m", tmp_path / "a.va", supply="AVDD1P0", ground="VSS").read_text()
    none = D.emit_pmu_va(res, "PMU_m", tmp_path / "b.va", supply="AVDD1P0", ground="VSS",
                         port_grounds=None).read_text()
    v_outs, i_outs = D.model_output_ports(res)
    same = D.emit_pmu_va(res, "PMU_m", tmp_path / "c.va", supply="AVDD1P0", ground="VSS",
                         port_grounds={p: "VSS" for p in v_outs + i_outs}).read_text()
    assert base == none == same
    assert (v_outs, i_outs) == (["VDD0P8_DIG", "VDD0P8_PLL", "VDD0P8_VCO"],
                                ["IBP_POLY_1P8U_VCO", "IBP_POLY_500N_VCO_Fit",
                                 "IBP_PTAT_TUNE_1P5U_VCO"])


def test_per_port_operating_point_params(tmp_path):
    """Each voltage rail's regulated output (vreg) and each large-signal bias's DC current
    (idc55) are MODULE PARAMETERS (editable CDF fields, default = fitted), declared exactly
    ONCE (no double-assign in initial_step) and still referenced by the body."""
    vf = _vfit(vreg=0.8)
    vf["pin"] = "VDD0P8_PLL"
    ls = dict(sink="i1p5u", pin="IBP_PTAT_1P5U", pol="sink", idc55=1.502e-6, didt=1e-9,
              g0=3e-10, vc=0.667, gdd=2e-9, vknee=0.05, knee_p=1.0, Cp=16e-15,
              in_white=0.0, in_kf=0.0, tnom_c=25.0)
    res = dict(voltage={"pll": vf}, current=[ls],
               meta=dict(name="x", loads=["nom"], supplies=["AVDD1P0"]))
    txt = D.emit_pmu_va(res, "PMU_x", tmp_path / "x.va", supply="AVDD1P0", ground="VSS").read_text()
    # declared as parameters, exactly once each (the promotion, not a 2nd initial_step assign)
    assert "localparam real VDD0P8_PLL_vreg = 8.000000e-01;" in txt
    assert "localparam real IBP_PTAT_1P5U_idc55 = 1.502000e-06;" in txt
    assert txt.count("VDD0P8_PLL_vreg =") == 1, "vreg must not be re-assigned in initial_step"
    assert txt.count("IBP_PTAT_1P5U_idc55 =") == 1, "idc55 must not be re-assigned in initial_step"
    # still used by the model body (regulated-output reference + Idc(T) temp law)
    assert "V(VDD0P8_PLL_vrg, VSS) <+ VDD0P8_PLL_vreg;" in txt
    assert "IBP_PTAT_1P5U_idc55 + IBP_PTAT_1P5U_didt" in txt
    # internal constants are localparam (NOT instance/CDF parameters): vdc reference, the noise
    # resistor NRk, and the noise-corner caps -> they no longer clutter the Cadence Q/CDF list.
    assert "localparam real vdc_AVDD1P0 =" in txt and "parameter real vdc_AVDD1P0" not in txt
    assert "localparam real NRk =" in txt and "parameter real NRk" not in txt
    assert "localparam real VDD0P8_PLL_Cn1 =" in txt   # noise-corner caps are constants too


def test_cft_feedthrough_emitted_when_present(tmp_path):
    """A rail carrying a gated vin->vout feedthrough cap (cft>0, the value fit_cft produced
    and the PMU path used to DROP) emits an editable `<rail>_Cft` parameter + a supply->rail
    ddt feedthrough contribution -- mirroring the old single-port emit_va. va_sanity passes."""
    vf = _vfit(vreg=0.8)
    vf["pin"] = "VDD0P8_PLL"
    vf["cft"] = 1.74e-13                  # ~174 fF, the Target-B feedthrough magnitude
    res = dict(voltage={"pll": vf}, current=[],
               meta=dict(name="x", loads=["nom"], supplies=["AVDD1P0"]))
    txt = D.emit_pmu_va(res, "PMU_cft", tmp_path / "c.va", supply="AVDD1P0", ground="VSS").read_text()
    assert "localparam real VDD0P8_PLL_Cft = 1.740000e-13;" in txt
    assert "I(AVDD1P0, VDD0P8_PLL) <+ VDD0P8_PLL_Cft*ddt(V(AVDD1P0, VDD0P8_PLL));" in txt
    ok, problems = D.va_sanity(txt, "AVDD1P0", ["VDD0P8_PLL"], [], ["VSS"])
    assert ok, problems


def test_cft_absent_is_byte_identical(tmp_path):
    """cft absent OR cft<=0 -> emission byte-for-byte equal to the pre-feedthrough .va (no
    `_Cft` param, no feedthrough contribution). Locks the default-inert guarantee on BOTH the
    literal (single-OP) and the scheduled (multi-load) voltage paths."""
    # literal path
    vf = _vfit(vreg=0.8); vf["pin"] = "VDD0P8_PLL"
    res_a = dict(voltage={"pll": vf}, current=[],
                 meta=dict(name="x", loads=["nom"], supplies=["AVDD1P0"]))
    base = D.emit_pmu_va(res_a, "PMU_b", tmp_path / "a.va", supply="AVDD1P0", ground="VSS").read_text()
    vf0 = _vfit(vreg=0.8); vf0["pin"] = "VDD0P8_PLL"; vf0["cft"] = 0.0
    res_b = dict(voltage={"pll": vf0}, current=[],
                 meta=dict(name="x", loads=["nom"], supplies=["AVDD1P0"]))
    zero = D.emit_pmu_va(res_b, "PMU_b", tmp_path / "b.va", supply="AVDD1P0", ground="VSS").read_text()
    assert base == zero
    assert "_Cft" not in base
    # scheduled path (multi-load): cft absent vs cft=0.0 also byte-identical
    sf, slabels = _multiload_vfit([50e-6, 580e-6, 2000e-6]); sf["pin"] = "VDD0P8_PLL"
    sres = dict(voltage={"pll": sf}, current=[],
                meta=dict(name="x", loads=slabels, supplies=["AVDD1P0"]))
    sbase = D.emit_pmu_va(sres, "PMU_s", tmp_path / "sa.va", supply="AVDD1P0", ground="VSS").read_text()
    sf0, _ = _multiload_vfit([50e-6, 580e-6, 2000e-6]); sf0["pin"] = "VDD0P8_PLL"; sf0["cft"] = 0.0
    sres0 = dict(voltage={"pll": sf0}, current=[],
                 meta=dict(name="x", loads=slabels, supplies=["AVDD1P0"]))
    szero = D.emit_pmu_va(sres0, "PMU_s", tmp_path / "sb.va", supply="AVDD1P0", ground="VSS").read_text()
    assert sbase == szero
    assert "_Cft" not in sbase


def test_vreg_schedule_emitted_from_transient(tmp_path):
    """A rail carrying a transient-derived DC load-reg schedule (vreg_sched) emits vreg as a
    load-scheduled real var -- `parameter real iload_<rail>` + a clamped ln(iload) poly --
    NOT a baked parameter. At each schedule current the poly reproduces the fitted vreg point
    (corner-exact to 6 sig figs). va_sanity passes."""
    cur = [1e-4, 2e-3, 4e-3]                          # 3 pts -> deg-2 poly INTERPOLATES (corner-exact)
    vrg = [0.8000, 0.8020, 0.8040]                    # rising vreg(iload) load-reg curve
    vf = _vfit(vreg=0.8); vf["pin"] = "VDD0P8_PLL"
    vf["vreg_sched"] = dict(currents=cur, vregs=vrg, i_nom=1e-4)
    res = dict(voltage={"pll": vf}, current=[],
               meta=dict(name="x", loads=["nom"], supplies=["AVDD1P0"]))
    txt = D.emit_pmu_va(res, "PMU_vsch", tmp_path / "v.va", supply="AVDD1P0", ground="VSS").read_text()
    # vreg is now an iload-driven schedule, no longer a baked parameter
    assert "parameter real iload_VDD0P8_PLL = 1.000000e-04;" in txt
    assert "VDD0P8_PLL_u = ln(min(max(iload_VDD0P8_PLL," in txt
    assert "VDD0P8_PLL_vreg = min(max(" in txt
    assert "parameter real VDD0P8_PLL_vreg" not in txt   # not a baked param anymore
    # corner-exact: the emitted poly (same _sched_expr the body uses) reproduces each point
    expr = D._sched_expr(cur, vrg, "u", False)
    for i, v in zip(cur, vrg):
        got = _eval_va_expr(expr, np.log(i))
        assert abs(got - v) <= 1e-5, f"vreg sched off at {i}: {got} vs {v}"
    ok, problems = D.va_sanity(txt, "AVDD1P0", ["VDD0P8_PLL"], [], ["VSS"])
    assert ok, problems


def test_vreg_schedule_absent_is_byte_identical(tmp_path):
    """vreg_sched absent or None -> emission byte-for-byte equal to the baked-vreg .va (vreg
    stays a per-rail parameter, no iload_<rail> / no ln() schedule). Locks the default-inert
    guarantee for the DC load-reg feature."""
    vf = _vfit(vreg=0.8); vf["pin"] = "VDD0P8_PLL"
    res_a = dict(voltage={"pll": vf}, current=[],
                 meta=dict(name="x", loads=["nom"], supplies=["AVDD1P0"]))
    base = D.emit_pmu_va(res_a, "PMU_n", tmp_path / "a.va", supply="AVDD1P0", ground="VSS").read_text()
    vf0 = _vfit(vreg=0.8); vf0["pin"] = "VDD0P8_PLL"; vf0["vreg_sched"] = None
    res_b = dict(voltage={"pll": vf0}, current=[],
                 meta=dict(name="x", loads=["nom"], supplies=["AVDD1P0"]))
    none = D.emit_pmu_va(res_b, "PMU_n", tmp_path / "b.va", supply="AVDD1P0", ground="VSS").read_text()
    assert base == none
    assert "localparam real VDD0P8_PLL_vreg = 8.000000e-01;" in base
    assert "iload_VDD0P8_PLL" not in base


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
