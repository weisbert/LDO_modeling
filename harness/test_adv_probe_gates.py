"""Synthetic lock tests for the ADVERSARIAL overfit-probe gates (HANDOFF_ADVERSARIAL_OVERFIT_
PROBE.md §C). Each gate's metric MUST fire on the known-bad pattern it is meant to catch and stay
quiet on the benign pattern -- so the gate is a real detector, not a tripwire. These exercise the
metric MATH analytically (no ngspice) + the graceful skip when the npz predates the held-out capture.

  python -m pytest harness/test_adv_probe_gates.py -q
"""
import sys
import pathlib

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fit_isrc as fi                                                   # noqa: E402
import crossval_isrc as cx                                             # noqa: E402
from score import a4_verdict                                           # noqa: E402


# ----------------------------------------------------------------- B1: interior-temp Idc miss
def _b1_worst_miss(idcT_5):
    """Fit the 3-temp linear law (-40/55/125) and return the worst |%miss| at the held-out 25/85."""
    temps3 = np.array([-40.0, 55.0, 125.0])
    idc3 = np.array([idcT_5[-40.0], idcT_5[55.0], idcT_5[125.0]])
    p = fi._fit_temp(temps3, idc3)
    miss = []
    for T in (25.0, 85.0):
        m = fi.predict_idcT(p, T)
        miss.append(abs(m - idcT_5[T]) / abs(idcT_5[T]) * 100)
    return max(miss), p["d2"]


def test_b1_fires_on_ushaped_idcT():
    # U-shaped Idc(T): min ~55C, wings ~15% higher -> 3-temp line through the wings+min misses 25/85
    ushape = {-40.0: 1.15e-6, 25.0: 1.01e-6, 55.0: 1.00e-6, 85.0: 1.03e-6, 125.0: 1.15e-6}
    worst, d2 = _b1_worst_miss(ushape)
    assert worst > 5.0, f"B1 should expose a U-shaped Idc(T); worst miss only {worst:.2f}%"
    assert d2 == 0.0, "with 3 fit temps the quadratic d2 must NOT engage (the whole point of B1)"


def test_b1_quiet_on_linear_idcT():
    # benign linear PTAT: the 3-temp line nails the interior temps
    lin = {T: 1e-6 * (1 + 0.0008 * (T - 55)) for T in (-40.0, 25.0, 55.0, 85.0, 125.0)}
    worst, _ = _b1_worst_miss(lin)
    assert worst < 5.0, f"B1 must stay quiet on a linear Idc(T); got {worst:.2f}%"


# ----------------------------------------------------------------- B2: band |Y| two-zero miss
def _y_two_zero(f, g0, wz1, wz2, wp1, wp2, cp):
    s = 1j * 2 * np.pi * f
    return g0 * (1 + s / wz1) * (1 + s / wz2) / ((1 + s / wp1) * (1 + s / wp2)) + s * cp


def test_b2_fires_on_two_zeros():
    # two zeros separated by ~3 decades, each settling to a plateau -> TWO rising steps in Re(Y);
    # the single zero-pole leaves residual AND the step-counter reports >=2.
    f = np.logspace(1, np.log10(5e8), 400)
    g0 = 1e-8
    Ygt = _y_two_zero(f, g0, wz1=2*np.pi*1e4, wz2=2*np.pi*1e7,
                      wp1=2*np.pi*2e5, wp2=2*np.pi*2e8, cp=2e-16)
    af = fi._fit_admittance(f, Ygt, g0)                 # single zero-pole keep-best
    p = dict(g0=g0, cp=af["cp"], y_wz=af["y_wz"], y_wp=af["y_wp"])
    rms = fi._y_rms_db(fi.predict_y(p, f), Ygt)
    n_steps, _ = cx._count_y_zero_steps(f, Ygt)
    assert rms > cx.G_Y_RMS_DB, f"single zero-pole should leave residual; got {rms:.2f} dB"
    assert n_steps >= 2, f"the step-counter must find both zeros; got {n_steps}"


def test_b2_quiet_on_single_zero():
    # one zero -> ONE rising step; even if rms is nonzero, B2 must not flag it (no 2nd zero)
    f = np.logspace(1, np.log10(5e8), 400)
    g0 = 1e-8
    s = 1j * 2 * np.pi * f
    Ygt = g0 * (1 + s / (2*np.pi*1e5)) / (1 + s / (2*np.pi*1e7)) + s * 2e-16
    n_steps, _ = cx._count_y_zero_steps(f, Ygt)
    assert n_steps < 2, f"a single zero must give <2 steps; got {n_steps}"


