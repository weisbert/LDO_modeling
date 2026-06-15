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


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
