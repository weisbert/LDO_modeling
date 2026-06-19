"""STAGE 2a -- the anti-footgun + provenance-banner tests.

These pin the locked design from HANDOFF_MODELING_COVERAGE sections 0/4:
  * the synthetic FLAT dc_loadreg/dc_dropout/dc_linereg fabrication in
    fit_multiport.export_single_port_refs is GONE (nothing fabricated);
  * a real dropout sweep (dc_<o>=[Iload,Vout]) is carried through as REAL data;
  * fit_model.emit / emit_va GRACEFULLY SKIP a missing DC sweep (no KeyError, no
    fabricated dropout, a 'no DC sweep' header note instead);
  * emit_pmu_va stamps a COVERAGE/OP/VALID_LOAD provenance banner sourced from
    fit_result['meta'], and the .va still passes va_sanity.

No simulator: pure-Python producer/consumer + static .va inspection.
"""
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "cadence")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fit_model as FM            # noqa: E402
import fit_multiport as FMP       # noqa: E402
import emit_pmu_model as D        # noqa: E402


class _Npz(dict):
    """A dict that answers `k in obj.files` -- the shape fit_model probes (ref.files)."""
    @property
    def files(self):
        return list(self.keys())


# --------------------------------------------------------------- single-port ref builders
def _small_signal_ref(nom="500u", vout_dc=0.8):
    """A minimal SINGLE-port ref dict with z/p/noise at one corner and NO DC arrays --
    exactly what export_single_port_refs writes for a small-signal-only (T0) extraction."""
    f = np.logspace(1, 8, 40)
    z = np.c_[f, 0.05 + 0 * f, 1e-9 * f]          # benign Zout [f, re, im]
    p = np.c_[f, 1e-3 + 0 * f, 0 * f]             # benign PSRR
    n = np.c_[f, 1e-9 + 0 * f]                    # benign noise Sv
    return _Npz({"loads": np.array([nom]),
                 f"z_{nom}": z, f"p_{nom}": p, f"noise_{nom}": n,
                 "meta_vout_dc": np.array(vout_dc),
                 "meta_port": np.array("pll")})


def _with_real_dc(ref, nom="500u"):
    """Add a REAL (monotone-droop) dc_loadreg/dc_dropout to a single-port ref."""
    iload = np.array([1e-6, 1e-4, 5e-4, 1e-3, 2e-3])
    vout = 0.8 - 30.0 * iload                      # real droop (Rout~30 ohm)
    dc = np.c_[iload, vout]
    ref = _Npz(dict(ref))
    ref["dc_loadreg"] = dc
    ref["dc_dropout"] = dc
    return ref


# ------------------------------------------------------- multi-port npz builder (producer)
def _multiport_npz(tmp_path, with_dc=False):
    """Write a 1-rail multi-port npz + return (path, manifest). The rail role key is 'pll'
    (matches the stand-in); when with_dc, a real dropout sweep dc_pll=[Iload,Vout] lands in
    the FULL ref (split_ports does not carry it -> export reads it off the full ref)."""
    nom = "500u"
    f = np.logspace(1, 8, 40)
    z = np.c_[f, 0.05 + 0 * f, 1e-9 * f]
    p = np.c_[f, 1e-3 + 0 * f, 0 * f]
    n = np.c_[f, 1e-9 + 0 * f]
    rec = {"loads": np.array([nom]),
           "z_pll_500u": z, "p_pll_AVDD1P0_500u": p, "noise_pll_500u": n}
    if with_dc:
        iload = np.array([1e-6, 1e-4, 5e-4, 1e-3, 2e-3])
        rec["dc_pll"] = np.c_[iload, 0.8 - 30.0 * iload]
    npz = tmp_path / "mp.npz"
    np.savez(npz, **rec)
    m = {"name": "mp", "supplies": {"AVDD1P0": {"dc": 1.0}},
         "current_psrr_supplies": ["AVDD1P0"],
         "v_out": {"pll": {"iload": 500e-6, "vout_dc": 0.8}},
         "i_out": {}}
    return npz, m


