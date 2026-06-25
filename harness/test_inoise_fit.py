"""Lock the current-noise fit (fit_isrc._fit_noise). Real PMU-sink current noise has a flicker
exponent slightly BELOW 1 (it falls slower than 1/f). Fitting the af=1 model In=sqrt(iw^2+kf/f),
a LINEAR least-squares in In^2 (power) is dominated by the large low-frequency flicker values and
inflates in_white 5-30x -> the PSD scores 9-15 dB off (the real i500n/i3p6u/i1p5u_ptat miss). The
fix fits in LOG-AMPLITUDE space, weighting every decade like the dB score does, which recovers the
true HF white floor. Pure numpy; no simulator -- the real-GT win (9-15 -> <1.5 dB) is validated
locally, silicon GT stays off the repo.
"""
import pathlib
import sys

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import fit_isrc as FI  # noqa: E402


def _dbrms(model, gt):
    return float(np.sqrt(np.mean((20 * np.log10(model / gt)) ** 2)))


def _power_ls(f, In):
    """the legacy linear LS in In^2 vs 1/f (what _fit_noise used to do)."""
    x = 1.0 / f
    c, *_ = np.linalg.lstsq(np.vstack([np.ones_like(x), x]).T, In ** 2, rcond=None)
    return np.sqrt(max(c[0], 0.0)), max(c[1], 0.0)


def test_log_fit_recovers_white_floor_on_subunity_flicker():
    f = np.logspace(1, 8, 60)
    iw_true = 4e-13
    kf = (3e-11) ** 2 * f[0] ** 0.85
    In = np.sqrt(iw_true ** 2 + kf / f ** 0.85)              # af=0.85 -- the real sub-1/f flicker
    p = FI._fit_noise(f, In)
    pred = np.sqrt(p["in_white"] ** 2 + p["in_kf"] / f)
    db = _dbrms(pred, In)
    assert db < 2.5, f"log noise fit should track the sub-1/f spectrum, got {db:.2f} dB"
    assert abs(p["in_white"] / iw_true - 1.0) < 0.5, \
        f"white floor must be recovered, got {p['in_white']:.2e} vs {iw_true:.2e}"
    # the legacy power LS inflates in_white and scores materially worse on the SAME data
    iwp, kfp = _power_ls(f, In)
    db_p = _dbrms(np.sqrt(iwp ** 2 + kfp / f), In)
    assert db_p > db + 4.0, f"power LS must be materially worse ({db_p:.2f} vs {db:.2f} dB)"
    assert iwp > 3 * iw_true, f"power LS should inflate in_white (got {iwp:.2e} vs true {iw_true:.2e})"


def test_clean_white_plus_flicker_unchanged():
    """A CLEAN af=1 white+flicker (In^2 exactly linear in 1/f) is recovered to ~0 dB -- the log fit
    must not regress the easy case the power LS already nailed."""
    f = np.logspace(1, 8, 50)
    iw_true, kf_true = 5e-13, 2e-20
    In = np.sqrt(iw_true ** 2 + kf_true / f)
    p = FI._fit_noise(f, In)
    assert abs(p["in_white"] / iw_true - 1.0) < 0.05, f"in_white off: {p['in_white']:.2e}"
    assert abs(p["in_kf"] / kf_true - 1.0) < 0.05, f"in_kf off: {p['in_kf']:.2e}"
    assert p["in_r2"] > 0.99


def test_degenerate_inputs_safe():
    """Too few points / non-positive values must not raise (a coverage-light or noisy port)."""
    assert FI._fit_noise(np.array([100.0]), np.array([1e-12]))["in_white"] == 1e-12
    out = FI._fit_noise(np.array([10.0, 100.0, 1e3, 1e4]),
                        np.array([1e-11, 0.0, -1.0, 1e-12]))   # zeros/negatives filtered
    assert np.isfinite(out["in_white"]) and out["in_white"] >= 0
