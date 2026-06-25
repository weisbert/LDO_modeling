"""Lock the 2nd-order output-admittance (cascode/Wilson) zero: fit_isrc._fit_admittance +
predict_y + the passive internal-node RC emit (emit_pmu_model._current_block_largesignal).

The g0+sCp form misses the real silicon current ref's RISING Re(Y) + the mid-band cap that
DROPS to the HF Cp (i3p6u_vco 4.3dB, i1p5u_ptat 7.0dB on the WuR PMU). A single zero/pole
pair Y=g0*(1+s/wz)/(1+s/wp)+sCp captures both. Guards locked here (all SYNTHETIC, no box):
  * a real zero is recovered (yrms drops a lot) and is passive (wz<wp, Re(Y)>=0);
  * a flat g0+sCp admittance is left ALONE (keep-best -> wz=wp=None, predict_y byte-identical);
  * the emit realizes the SAME transfer as a passive Cz,Rz>0 branch (fit<->emit round-trip);
  * the branch is opt-in: absent on a flat sink -> the .va is substring-identical to before.
"""
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fit_isrc as ISR                                                   # noqa: E402
from emit_pmu_model import _current_block                               # noqa: E402

FGRID = np.logspace(1, np.log10(5e8), 47)                               # the @y char grid (10..500MHz)


def _y_pole_zero(f, g0, wz, wp, cp):
    s = 1j * 2 * np.pi * np.asarray(f, float)
    return g0 * (1.0 + s / wz) / (1.0 + s / wp) + s * cp


def _rms_db(m, g):
    return float(np.sqrt(np.mean((20 * np.log10((np.abs(m) + 1e-30) / (np.abs(g) + 1e-30))) ** 2)))


def test_recovers_a_real_cascode_zero():
    """A GT with a genuine zero (wp/wz=20) is recovered passive (wz<wp) and the pole-zero
    predict_y cuts |Y| rms WAY below the g0+sCp baseline."""
    g0, wz, wp, cp = 3.0e-8, 2 * np.pi * 1.5e5, 2 * np.pi * 3.0e6, 5e-15
    Y = _y_pole_zero(FGRID, g0, wz, wp, cp)
    af = ISR._fit_admittance(FGRID, Y, g0)
    assert af["y_wz"] is not None and af["y_wp"] is not None, "real zero was missed"
    assert af["y_wz"] < af["y_wp"], "non-passive: zero must precede pole (wz<wp)"
    p = dict(g0=g0, cp=af["cp"], y_wz=af["y_wz"], y_wp=af["y_wp"])
    base = _rms_db(g0 + 1j * 2 * np.pi * FGRID * cp, Y)
    new = _rms_db(ISR.predict_y(p, FGRID), Y)
    assert base > 3.0, f"baseline should be a real miss, got {base:.2f}dB"
    assert new < 0.5, f"pole-zero fit should nail it, got {new:.2f}dB"
    # passive across the whole band: Re(Y) >= 0
    assert np.all(ISR.predict_y(p, FGRID).real >= -1e-18)


def test_flat_admittance_left_alone_byte_identical():
    """A pure g0+sCp admittance has NO zero -> keep-best returns wz=wp=None and predict_y is
    BYTE-IDENTICAL to the legacy g0+sCp form (no spurious 2nd-order term on a flat sink)."""
    g0, cp = 1.0e-7, 2e-15
    Y = g0 + 1j * 2 * np.pi * FGRID * cp
    af = ISR._fit_admittance(FGRID, Y, g0)
    assert af["y_wz"] is None and af["y_wp"] is None, "spurious zero on a flat admittance"
    p_new = dict(g0=g0, cp=af["cp"], y_wz=None, y_wp=None)
    p_leg = dict(g0=g0, cp=cp)                                          # legacy dict, no y_* keys
    a = ISR.predict_y(p_new, FGRID)
    b = ISR.predict_y(p_leg, FGRID)
    assert np.array_equal(a, b), "flat-admittance predict_y diverged from legacy g0+sCp"


def test_too_few_points_falls_back():
    """< Y_PZ_MIN_PTS @y points -> no fit attempted (returns the HF-cap scalar, no zero)."""
    f = FGRID[:4]
    Y = _y_pole_zero(f, 3e-8, 2 * np.pi * 1e5, 2 * np.pi * 2e6, 5e-15)
    af = ISR._fit_admittance(f, Y, 3e-8)
    assert af["y_wz"] is None and af["y_wp"] is None