# ============================================================ A) footgun removed
def test_export_smallsignal_has_no_dc_keys(tmp_path):
    """A small-signal npz (no dc_<o>) -> the per-output ref carries NO dc_loadreg /
    dc_dropout / dc_linereg key. The footgun is GONE; nothing is fabricated."""
    npz, m = _multiport_npz(tmp_path, with_dc=False)
    refs = FMP.export_single_port_refs(npz, m, outdir=tmp_path / "ref")
    assert set(refs) == {"pll"}
    d = np.load(refs["pll"], allow_pickle=True)
    for k in ("dc_loadreg", "dc_dropout", "dc_linereg"):
        assert k not in d.files, f"{k} must be ABSENT (no fabrication), got {d.files}"


def test_export_with_real_dc_carries_real_curve(tmp_path):
    """A npz WITH a real dc_<o>=[Iload,Vout] -> the per-output ref carries dc_loadreg AND
    dc_dropout EQUAL to the REAL array (never a flat fabricated curve)."""
    npz, m = _multiport_npz(tmp_path, with_dc=True)
    refs = FMP.export_single_port_refs(npz, m, outdir=tmp_path / "ref")
    d = np.load(refs["pll"], allow_pickle=True)
    assert "dc_loadreg" in d.files and "dc_dropout" in d.files
    real = np.load(npz, allow_pickle=True)["dc_pll"]
    assert np.allclose(d["dc_loadreg"], real)
    assert np.allclose(d["dc_dropout"], real)
    # it is REAL (a real droop), not a flat fabrication: Vout is not constant
    assert np.ptp(d["dc_loadreg"][:, 1]) > 1e-6, "carried DC must be the real (drooping) curve"
    # dc_linereg: no in-situ line-reg sweep -> still omitted (never fabricated)
    assert "dc_linereg" not in d.files


def test_no_fabricated_flat_dc_literal_in_source():
    """Grep-style guard: the fabricated flat-DC np.array literals are GONE from the
    fit_multiport source (the exact footgun shape was `np.array([[...],[...],[...]])`)."""
    src = (ROOT / "harness" / "fit_multiport.py").read_text()
    assert "np.array([[" not in src, "a fabricated flat-DC np.array([[...]]) literal remains"
    # and the specific fabricated keys are no longer assigned a synthetic literal
    assert 'vdc - 1e-3' not in src and 'vdc - 2e-3' not in src, "synthetic dropout literal remains"


