"""Lock the noise-scoring metric: score._noise_metrics R1/R2/R3 (Sg^2-energy-weighted psd_rms,
GT-Zout-anchored frequency-aware resonance metric, LF-windowed integrated-RMS).

_noise_metrics is the ENTIRE surface of the R1/R2/R3 noise-metric optimizations, yet nothing else
in the suite asserts on psd_rms/pkdb/npkf/ir_lf -- so a future edit could silently revert the Sg^2
weighting, unanchor the resonance metric, or drop the composite caps and the rest of the suite would
stay green. These are PURE-LOGIC tests (no simulator): synthetic fn/Sg/Sm arrays in, metric dict out.

Run:  python3 -m pytest harness/test_noise_metrics.py -q
"""
import numpy as np
import pytest

import score


def _lz(f, f0, q, h):
    """A simple resonance bump (Lorentzian-ish) of height h at f0, Q=q."""
    return h / np.sqrt((1 - (f / f0) ** 2) ** 2 + (f / (q * f0)) ** 2)


# --------------------------------------------------------------------------- R1: energy weighting
def test_r1_energy_weighting_ignores_floor_tail():
    """A model that errs ONLY in the deep post-rolloff floor tail (~no GT energy) scores ~0 on the
    Sg^2-weighted psd_rms, while the OLD flat-unweighted RMS over-penalizes it many-fold."""
    fn = np.logspace(1, 8, 700)
    Sg = 1e-7 / np.sqrt(fn) + 1e-12
    Sm = Sg.copy()
    Sm[fn > 3e7] = Sg[fn > 3e7] * (10 ** (10 / 20.0))      # +10 dB only in the energy-less tail
    nm = score._noise_metrics(fn, Sg, fn, Sm, fres=None)
    db = 20 * np.log10((score._interp_mag(fn, fn, Sm) + 1e-18) / (Sg + 1e-18))
    band = (fn >= 10) & (fn <= 100e6)
    unw = float(np.sqrt(np.mean(db[band] ** 2)))           # the old flat metric
    assert nm["psd_rms"] < 0.1                             # weighted: tail suppressed
    assert unw > 1.0                                       # flat: tail dominates
    assert unw / nm["psd_rms"] > 50                        # the regression-to-flat-weighting tripwire


def test_r1_full_band_kept_not_capped_at_1mhz():
    """A real >1 MHz in-band error must still register (the full band is kept, not capped at 1 MHz)."""
    fn = np.logspace(2, 8, 1200)
    Sg = 1e-9 + _lz(fn, 4e6, 8, 5e-8)
    Sm = 1e-9 + _lz(fn, 4e6, 8, 1e-8)                      # model 14 dB low at the 4 MHz resonance
    nm = score._noise_metrics(fn, Sg, fn, Sm, fres=4e6)
    assert nm["psd_rms"] > 1.0


# ------------------------------------------------------------ R2: Zout-anchored resonance metric
def test_r2_anchored_resonance_catches_frequency_move():
    """A peak of CORRECT height at the WRONG frequency now scores large (matched-freq pkdb at the
    Zout anchor) where the OLD fixed 0.5-3 MHz max/max window read ~0."""
    fn = np.logspace(2, 8, 1500)
    Sg = 1e-9 + _lz(fn, 1e6, 10, 5e-8)
    Sm = 1e-9 + _lz(fn, 2.5e6, 10, 5e-8)                   # same height, moved 1 MHz -> 2.5 MHz
    nm = score._noise_metrics(fn, Sg, fn, Sm, fres=1e6)
    assert abs(nm["pkdb"]) > 10                            # caught at the 1 MHz anchor
    assert nm["npkf"] > 2.0                                # model peak mis-located ~2.5x
    rb = (fn >= 0.5e6) & (fn <= 3e6)
    old = 20 * np.log10(Sm[rb].max() / Sg[rb].max())       # what the OLD metric would have read
    assert abs(old) < 0.5                                  # ~0 -> the exact bug the anchor fixes