def test_scalar_or_mismatched_ac_y_falls_back_no_crash():
    """A view/npz carrying a SCALAR (0-D) or wrong-length ac_y alongside a real ac_f must NOT
    crash and must NOT clobber the precomputed scalar cp: fit_isrc keeps y_wz=y_wp=None and the
    scalar cp, predict_y stays the legacy g0+sCp. (Regression: a flat-admittance stub view passes
    ac_y=1.25e-9+0j with a 40-pt ac_f -> the old guard checked only ac_f.size and IndexError'd.)"""
    f = np.logspace(1, 8, 40)
    Vo = np.linspace(0.0, 1.0, 16)
    base = dict(name="t", pol="sink", vc=0.4, vdd=1.0, iv_v=Vo, iv_i=np.full_like(Vo, 1.25e-9),
                rout=8e8, cp=2e-15, psrr_f=f, psrr_g=np.zeros_like(f, complex),
                nz_f=f, nz_in=np.full_like(f, 1e-15), temps=np.array([55.0]), idcT=np.array([1.25e-9]))
    for bad in (1.25e-9 + 0j,                      # 0-D scalar (the regression shape)
                np.array(1.25e-9 + 0j),            # 0-D array
                np.full(7, 1.25e-9 + 0j)):         # wrong length vs the 40-pt ac_f
        v = dict(base, ac_f=f, ac_y=bad)
        p = ISR.fit_isrc(v)                        # must not raise
        assert p["y_wz"] is None and p["y_wp"] is None
        assert p["cp"] == 2e-15, "scalar cp was clobbered by the malformed @y path"
    # _fit_admittance itself is shape-robust (defense in depth)
    af = ISR._fit_admittance(f, 1.25e-9 + 0j, 1.25e-9)
    assert af["y_wz"] is None and af["y_wp"] is None


def test_emit_round_trip_passive_rc_matches_fit():
    """The emit realizes the fitted pole-zero as a PASSIVE series Cz-Rz branch (both > 0); the
    branch admittance + g0 + sCp reproduces predict_y across the band (fit<->emit consistency)."""
    g0, wz, wp, cp = 3.08e-8, 2 * np.pi * 1.55e5, 2 * np.pi * 9.64e5, 5.7e-15
    crow = dict(sink="IBX", pin="IBX", pol="sink", idc55=1e-4, didt=0.0, d2=0.0,
                g0=g0, vc=0.4, gdd=0.0, vknee=0.05, knee_p=1.0, knee_side="lo",
                vhi=1.05, Cp=cp, y_wz=wz, y_wp=wp, in_white=0.0, in_kf=0.0, tnom_c=55.0)
    blk = _current_block("IBX", crow, "AVDD1P0", "VSS")
    assert blk["nodes"] == ["IBX_nz"]
    # recover Cz,Rz from the emitted asg and rebuild the branch admittance Y_RC = jwCz/(1+jwCzRz)
    import re
    Cz = float(re.search(r"IBX_Cz = ([-\d.e+]+);", blk["asg"]).group(1))
    Rz = float(re.search(r"IBX_Rz = ([-\d.e+]+);", blk["asg"]).group(1))
    assert Cz > 0 and Rz > 0, "non-passive emit (Cz/Rz must be > 0)"
    w = 2 * np.pi * FGRID
    Y_emit = g0 + 1j * w * cp + (1j * w * Cz) / (1.0 + 1j * w * Cz * Rz)
    Y_fit = ISR.predict_y(dict(g0=g0, cp=cp, y_wz=wz, y_wp=wp), FGRID)
    assert _rms_db(Y_emit, Y_fit) < 1e-6, "emit RC branch does not match the fitted pole-zero"


def test_emit_absent_when_no_zero_substring_identical():
    """No y_wz/y_wp -> NO _nz node, NO Cz/Rz vars/cards: the current block is substring-identical
    to the pre-zero large-signal emit (same opt-in discipline as the d2 Idc(T) term)."""
    crow = dict(sink="IBX", pin="IBX", pol="sink", idc55=1e-4, didt=0.0, d2=0.0,
                g0=3e-8, vc=0.4, gdd=0.0, vknee=0.05, knee_p=1.0, knee_side="lo",
                vhi=1.05, Cp=5e-15, in_white=0.0, in_kf=0.0, tnom_c=55.0)
    blk = _current_block("IBX", crow, "AVDD1P0", "VSS")
    whole = blk["body"] + blk["asg"] + "".join(blk["rvars"])
    assert "IBX_nz" not in whole and "IBX_Cz" not in whole and "IBX_Rz" not in whole
    assert blk["nodes"] == []


if __name__ == "__main__":
    test_recovers_a_real_cascode_zero()
    test_flat_admittance_left_alone_byte_identical()
    test_too_few_points_falls_back()
    test_scalar_or_mismatched_ac_y_falls_back_no_crash()
    test_emit_round_trip_passive_rc_matches_fit()
    test_emit_absent_when_no_zero_substring_identical()
    print("admittance pole-zero: fit + keep-best + passivity + emit round-trip all OK")