# ----------------------------------------------------------------- B3: off-vc PSRR sign flip
def test_b3_fires_on_flip_with_single_sign_model():
    # GT flips (+ at lo, - at hi); the single-gdd model is one sign everywhere -> >=1 wrong
    flips, wrong, exposed = cx._psrr_offvc_exposed(g_gt_lo=+5e-9, g_gt_hi=-4e-9,
                                                   g_md_lo=+1e-9, g_md_hi=+1e-9)
    assert flips and wrong >= 1 and exposed


def test_b3_quiet_when_no_flip():
    flips, wrong, exposed = cx._psrr_offvc_exposed(g_gt_lo=-5e-9, g_gt_hi=-4e-9,
                                                   g_md_lo=+1e-9, g_md_hi=+1e-9)
    assert (not flips) and (not exposed)            # GT never flips -> not a B3 case


def test_b3_quiet_when_model_tracks_sign():
    flips, wrong, exposed = cx._psrr_offvc_exposed(g_gt_lo=+5e-9, g_gt_hi=-4e-9,
                                                   g_md_lo=+2e-9, g_md_hi=-2e-9)
    assert flips and wrong == 0 and (not exposed)   # a (hypothetical) sign-tracking model is fine


# ----------------------------------------------------------------- B4: IV(Vo,T) moving knee
def _iv_grid(vk_by_T):
    """Build a synthetic IV(Vo,T) grid [nVo, nT] (sink, low-side knee) with knee Vk(T)."""
    Vo = np.linspace(0.0, 1.05, 211)
    cols = [1e-6 * fi.gate(Vo, vk, 2.5, "sink", side="lo") for vk in vk_by_T]
    return Vo, np.array(cols).T


def test_b4_knee_shift_fires_on_moving_knee():
    # knee climbs 0.10 -> 0.30 V across temp -> a real T x compliance cross-term (>40 mV)
    gtv, gti = _iv_grid([0.10, 0.18, 0.30])
    shift, vk = cx._knee_shift_mv(gtv, gti)
    assert shift > cx.G_KNEE_SHIFT_MV, f"B4 knee-shift should fire on a moving knee; only {shift:.1f} mV"


def test_b4_knee_shift_quiet_on_fixed_knee():
    # benign mirror: knee barely drifts -> below the 40 mV bar (matches the 7-11 mV baseline cluster)
    gtv, gti = _iv_grid([0.175, 0.18, 0.185])
    shift, vk = cx._knee_shift_mv(gtv, gti)
    assert shift < cx.G_KNEE_SHIFT_MV, f"B4 knee-shift must stay quiet on a fixed knee; got {shift:.1f} mV"


# ----------------------------------------------------------------- A4: large-signal class-AB
def test_a4_fires_on_high_wrms():
    extra = {"big": dict(wrms=40.0, asymg=0.1, asymm=0.1),
             "slew": dict(wrms=5.0, asymg=0.1, asymm=0.1)}
    assert a4_verdict(extra) is True


def test_a4_fires_on_asymmetry_mismatch():
    # waveform rms ok-ish but the GT is strongly asymmetric and the model symmetric
    extra = {"big": dict(wrms=10.0, asymg=0.55, asymm=0.05)}
    assert a4_verdict(extra) is True


def test_a4_quiet_on_clean_linear():
    extra = {"big": dict(wrms=8.0, asymg=0.10, asymm=0.09),
             "slew": dict(wrms=9.0, asymg=0.12, asymm=0.11)}
    assert a4_verdict(extra) is False


def test_a4_quiet_when_no_steps():
    assert a4_verdict({}) is False


# ----------------------------------------------------------------- graceful skip (legacy npz)
def test_gates_skip_without_heldout_fields():
    d = {}                                          # an npz captured before the held-out grids
    for fn in (cx.gate_heldout_idc, cx.gate_y_rms, cx.gate_psrr_offvc, cx.gate_iv_temps):
        r = fn("x", "isrc_model_x", pathlib.Path("/nonexistent.lib"), 0.5, d)
        assert r["exposed"] is False and r["metric"] is None