# ============================================================ B) graceful skip in emit/emit_va
def _fit_single_port(ref):
    """Drive fit_model's per-corner fitters over a single-port ref (no DC needed) and
    return P. Mirrors how export+fit feed the GUI/emit path."""
    FM.ref = ref
    FM.LOADS = [str(x) for x in ref["loads"]]
    FM.NOMINAL = FM.LOADS[len(FM.LOADS) // 2]
    FM.CFT = 0.0
    FM.C, FM.RC = FM.fit_cout_esr()
    return FM.fit_all()


def test_emit_lib_skips_missing_dc(tmp_path):
    """emit() on a ref WITHOUT dc_loadreg/dropout: no KeyError, no PWL dropout table, and
    the 'no DC sweep' header NOTE is stamped. The slew_en ternary is gone (linear branch)."""
    ref = _small_signal_ref()
    P = _fit_single_port(ref)
    lib = tmp_path / "ss.lib"
    FM.emit(P, lib)                                  # must NOT raise
    txt = lib.read_text()
    assert "pwl(V(" not in txt, "no fabricated dropout PWL table may be emitted"
    assert "slew_en > 0.5" not in txt, "no slew_en dropout ternary without real DC"
    assert "NOT modeled" in txt and "small-signal scope" in txt, "missing the no-DC NOTE"
    # branch A is the pure linear R_a contribution
    assert "I = {V(vreg,nA)/R_a}" in txt or "I = {V(vrgn,nA)/R_a}" in txt


def test_emit_va_skips_missing_dc(tmp_path):
    """emit_va() on a ref WITHOUT DC: valid output, NO nonlinear-branch dropout / PWL
    expression, the 'no DC sweep' header note, and the slew_en if/else collapsed."""
    ref = _small_signal_ref()
    P = _fit_single_port(ref)
    va = tmp_path / "ss.va"
    tbl = tmp_path / "ss.tbl"
    FM.emit_va(P, va, tbl)                            # must NOT raise
    txt = va.read_text()
    assert "if (slew_en == 0)" not in txt, "no slew_en branch-swap without real DC"
    assert "vdrp =" not in txt and "max(vdrp" not in txt, "no dropout PWL expression"
    assert "NOT modeled" in txt and "small-signal scope" in txt, "missing the no-DC NOTE"
    # the linear branch-A contribution is still present
    assert "V(nA, vrg) <+ R_a*I(nA, vrg)" in txt
    # structural sanity: balanced module/endmodule + begin/end
    import re
    assert txt.count("endmodule") == 1 and "module ldo_model(" in txt
    assert len(re.findall(r"\bbegin\b", txt)) == len(re.findall(r"\bend\b", txt))
    # the .tbl records the absence rather than a fabricated curve
    assert "no DC sweep" in tbl.read_text()


def test_emit_va_with_dc_additive_slew(tmp_path):
    """GUARDRAIL-1: WITH real DC, slew_en is now ADDITIVE (no branch swap). The linear R_a
    conductance branch is ALWAYS active and slew_en scales a nonlinear correction (the
    dropout PWL minus its OP tangent). slew_en=0 == the pure linear core; the correction is
    0 (value AND slope) at the OP so the at-OP small-signal Zout is identical for slew_en in
    {0,1}; slew_en=1 reproduces the dropout deviation at the swept points."""
    ref = _with_real_dc(_small_signal_ref())
    P = _fit_single_port(ref)
    va = tmp_path / "dc.va"
    FM.emit_va(P, va, tmp_path / "dc.tbl")
    txt = va.read_text()
    # ADDITIVE form: no branch swap, linear core always active, slew_en-scaled correction
    assert "if (slew_en == 0)" not in txt, "branch swap must be gone (additive restructure)"
    assert "I(vrg, nA) <+ V(vrg, nA)/R_a;" in txt, "linear R_a core must be ALWAYS active"
    assert "I(vrg, nA) <+ slew_en * (" in txt, "additive slew_en-scaled correction missing"
    assert "max(vdrp" in txt and "vdrp =" in txt, "the dropout PWL must still be inlined"
    assert "NOT modeled" not in txt

    # numeric: the additive correction is 0 (value AND slope) at the OP, and reproduces the
    # (PWL - OP tangent) deviation at every swept knot. Evaluate the EMITTED expression.
    import re
    vreg121 = P[FM.NOMINAL]["vreg"]
    vd, il = FM.build_pwl_arrays(vreg121)
    i_op = FM._amps(FM.NOMINAL)
    vdrp_op, i_pwl_op, g_op = FM._pwl_op_tangent(vd, il, i_op)
    expr = FM._pwl_additive_va_expr(vd, il, i_op, var="vdrp")
    pyexpr = re.sub(r"\s+", " ", expr).replace("max", "np.maximum")

    def ev(vdrp):
        return eval(pyexpr, {"np": np, "vdrp": vdrp})   # noqa: S307 (trusted, generated)

    assert abs(ev(vdrp_op)) < 1e-9, "correction value at OP must be 0"
    h = 1e-9
    slope = (ev(vdrp_op + h) - ev(vdrp_op - h)) / (2 * h)
    assert abs(slope) < 1e-5, "correction SLOPE at OP must be 0 (Zout unchanged)"
    for v in vd:                      # at every swept knot: correction == PWL - OP tangent
        expect = float(np.interp(v, vd, il)) - (i_pwl_op + g_op * (v - vdrp_op))
        assert abs(ev(v) - expect) < 1e-9 * max(1.0, abs(expect))


def test_emit_lib_with_dc_additive_slew(tmp_path):
    """GUARDRAIL-1, ngspice side: emit() with real DC emits the SAME additive restructure --
    the Bra B-source is `V(rail,nA)/R_a + slew_en * (pwl(...) - OP tangent)` (no `? :` swap),
    so slew_en=0 is the linear core byte-for-byte and the at-OP conductance is unchanged."""
    ref = _with_real_dc(_small_signal_ref())
    P = _fit_single_port(ref)
    lib = tmp_path / "dc.lib"
    FM.emit(P, lib)
    txt = lib.read_text()
    assert "slew_en > 0.5" not in txt, "ternary branch swap must be gone (additive)"
    assert "/R_a + slew_en * (pwl(" in txt, "additive linear-core + slew_en*correction missing"
    assert "NOT modeled" not in txt


# ============================================================ C+D) provenance banner
def _banner_fit_result(tier="T2", op_iload=5e-4, op_temp=55.0, valid_load=(5e-5, 6e-3)):
    """A minimal emit_pmu_va fit_result carrying the provenance fields on meta (what
    fit_multiport now sets)."""
    G = [1e-3, -5e-4, 2e5, 2e-4, 1e6, -1e-4, 5e6]
    Q = (1e-3, 1e-9, 3e6, 2.0)
    p = dict(iv=op_iload, R_a=0.05, L_a=1e-7, R_pl=1e3, R_b=1e9, L_b=1e-12,
             G0=G[0], G1=G[1], w1=G[2], G2=G[3], w2=G[4], G3=G[5], w3=G[6],
             pcb0=Q[0], pcb1=Q[1], pcw0=Q[2], pcq=Q[3], gnw=1e-9, vreg=0.8,
             _psrr={"AVDD1P0": (G, Q)})
    nfk = list(np.logspace(2, 6, 4))
    for k in range(4):
        p[f"gn{k+1}"] = 1e-9 * (k + 1)
    vfit = dict(P={"nom": p}, nfk=nfk, cout=2e-8, esr=0.12, err=[], supplies=["AVDD1P0"],
                pin="VDD0P8_PLL")
    return dict(voltage={"pll": vfit}, current=[],
                meta=dict(name="prov", loads=["nom"], supplies=["AVDD1P0"],
                          coverage_tier=tier, op_iload=op_iload, op_temp=op_temp,
                          valid_load=valid_load))


def test_banner_from_meta(tmp_path):
    """The emitted .va header carries the COVERAGE= banner sourced from fit_result['meta'];
    va_sanity still passes (the banner is a comment)."""
    res = _banner_fit_result()
    va = tmp_path / "prov.va"
    D.emit_pmu_va(res, "PMU_prov", va, supply="AVDD1P0", ground="VSS")
    txt = va.read_text()
    assert "// COVERAGE=T2" in txt
    assert "OP=5e-04@55" in txt or "OP=0.0005@55" in txt, [l for l in txt.splitlines()
                                                            if "COVERAGE=" in l]
    assert "VALID_LOAD=[5e-05..0.006]" in txt
    ok, problems = D.va_sanity(txt, "AVDD1P0", ["VDD0P8_PLL"], [], "VSS")
    assert ok, problems


def test_banner_explicit_provenance_overrides_meta(tmp_path):
    """An explicit provenance dict OVERRIDES meta for the banner fields."""
    res = _banner_fit_result(tier="T0")
    va = tmp_path / "prov2.va"
    prov = dict(tier="T4", op_iload=2e-3, op_temp=125.0, valid_load=(2e-4, 6e-3))
    D.emit_pmu_va(res, "PMU_prov2", va, supply="AVDD1P0", ground="VSS", provenance=prov)
    txt = va.read_text()
    assert "// COVERAGE=T4" in txt and "T0" not in txt.split("\n")[
        next(i for i, l in enumerate(txt.splitlines()) if "COVERAGE=" in l)]
    assert "VALID_LOAD=[0.0002..0.006]" in txt


def test_banner_default_when_unspecified(tmp_path):
    """No provenance + bare meta -> a clear 'unspecified' banner (never crashes)."""
    res = _banner_fit_result()
    res["meta"] = dict(name="bare", loads=["nom"], supplies=["AVDD1P0"])  # strip provenance
    va = tmp_path / "prov3.va"
    D.emit_pmu_va(res, "PMU_bare", va, supply="AVDD1P0", ground="VSS")
    txt = va.read_text()
    assert "// COVERAGE=unspecified" in txt and "VALID_LOAD=?" in txt


def test_fit_multiport_meta_carries_provenance(tmp_path):
    """The REAL producer: fit_multiport meta carries coverage_tier/valid_load/op_iload/
    op_temp (defensively None when the manifest declares none)."""
    npz, m = _multiport_npz(tmp_path, with_dc=False)
    res = FMP.fit_multiport(str(npz), m)
    for k in ("coverage_tier", "valid_load", "op_iload", "op_temp"):
        assert k in res["meta"], f"meta missing provenance field {k}"


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
