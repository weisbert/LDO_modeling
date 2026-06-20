"""NGSPICE-GATED regression for this session's GUARDRAIL-1 (additive slew_en).

Locks the ADDITIVE-slew_en restructure (HANDOFF coverage GUARDRAIL-1) by RUNNING
the real ngspice on the REAL emitted behavioral model -- not just inspecting the
emitted text (that is test_antifootgun.py's job). Two facts are pinned in-circuit:

  1a) the at-OP small-signal Zout is IDENTICAL for slew_en in {0,1}. The additive
      nonlinear correction is 0 in VALUE *and* SLOPE at the operating point, so the
      branch-A conductance the AC solver sees is the pure linear 1/R_a either way.
      We inject a 1 A AC current into vout and read |Zout(f->0)| = |V(vout)| for
      slew_en=0 and slew_en=1; they must match to a tight relative tolerance.

  1b) the dropout/current-limit deviation appears ONLY with slew_en=1, and ONLY at
      high load. slew_en=0 stays the pure linear R_a extrapolation; slew_en=1 adds
      the genuine characterized dropout, so at a high load (well above the OP, toward
      the capless-rail collapse the prior validation saw) V(vout) for slew_en=1 sits
      MEANINGFULLY below the slew_en=0 linear value.

Engine: ngspice (harness/ng.py -> ng.NGSPICE, honors $NGSPICE). The skip guard probes
ng.NGSPICE --version; because that honors $NGSPICE,
    NGSPICE=/nonexistent python3 -m pytest harness/test_coverage_ngspice.py -q
reports this module SKIPPED (not passed) -- the skip path is testable.
"""
import pathlib
import subprocess
import sys

import numpy as np
import pytest

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
for _p in (str(HERE), str(ROOT / "cadence")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import ng                  # noqa: E402  (ngspice driver; ng.NGSPICE honors $NGSPICE)
import fit_model as FM     # noqa: E402

VARIANT = "v2_capless"     # has a REAL DC dropout sweep (dc_loadreg/dc_dropout to ~6 mA)
HIGH_LOAD = 6e-3           # well above the OP -> capless-rail dropout regime; ngspice converges


def _have_ngspice():
    """True iff ng.NGSPICE (which honors $NGSPICE) runs and reports a version. Probing the
    SAME binary the deck runner uses makes the skip path testable via NGSPICE=/nonexistent."""
    try:
        r = subprocess.run([ng.NGSPICE, "--version"], capture_output=True, timeout=20)
        return r.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _have_ngspice(), reason="ngspice not available")


# --------------------------------------------------------------------- deck builders
def _xline(op_amps, slew_en, vdd):
    """DUT instance line. The emitted model is a 3-port subckt (vin vout gnd); tie its
    explicit ground to node 0 (same convention as harness/bench.xline)."""
    return (f"Xdut vin vout 0 ldo_model "
            f"iload={op_amps:.6e} slew_en={slew_en} vdd={vdd:g}")


def _zout_deck(op_amps, slew_en, vdd):
    """GUARDRAIL-1a deck: 1 A AC current into vout at the OP -> Zout(f)=V(vout)/1.
    DC supply at vdd, the load isource set to the OP current (AC 0 so it is a pure DC
    operating-point load). Low-start AC so f->0 gives the LF Zout floor (=R_a)."""
    return f"""* GUARDRAIL-1a Zout @OP (slew_en={slew_en})
{_xline(op_amps, slew_en, vdd)}
Vin vin 0 DC {vdd:g}
Iload vout 0 DC {op_amps:.6e} AC 0
Iac 0 vout AC 1
.control
set wr_singlescale
ac dec 8 1 1k
wrdata out.dat vr(vout) vi(vout)
quit
.endc
.end
"""


def _op_deck(iload, slew_en, vdd):
    """GUARDRAIL-1b deck: a DC operating point at a chosen load -> V(vout)."""
    return f"""* GUARDRAIL-1b op @iload={iload:g} (slew_en={slew_en})
{_xline(iload, slew_en, vdd)}
Vin vin 0 DC {vdd:g}
Iload vout 0 DC {iload:.6e}
.control
op
wrdata out.dat v(vout)
quit
.endc
.end
"""