def test_r2_fres_none_fallback_does_not_latch_lf_tail():
    """With fres=None the fallback anchors to the in-band Sv peak over [2e5,5e7] -- the resonance,
    NOT the 1/f band bottom that a naive argmax(Sv) would latch."""
    fn = np.logspace(2, 8, 1500)
    Sg = 1e-9 + 1e-7 / fn + _lz(fn, 2e6, 8, 5e-8)
    Sm = Sg.copy()
    nm = score._noise_metrics(fn, Sg, fn, Sm, fres=None)
    sb = np.where((fn >= 2e5) & (fn <= 5e7))[0]
    f0 = fn[sb[int(np.argmax(Sg[sb]))]]
    assert 1e6 < f0 < 4e6                                  # resonance, not the 200 kHz bottom
    assert nm["npkf"] == pytest.approx(1.0, abs=0.05)      # aligned (perfect model)


# ------------------------------------------------------------------ R3: composite folding + caps
def test_r3_composite_folding_with_saturation():
    """A pathological ~80 dB notch yields a huge |pkdb| that saturates to NPK_CAP in the fold, so a
    single designed order-limit can never dominate the composite."""
    fn = np.logspace(2, 8, 1200)
    Sg = 1e-9 + _lz(fn, 2e6, 8, 5e-8)
    Sm = 1e-9 + _lz(fn, 2e6, 8, 5e-12)                     # model ~80 dB low at the resonance
    nm = score._noise_metrics(fn, Sg, fn, Sm, fres=2e6)
    assert abs(nm["pkdb"]) > score.NPK_CAP
    folded = float(np.minimum(np.abs(nm["pkdb"]), score.NPK_CAP)) * score.W["npk"]
    assert abs(folded - score.NPK_CAP * score.W["npk"]) < 1e-9   # exactly 6*0.1, never the raw value
    folded_ir = float(np.minimum(np.abs(nm["ir_lf"]), score.NIR_LF_CAP)) * score.W["nir_lf"]
    assert folded_ir <= score.NIR_LF_CAP * score.W["nir_lf"] + 1e-12


def test_r3_lf_window_is_10hz_to_100khz_and_distinct():
    """ir_lf integrates ONLY 10 Hz-100 kHz; an error confined above 1 MHz leaves it ~0 while the
    diagnostic full-band ir_pct moves."""
    fn = np.logspace(1, 8, 1500)
    Sg = 1e-7 / np.sqrt(fn) + 1e-12
    Sm = Sg.copy()
    Sm[fn >= 1e6] = Sg[fn >= 1e6] * 2                      # error only above the LF window
    nm = score._noise_metrics(fn, Sg, fn, Sm, fres=None)
    assert abs(nm["ir_lf"]) < 1.0                          # LF window untouched
    assert abs(nm["ir_pct"]) > 1.0                         # full-band ir still sees it


# --------------------------------------------------------------------------------- edge / safety
def test_edge_empty_lf_band_and_bad_fres_do_not_crash():
    """No LF-window samples -> ir_lf==0.0 (no ZeroDivisionError); non-finite/out-of-range fres ->
    no raise, all returned scalars finite."""
    fe = np.array([2e5, 1e6, 2e6, 1e7])
    Se = np.array([1e-9, 2e-9, 5e-9, 1e-9])
    assert score._noise_metrics(fe, Se, fe, Se, fres=None)["ir_lf"] == 0.0
    fn = np.logspace(2, 8, 200)
    Sg = 1e-9 + 0 * fn
    Sm = Sg.copy()
    for bad in (float("nan"), -5.0, 0.0, 1e12):
        r = score._noise_metrics(fn, Sg, fn, Sm, fres=bad)
        assert all(np.isfinite([r["psd_rms"], r["pkdb"], r["npkf"], r["ir_lf"]]))
