"""Lock test for the sink g0-source bug (B2): the EMIT path (fit_multiport._fit_current_largesignal)
must derive the output conductance g0 from the AC-admittance DC real part -- EXACTLY as the report
GRADE path (report_multiport: rout = 1/|ac_y[0].real|) -- so emit == grade.

THE BUG: the emit path used a FULL-SWEEP I-V chord (Is[-1]-Is[0])/(Vs[-1]-Vs[0]) which CROSSES the
compliance turn-off knee -> the slope is dominated by the knee collapse, not the saturation-region
output conductance -> ~225x too steep on the real WuR refs -> 29-37% IVrms baked into the .va while
the report grade (AC-admittance g0) reads 0.3-1.2%. See [[insitu-sink-g0-source-bug]].
"""
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fit_isrc as ISR               # noqa: E402
import fit_multiport as FMP          # noqa: E402


def _knee_sink(g0_true=1.0e-7, idc=1.5e-6, vc=0.8, vhi=1.7):
    """A high-side-ceiling current ref (the real WuR shape): ~flat saturation current with a small
    output conductance g0_true, collapsing to ~0 above vhi. The FULL-SWEEP chord crosses that
    collapse and reads a far steeper (wrong) conductance than g0_true."""
    Vo = np.linspace(0.0, 2.0, 41)
    flat = idc + g0_true * (Vo - vc)
    gate = 0.5 * (1.0 - np.tanh((Vo - vhi) / 0.03))     # ~1 below vhi, ~0 above (sharp ceiling)
    I = flat * gate
    return Vo, I, g0_true, vc


def _y_curve(g0_true, cp=2.0e-15):
    """AC admittance Y(f) whose DC (lowest-f) real part == g0_true (+ a small parasitic cap)."""
    f = np.logspace(0, 8, 32)
    Y = g0_true + 1j * (2 * np.pi * f) * cp
    return np.c_[f, Y.real, Y.imag]


def _full_chord_g0(Vo, I):
    order = np.argsort(Vo)
    Vs, Is = Vo[order], I[order]
    return (Is[-1] - Is[0]) / (Vs[-1] - Vs[0])


def test_emit_g0_from_ac_admittance_not_full_chord():
    """With an AC admittance present, the emitted g0 == |ac_y[0].real| (the grade), NOT the
    knee-crossing full-sweep chord."""
    Vo, I, g0_true, vc = _knee_sink()
    cp = {"y": {"Lnom": _y_curve(g0_true)}}
    ivmap = {"Lnom": np.c_[Vo, I]}
    row = FMP._fit_current_largesignal("ISINK", cp, ivmap, sink_dc=vc, pol="sink",
                                       tnom_c=55.0, ref={})
    assert row is not None
    # emit g0 magnitude tracks the AC DC conductance (sink g0 is +; _fit_iv sets g0=+1/rout)
    assert abs(abs(row["g0"]) - g0_true) / g0_true < 0.05, (row["g0"], g0_true)
    # and it is materially DIFFERENT from the (buggy) full-sweep chord -> the test exercises the bug
    chord = abs(_full_chord_g0(Vo, I))
    assert chord > 3 * g0_true, chord                    # the knee really does inflate the chord
    assert abs(row["g0"]) < 0.5 * chord                  # emit no longer uses the inflated chord
    # the I-V residual collapses to the grade quality (sub-% of plateau), not the 29-37% bug
    assert row["ivrms"] < 5.0, row["ivrms"]


def test_emit_equals_grade_g0():
    """emit-path g0 (fit_multiport) == grade-path g0 (the report's rout=1/|ac_y[0].real| -> fit_isrc):
    SAME _fit_iv, SAME rout -> identical g0/knee/ivrms. This is the emit==grade invariant."""
    Vo, I, g0_true, vc = _knee_sink()
    yc = _y_curve(g0_true)
    cp = {"y": {"Lnom": yc}}
    ivmap = {"Lnom": np.c_[Vo, I]}
    emit = FMP._fit_current_largesignal("ISINK", cp, ivmap, sink_dc=vc, pol="sink",
                                        tnom_c=55.0, ref={})
    # grade path: rout from the AC DC real part, then the SAME fit_isrc._fit_iv
    ac_y = yc[:, 1] + 1j * yc[:, 2]
    rout_grade = 1.0 / abs(ac_y[0].real)
    order = np.argsort(Vo)
    iv_grade = ISR._fit_iv(Vo[order], I[order], vc=vc, pol="sink", rout=rout_grade)
    assert abs(emit["g0"] - iv_grade["g0"]) < 1e-12, (emit["g0"], iv_grade["g0"])
    assert emit["knee_side"] == iv_grade["knee_side"]


def test_fallback_chord_is_knee_agnostic():
    """With NO AC admittance, the fallback fits the slope over the conducting SATURATION region
    only (excludes the collapsed knee tail), so g0 is far closer to the true conductance than the
    full-sweep chord -- for a high-side-ceiling knee where the collapse is at the TOP of the sweep."""
    Vo, I, g0_true, vc = _knee_sink()
    cp = {}                                              # no 'y' -> fallback chord
    ivmap = {"Lnom": np.c_[Vo, I]}
    row = FMP._fit_current_largesignal("ISINK", cp, ivmap, sink_dc=vc, pol="sink",
                                       tnom_c=55.0, ref={})
    assert row is not None
    chord = abs(_full_chord_g0(Vo, I))
    assert abs(row["g0"]) < 0.5 * chord                  # knee-agnostic fit beats the full chord
    assert row["ivrms"] < 10.0, row["ivrms"]


if __name__ == "__main__":
    test_emit_g0_from_ac_admittance_not_full_chord()
    test_emit_equals_grade_g0()
    test_fallback_chord_is_knee_agnostic()
    print("B2 sink-rout emit==grade lock: PASS")