def _run_zout(lib, tmp_path, slew_en, op_amps, vdd):
    """Run the Zout AC deck -> (f, |Zout|) arrays. |Zout| = |V(vout)| for a 1 A drive."""
    deck = ng.assemble(_zout_deck(op_amps, slew_en, vdd), libs=[lib])
    r = ng.run(deck, tmp_path / f"z{slew_en}", outputs=["out.dat"])
    assert r["out.dat"] is not None, f"ngspice Zout run failed (rc={r['_rc']}):\n{r['_stderr'][-1500:]}"
    f, z = ng.complex_col(r["out.dat"][1])
    return f, np.abs(z)


def _run_op(lib, tmp_path, slew_en, iload, vdd):
    """Run the .op deck -> V(vout) (float). wrdata writes [scale, v(vout)]; take the value."""
    deck = ng.assemble(_op_deck(iload, slew_en, vdd), libs=[lib])
    r = ng.run(deck, tmp_path / f"o{slew_en}_{iload:g}", outputs=["out.dat"])
    assert r["out.dat"] is not None, f"ngspice op run failed (rc={r['_rc']}):\n{r['_stderr'][-1500:]}"
    return float(r["out.dat"][1].flatten()[-1])


# --------------------------------------------------------------------- fixture: fit + emit once
@pytest.fixture(scope="module")
def model(tmp_path_factory):
    """Fit v2_capless and emit the REAL behavioral ldo_model.lib once for the module.
    Returns (lib_path, op_amps, vdd) read from the fit -- the SAME OP the emitted
    '.subckt ldo_model ... iload=<NOM> ... vdd=<VREF>' default line carries."""
    tmp = tmp_path_factory.mktemp("cov_ng")
    res = FM.fit_variant(VARIANT)
    lib = tmp / "ldo_model.lib"
    FM.emit(res.P, lib)                       # emit the additive-slew_en model (must have real DC)
    txt = lib.read_text()
    # this guardrail is meaningless without the additive restructure actually emitted
    assert "/R_a + slew_en * (pwl(" in txt, "emitted model is not the additive-slew_en form"
    assert "slew_en > 0.5" not in txt, "a ternary branch-swap leaked into the emitted model"
    op_amps = FM._amps(res.nominal)           # OP load current (= the subckt iload default)
    vdd = float(res.vref)                      # supply DC ref (= the subckt vdd default)
    # cross-check against the emitted '.subckt ldo_model ... iload=<NOM> ... vdd=<VREF>' line
    sub = next(l for l in txt.splitlines() if l.startswith(".subckt ldo_model"))
    assert f"iload={res.nominal}" in sub and f"vdd={vdd:g}" in sub, sub
    return lib, op_amps, vdd, tmp


# ============================================================ GUARDRAIL-1a
def test_guardrail1a_at_op_zout_identical(model):
    """At the OP, |Zout(f->0)| is EQUAL for slew_en in {0,1}: the additive correction has
    0 value AND 0 slope at the OP, so the small-signal branch-A conductance is the pure
    linear 1/R_a regardless of slew_en. Run real ngspice AC once per slew_en."""
    lib, op_amps, vdd, tmp = model
    f0, z0 = _run_zout(lib, tmp, 0, op_amps, vdd)
    f1, z1 = _run_zout(lib, tmp, 1, op_amps, vdd)
    zout0_lf, zout1_lf = float(z0[0]), float(z1[0])     # f->0 floor
    # the additive correction must NOT perturb the at-OP small-signal Zout
    rel = abs(zout0_lf - zout1_lf) / max(zout0_lf, 1e-30)
    assert rel < 1e-3, (f"at-OP Zout differs with slew_en: {zout0_lf} vs {zout1_lf} "
                        f"(rel {rel:.2e}) -- the correction is not 0-slope at the OP")
    # sanity: a real, finite LF output impedance (the R_a floor), not a degenerate ~0/inf
    assert 1.0 < zout0_lf < 1e4, f"implausible LF Zout {zout0_lf}"
    # expose the numbers for the structured evidence
    print(f"GUARDRAIL-1a Zout(f={f0[0]:.3g}Hz): slew0={zout0_lf:.6f} slew1={zout1_lf:.6f} rel={rel:.2e}")


# ============================================================ GUARDRAIL-1b
def test_guardrail1b_dropout_only_with_slew(model):
    """At a high load (well above the OP), slew_en=0 stays the pure linear R_a
    extrapolation while slew_en=1 adds the genuine characterized dropout, so its V(vout)
    sits MEANINGFULLY below. Run real ngspice .op once per slew_en at HIGH_LOAD."""
    lib, op_amps, vdd, tmp = model
    vout_lin = _run_op(lib, tmp, 0, HIGH_LOAD, vdd)     # slew_en=0: pure-linear R_a value
    vout_drp = _run_op(lib, tmp, 1, HIGH_LOAD, vdd)     # slew_en=1: linear + real dropout
    # the dropout pulls Vout DOWN, and unambiguously so (>>numerical noise)
    assert vout_drp < vout_lin - 0.05, (
        f"slew_en=1 must drop meaningfully below the slew_en=0 linear value at {HIGH_LOAD} A: "
        f"linear={vout_lin:.6f} dropout={vout_drp:.6f}")
    # slew_en=0 must be the LINEAR extrapolation (R_a slope), i.e. above where dropout pulls it
    assert vout_lin > vout_drp, "slew_en=0 must be the un-dropped linear value"
    # both rails are physical (no NaN / nonsense) and below the regulated nominal
    assert 0.0 < vout_drp < vout_lin < 1.0, (vout_lin, vout_drp)
    print(f"GUARDRAIL-1b Vout @ {HIGH_LOAD} A: slew0(linear)={vout_lin:.6f} "
          f"slew1(dropout)={vout_drp:.6f}  delta={vout_lin - vout_drp:.6f}")


def test_guardrail1b_slew0_is_pure_linear_Ra(model):
    """Corroborate that slew_en=0 IS the pure-linear R_a extrapolation: V(vout) vs load is
    a straight line of slope R_a (the clamped corner value) -- no dropout curvature. Two
    real .op runs at two high loads, slope == the model's R_a within tight tolerance."""
    lib, op_amps, vdd, tmp = model
    il_a, il_b = 1e-3, HIGH_LOAD
    v_a = _run_op(lib, tmp, 0, il_a, vdd)
    v_b = _run_op(lib, tmp, 0, il_b, vdd)
    slope = (v_a - v_b) / (il_b - il_a)                 # dV/dI = effective R_a
    # R_a is clamped to the corner envelope; the high-load value pins to the top corner R_a
    res = FM.fit_variant(VARIANT)
    ra_top = float(res.P[res.loads[-1]]["R_a"])
    assert abs(slope - ra_top) / ra_top < 0.05, (
        f"slew_en=0 slope {slope:.3f} ohm != pure-linear R_a {ra_top:.3f} ohm -- not linear")
    print(f"slew_en=0 dV/dI = {slope:.4f} ohm  (model R_a={ra_top:.4f} ohm)")


# ============================================================ R4 held-out off-corner-LOAD noise
class _Ref:
    """Dict-backed stand-in for the np.load ref object (.files + __getitem__)."""
    def __init__(self, d):
        self._d = d
        self.files = list(d)

    def __getitem__(self, k):
        return self._d[k]


def test_offgrid_noise_gate_end_to_end(model):
    """END-TO-END (real ngspice): the held-out off-corner-load noise gate measures the EMITTED
    model's INTERPOLATED noise at an off-corner current (the iload param driving the quad-in-ln
    interpolation) and grades it vs a REAL GT off-corner spectrum (transistor ground truth). This
    covers what the pure-logic monkeypatched tests cannot: the interpolation actually running and the
    fres=None Sv-peak fallback on a real spectrum."""
    import bench
    import score
    import variants
    lib, op_amps, vdd, tmp = model
    il = bench.OFFGRID_NOISE_LOADS[0]                       # 49u: strictly interior, iload-clamp inactive
    v = variants.VARIANTS[VARIANT]
    fg, Sg = bench.measure_noise(v["libs"], v["subckt"], il, xparams=v.get("xparams", ""))   # real GT
    ref = _Ref({f"noise_offgrid_{il}": np.c_[fg, Sg]})
    m = score._offgrid_noise_metrics(ref, str(lib), "ldo_model", "")
    assert m is not None and len(m["rows"]) == 1 and m["rows"][0]["il"] == il
    assert np.isfinite(m["worst_db"]) and m["worst_db"] >= 0
    # the interpolated off-corner noise tracks GT (observed <0.5dB across all variants); a gross
    # interpolation blow-up would trip this loose bound
    assert m["worst_db"] < 3.0, f"off-corner noise interp psd_rms {m['worst_db']:.2f}dB too high"
    print(f"R4 off-corner {il}: held-out noise psd_rms={m['worst_db']:.3f}dB (model interpolated vs GT)")


if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", __file__, "-q"]))
